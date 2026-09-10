#!/bin/sh
# host は Docker 操作、隔離 Backend container は JSON/配備契約の検査を担当する。
set -eu
umask 077
fail() { echo 'Deployment stopped; result may be unknown. Keep isolation; inspect before retry.' >&2; exit 1; }
skm_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$skm_root"
skm_phase=${1-help}
case "$skm_phase" in
    help) echo 'Phases: all, load, migrate, api, worker, web. See docs/operations/deployment.md.'; exit 0 ;;
    all|load|migrate|api|worker|web) ;;
    *) fail ;;
esac
[ "${MAINTENANCE_CONFIRMED-}" = 1 ] || fail
if [ "$skm_phase" = worker ]; then [ "${BACKGROUND_APPROVED-}" = 1 ] || fail; fi
if [ "$skm_phase" = web ]; then [ "${WEB_ONLY_CONFIRMED-}" = 1 ] || fail; fi
[ -n "${DAEMON_ID-}" ] || fail
skm_project=${COMPOSE_PROJECT_NAME-skillmind}
case "$skm_project" in ''|[!a-z0-9]*|*[!a-z0-9_-]*) fail ;; esac
export COMPOSE_PROJECT_NAME="$skm_project"

# checksum は転送 package と独立した経路で確認する。dotenv の source/eval はしない。
hex64() { [ "${#1}" = 64 ] && case "$1" in *[!0-9a-f]*) return 1 ;; esac; }
hex64 "${RELEASE_SHA256-}" || fail
[ -f SHA256SUMS ] || fail
echo 'Verifying release files...'
skm_digest=$(sha256sum SHA256SUMS)
[ "${skm_digest%% *}" = "$RELEASE_SHA256" ] || fail
skm_seen='|'
skm_archive_sha=
[ -f images.tar ] && [ ! -L images.tar ] || fail
exec 9< images.tar
while read -r skm_hash skm_file skm_extra; do
    hex64 "$skm_hash" && [ -z "$skm_extra" ] || fail
    case "$skm_file" in compose.yaml|Makefile|.env.example|scripts/compose.sh|scripts/deploy.sh|release.env|images.tar) ;; *) fail ;; esac
    case "$skm_seen" in *"|$skm_file|"*) fail ;; esac
    skm_seen="$skm_seen$skm_file|"
    [ -f "$skm_file" ] && [ ! -L "$skm_file" ] || fail
    if [ "$skm_file" = images.tar ]; then
        skm_digest=$(sha256sum "/proc/$$/fd/9")
    else
        skm_digest=$(sha256sum "$skm_file")
    fi
    [ "${skm_digest%% *}" = "$skm_hash" ] || fail
    if [ "$skm_file" = images.tar ]; then skm_archive_sha=$skm_hash; fi
done < SHA256SUMS
for skm_file in compose.yaml Makefile .env.example scripts/compose.sh scripts/deploy.sh release.env images.tar; do
    case "$skm_seen" in *"|$skm_file|"*) ;; *) fail ;; esac
done
skm_keys='|'
while IFS='=' read -r skm_key skm_value; do
    case "$skm_keys" in *"|$skm_key|"*) fail ;; esac
    skm_keys="$skm_keys$skm_key|"
    case "$skm_key" in
        FORMAT) [ "$skm_value" = 1 ] || fail ;;
        BACKEND_IMAGE_ID) BACKEND_IMAGE_ID=$skm_value ;;
        WEB_IMAGE_ID) WEB_IMAGE_ID=$skm_value ;;
        PLATFORM) skm_platform=$skm_value ;;
        CONTEXT_PATH) skm_context=$skm_value ;;
        VERSION) case "$skm_value" in ''|*[!a-zA-Z0-9._-]*) fail ;; esac ;;
        *) fail ;;
    esac
done < release.env
[ "$skm_keys" = '|FORMAT|BACKEND_IMAGE_ID|WEB_IMAGE_ID|PLATFORM|CONTEXT_PATH|VERSION|' ] || fail
for skm_id in "$BACKEND_IMAGE_ID" "$WEB_IMAGE_ID"; do
    case "$skm_id" in sha256:*) hex64 "${skm_id#sha256:}" || fail ;; *) fail ;; esac
