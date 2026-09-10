SHELL := /bin/sh
# 旧 GNU make の .ONESHELL でも一つの argv として解釈できる形にする。
.SHELLFLAGS := -euc
.ONESHELL:
.DEFAULT_GOAL := status
ENV_FILE ?= .env
export ENV_FILE
COMPOSE = docker compose --project-directory . --file compose.yml --env-file "$(ENV_FILE)"
.PHONY: status config build deploy run stop down bootstrap-admin logs logs-follow

status:
	@$(COMPOSE) ps --all
config:
	@$(COMPOSE) config --quiet
build:
	@$(COMPOSE) build api web
run: deploy
stop:
	@$(COMPOSE) stop
down:
	@$(COMPOSE) down

# 初回も更新も同じ手順。失敗後は自動再試行・DB rollback・volume 削除を行わない。
deploy:
	@skm_stage=configuration
	trap 'skm_code=$$?; if [ "$$skm_code" -ne 0 ]; then printf "Deployment failed at [%s] (exit %s). See the error above; inspect before retry.\n" "$$skm_stage" "$$skm_code" >&2; fi' 0
	trap 'exit 130' INT
	trap 'exit 143' TERM
	step() { skm_stage="$$1"; printf '\n[%s]\n' "$$skm_stage"; }
	step configuration
	test -f "$(ENV_FILE)" && test -s "$(ENV_FILE)" || { echo 'Missing or empty environment file.' >&2; exit 1; }
	test -f images.tar && test -s images.tar || { echo 'Missing or empty images.tar. Copy the exported archive here.' >&2; exit 1; }
	$(COMPOSE) config --quiet
	step load-images
	docker image load --input images.tar
	step prepare-images
	# Application image は必ず offline archive/local から使う。基盤のみ不足時に取得する。
	docker image inspect skillmind/backend:0.1.0 skillmind/web:0.1.0 --format '{{.Id}}'
	$(COMPOSE) pull --policy missing postgres redis object-storage object-storage-init
	skm_platform=$$(docker info --format '{{.OSType}}/{{.Architecture}}')
	case "$$skm_platform" in linux/x86_64) skm_platform=linux/amd64 ;; linux/aarch64) skm_platform=linux/arm64 ;; esac
	case "$$skm_platform" in linux/amd64|linux/arm64) ;; *) echo 'Unsupported Docker server platform.' >&2; exit 1 ;; esac
	skm_images=$$($(COMPOSE) config --images)
	for skm_image in $$skm_images; do
		skm_actual=$$(docker image inspect "$$skm_image" --format '{{.Os}}/{{.Architecture}}')
		[ "$$skm_actual" = "$$skm_platform" ] || { printf 'Image platform mismatch: %s (%s, server %s)\n' "$$skm_image" "$$skm_actual" "$$skm_platform" >&2; exit 1; }
	done
	step stop-application
	$(COMPOSE) stop --timeout 60 api web worker migrate object-storage-init
	step infrastructure
	$(COMPOSE) up -d --no-build --no-deps --pull never --wait --wait-timeout 120 postgres redis object-storage
	step initialize-storage
	$(COMPOSE) up --no-build --no-deps --pull never --force-recreate --exit-code-from object-storage-init object-storage-init
	step migration-check
	# run の pull policy は compose.yml に置き、旧 Compose の未対応 flag を避ける。
	$(COMPOSE) run --rm -T --no-deps migrate python -m skillmind.ops.preflight --migration-plan
	step migrate
	$(COMPOSE) run --rm -T --no-deps migrate
	step readiness
	$(COMPOSE) run --rm -T --no-deps migrate python -m skillmind.ops.preflight
	step api-web
	$(COMPOSE) up -d --no-build --no-deps --pull never --wait --wait-timeout 120 api web
	step worker
	$(COMPOSE) up -d --no-build --no-deps --pull never --wait --wait-timeout 120 worker
	step complete
	$(COMPOSE) ps --all

# 管理者は対話入力で初回だけ作成する。既存 account/password は変更しない。
bootstrap-admin:
	@$(COMPOSE) exec api python -m skillmind.ops.bootstrap_admin
logs:
	@$(COMPOSE) logs --tail=100
logs-follow:
	@$(COMPOSE) logs --tail=100 --follow
