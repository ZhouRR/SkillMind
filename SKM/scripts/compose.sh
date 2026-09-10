#!/bin/sh
# dotenv は実行せず、Compose 自身に同一の絶対 file を解釈させる。
set -eu
fail() { echo 'Compose failed; check the selected environment privately.' >&2; exit 1; }
skm_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$skm_root"
skm_env=${ENV_FILE-.env}
[ -n "$skm_env" ] && [ -f "$skm_env" ] || fail
skm_env=$(realpath -- "$skm_env")
skm_project=${COMPOSE_PROJECT_NAME-skillmind}
case "$skm_project" in ''|[!a-z0-9]*|*[!a-z0-9_-]*) fail ;; esac
unset COMPOSE_FILE COMPOSE_ENV_FILES COMPOSE_PATH_SEPARATOR COMPOSE_PROFILES
export COMPOSE_PROJECT_NAME="$skm_project" SKM_COMPOSE_ENV_FILE="$skm_env"
export COMPOSE_DISABLE_ENV_FILE=true COMPOSE_PROFILES= COMPOSE_COMPATIBILITY=false
for skm_pin in "${BACKEND_IMAGE_ID-}" "${WEB_IMAGE_ID-}"; do
    case "$skm_pin" in
        '') ;;
        sha256:*)
            skm_digest=${skm_pin#sha256:}
            [ "${#skm_digest}" = 64 ] || fail
            case "$skm_digest" in *[!0-9a-f]*) fail ;; esac ;;
        *) fail ;;
    esac
done
export SKM_BACKEND_IMAGE=${BACKEND_IMAGE_ID:-skillmind/backend:0.1.0}
export SKM_WEB_IMAGE=${WEB_IMAGE_ID:-skillmind/web:0.1.0}
# 固定 target の上書きを拒否する。exec/run の container command はそのまま渡す。
[ "$#" -gt 0 ] || fail
skm_command=$1
case "$skm_command" in [a-z]* ) ;; *) fail ;; esac
skm_skip=0
skm_first=1
for skm_arg in "$@"; do
    if [ "$skm_first" = 1 ]; then skm_first=0; continue; fi
    if [ "$skm_skip" = 1 ]; then skm_skip=0; continue; fi
    case "$skm_arg" in
        --file|--file=*|--env-file*|--env-from-file*|--project-*|--profile*|--context*|--host*|--config*|--compatibility|-p*|-H*|-c*) fail ;;
        -f*) [ "$skm_command" = logs ] || fail ;;
    esac
    case "$skm_command" in exec|run)
        case "$skm_arg" in
            --env|-e|--index|--user|-u|--workdir|-w|--entrypoint|--name|--label|-l|--volume|-v|--cap-add|--cap-drop|--pull) skm_skip=1 ;;
            --) break ;;
            -*) ;;
            *) break ;;
        esac ;;
    esac
done
docker compose --project-directory "$skm_root" --file "$skm_root/compose.yaml" \
    --env-file "$skm_env" --project-name "$skm_project" "$@" 2>/dev/null || fail