done
case "$skm_platform" in linux/amd64|linux/arm64) ;; *) fail ;; esac
case "$skm_context" in /*) ;; *) fail ;; esac
export BACKEND_IMAGE_ID WEB_IMAGE_ID

# 一時領域はこの invocation 専用。env/inspect の原文は公開・永続保存しない。
skm_tmp=$(mktemp -d /tmp/skillmind-deploy.XXXXXXXX)
cleanup() {
    rm -f -- "$skm_tmp/config.json" "$skm_tmp/containers.json" "$skm_tmp/ids.txt" \
        "$skm_tmp/images.txt" "$skm_tmp/refs.txt" "$skm_tmp/daemon.txt" "$skm_tmp/preflight.json"
    rmdir -- "$skm_tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
compose() { sh "$skm_root/scripts/compose.sh" "$@"; }
target() {
    skm_actual=$(docker info --format '{{.ID}}' 2>/dev/null) || fail
    [ "$skm_actual" = "$DAEMON_ID" ] || fail
    skm_arch=$(docker info --format '{{.OSType}}/{{.Architecture}}' 2>/dev/null) || fail
    case "$skm_arch" in linux/x86_64) skm_arch=linux/amd64 ;; linux/aarch64) skm_arch=linux/arm64 ;; esac
    [ "$skm_arch" = "$skm_platform" ] || fail
    printf '%s\n' "$skm_actual" > "$skm_tmp/daemon.txt"
    compose config --quiet || fail
}
ids() {
    docker ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$skm_project" \
        > "$skm_tmp/ids.txt" 2>/dev/null || fail
    while IFS= read -r skm_id; do hex64 "$skm_id" || fail; done < "$skm_tmp/ids.txt"
}
stopped() {
    target
    ids
    while IFS= read -r skm_id; do
        skm_row=$(docker container inspect --format '{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.oneoff"}}|{{.State.Status}}' "$skm_id" 2>/dev/null) || fail
        case "$skm_row" in
            *'|created'|*'|exited'|*'|dead') ;;
            postgres\|False\|running|redis\|False\|running|object-storage\|False\|running) ;;
            *) fail ;;
        esac
    done < "$skm_tmp/ids.txt"
}
check() {
    # 内容だけを read-only mount。daemon socket・network は渡さない。
    if docker run --rm --pull never --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
        --mount "type=bind,src=$skm_tmp,dst=/snapshot,readonly" \
        --mount "type=bind,src=$skm_root/images.tar,dst=/release/images.tar,readonly" \
        --entrypoint python "$BACKEND_IMAGE_ID" /opt/skillmind-deploy/deploy_checks.py \
        "$@" --backend "$BACKEND_IMAGE_ID" --web "$WEB_IMAGE_ID" \
        --project "$skm_project" --daemon "$DAEMON_ID" --context-path "$skm_context" 2>/dev/null; then
        return 0
    else
        echo "Validation failed during $1; inspect identity/configuration/health privately." >&2
        fail
    fi
}
snapshot() {
    target
    compose config --format json > "$skm_tmp/config.json" || fail
    ids
    set --
    while IFS= read -r skm_id; do set -- "$@" "$skm_id"; done < "$skm_tmp/ids.txt"
    if [ "$#" = 0 ]; then printf '[]\n' > "$skm_tmp/containers.json";
    else docker container inspect "$@" > "$skm_tmp/containers.json" 2>/dev/null || fail; fi
    compose config --images > "$skm_tmp/refs.txt" || fail
    if [ "$#" != 0 ]; then
        docker container inspect --format '{{.Image}}' "$@" >> "$skm_tmp/refs.txt" 2>/dev/null || fail
    fi
    : > "$skm_tmp/images.txt"
    while IFS= read -r skm_ref; do
        [ -n "$skm_ref" ] || fail
        printf '%s\n' "$skm_ref" >> "$skm_tmp/images.txt"
        docker image inspect --format '{"id":{{json .Id}},"environment":{{json .Config.Env}}}' \
            "$skm_ref" >> "$skm_tmp/images.txt" 2>/dev/null || fail
    done < "$skm_tmp/refs.txt"
}
gate() { installed; snapshot; check state --mode "$1"; }
preflight() {
    if [ "$1" = plan ]; then
        compose run --rm -T --no-deps --pull never migrate python -m skillmind.ops.preflight --migration-plan > "$skm_tmp/preflight.json" || fail
        check preflight --migration-plan
    else
        if [ "$1" = api ]; then
            compose exec -T api python -m skillmind.ops.preflight > "$skm_tmp/preflight.json" || fail
        else
            compose run --rm -T --no-deps --pull never migrate python -m skillmind.ops.preflight > "$skm_tmp/preflight.json" || fail
        fi
        check preflight
    fi
}
start() { target; compose up -d --no-build --no-deps --pull never --wait --wait-timeout 120 "$@" >/dev/null || fail; }
load_images() {
    stopped
    echo 'Loading verified release images...'
    # 初回は helper も未導入。checksum 後に load し、内部検査前は業務を起動しない。
    docker image load <&9 >/dev/null 2>&1 || fail
    target
    installed
    check archive --checksum "$skm_archive_sha"
    stopped
}
installed() {
    for skm_image in "$BACKEND_IMAGE_ID" "$WEB_IMAGE_ID"; do
        skm_installed=$(docker image inspect --format '{{.Id}}|{{.Os}}/{{.Architecture}}' "$skm_image" 2>/dev/null) || fail
        [ "$skm_installed" = "$skm_image|$skm_platform" ] || fail
    done
    skm_built_context=$(docker image inspect --format '{{index .Config.Labels "org.skillmind.context-path"}}' "$WEB_IMAGE_ID" 2>/dev/null) || fail
    [ "$skm_built_context" = "$skm_context" ] || fail
}
migrate() {
    echo 'Checking and applying migrations...'
    gate stopped
    preflight plan
    gate stopped
    compose run --rm -T --no-deps --pull never migrate >/dev/null || fail
    preflight ready
}
api() {
    echo 'Checking and starting API/Web (Worker remains stopped)...'
    gate stopped; preflight ready; gate stopped; start api web; gate frontend; preflight api
}
case "$skm_phase" in
    all) load_images; migrate; api ;;
    load) load_images ;;
    migrate) migrate ;;
    api) api ;;
    worker) gate frontend; preflight api; gate frontend; start worker; gate worker ;;
    web)
        # Backend は既存 image と一致する必要がある。queue/DB/Worker の操作はしない。
        target
        docker image load <&9 >/dev/null 2>&1 || fail
        installed
        check archive --checksum "$skm_archive_sha"
        gate web-before
        start web
        gate web-after ;;
esac
echo "Deployment phase $skm_phase completed. Background/ordinary ingress require separate approval."
