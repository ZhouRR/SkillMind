SHELL := /bin/sh
# 旧 GNU make の .ONESHELL でも一つの argv として解釈できる形にする。
.SHELLFLAGS := -euc
.ONESHELL:
.DEFAULT_GOAL := status
ENV_FILE ?= .env
export ENV_FILE
COMPOSE = docker compose --project-directory . --file compose.yml --env-file "$(ENV_FILE)"
.PHONY: status config build deploy run stop down bootstrap-admin logs logs-follow runtime-check

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
	# 使用中判定は停止済み container も含める。Docker の非 force 削除も最終防壁にする。
	skm_image_in_use() {
		skm_container_ids=$$(docker ps -aq) || return 2
		[ -n "$$skm_container_ids" ] || return 1
		skm_used_ids=$$(docker inspect --format '{{.Image}}' $$skm_container_ids) || return 2
		printf '%s\n' "$$skm_used_ids" | grep -Fxq "$$1"
	}
	skm_cleanup_unused_tags() {
		skm_preserve_current=$$1
		for skm_repository in skillmind/backend skillmind/web; do
			skm_refs=$$(docker image ls --no-trunc --format '{{.Repository}}:{{.Tag}}' "$$skm_repository") || return
			for skm_ref in $$skm_refs; do
				case "$$skm_ref" in "$$skm_repository":'<none>') continue ;; "$$skm_repository":*) ;; *) continue ;; esac
				# 部分 archive が含まない現行 tag は load 前に消さない。
				if [ "$$skm_preserve_current" = true ] && [ "$$skm_ref" = "$$skm_repository:0.1.0" ]; then continue; fi
				skm_image_id=$$(docker image inspect "$$skm_ref" --format '{{.Id}}') || return
				if skm_image_in_use "$$skm_image_id"; then continue; else
					skm_use_code=$$?
					[ "$$skm_use_code" -eq 1 ] || return "$$skm_use_code"
				fi
				docker image rm "$$skm_ref" || return
			done
		done
	}
	step configuration
	test -f "$(ENV_FILE)" && test -s "$(ENV_FILE)" || { echo 'Missing or empty environment file.' >&2; exit 1; }
	test -f images.tar && test -s images.tar || { echo 'Missing or empty images.tar. Copy the exported archive here.' >&2; exit 1; }
	$(COMPOSE) config --quiet
	step cleanup-old-images
	skm_old_backend_ids=$$(docker image ls --no-trunc --format '{{.ID}}' skillmind/backend)
	skm_old_web_ids=$$(docker image ls --no-trunc --format '{{.ID}}' skillmind/web)
	skm_old_image_ids="$$skm_old_backend_ids $$skm_old_web_ids"
	skm_cleanup_unused_tags true
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
	$(COMPOSE) stop --timeout 60 api web worker maintenance migrate object-storage-init
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
	step backend
	# 同じ image/env から全 Backend を再作成し、旧 Worker だけを残さない。
	$(COMPOSE) up -d --no-build --no-deps --pull never --force-recreate --wait --wait-timeout 120 api worker maintenance
	step runtime-check
	$(MAKE) --no-print-directory runtime-check
	step web
	$(COMPOSE) up -d --no-build --no-deps --pull never --force-recreate --wait --wait-timeout 120 web
	step cleanup-replaced-images
	skm_cleanup_unused_tags false
	# tag の付け替えで無名になった旧 image だけを、全 container の参照確認後に削除する。
	for skm_image_id in $$skm_old_image_ids; do
		if docker image inspect "$$skm_image_id" --format '{{.Id}}' >/dev/null 2>&1; then
			if skm_image_in_use "$$skm_image_id"; then continue; else
				skm_use_code=$$?
				[ "$$skm_use_code" -eq 1 ] || exit "$$skm_use_code"
			fi
			skm_tags=$$(docker image inspect "$$skm_image_id" --format '{{json .RepoTags}}')
			case "$$skm_tags" in null|'[]') docker image rm "$$skm_image_id" ;; esac
		fi
	done
	step complete
	$(COMPOSE) ps --all

# 管理者は対話入力で初回だけ作成する。既存 account/password は変更しない。
bootstrap-admin:
	@$(COMPOSE) exec api python -m skillmind.ops.bootstrap_admin
logs:
	@$(COMPOSE) logs --tail=100
logs-follow:
	@$(COMPOSE) logs --tail=100 --follow

# 配備検査は業務受付とは分離する。モデル・DB・job の実行/再送は行わない。
runtime-check:
	@skm_expected=$$(docker image inspect skillmind/backend:0.1.0 --format '{{.Id}}')
	skm_reference=
	for skm_service in api worker maintenance; do
		skm_containers=$$($(COMPOSE) ps -q "$$skm_service")
		[ -n "$$skm_containers" ] || { printf 'Backend service is not running: %s\n' "$$skm_service" >&2; exit 1; }
		for skm_container in $$skm_containers; do
			skm_image=$$(docker inspect "$$skm_container" --format '{{.Image}}')
			[ "$$skm_image" = "$$skm_expected" ] || { printf 'Backend image mismatch: %s\n' "$$skm_service" >&2; exit 1; }
			skm_identity=$$(docker exec "$$skm_container" python -m skillmind.ops.runtime_identity --fingerprint)
			case "$$skm_identity" in sha256:*) ;; *) echo 'Invalid runtime identity response.' >&2; exit 1 ;; esac
			skm_digest=$${skm_identity#sha256:}
			[ "$${#skm_digest}" -eq 64 ] || { echo 'Invalid runtime identity length.' >&2; exit 1; }
			case "$$skm_digest" in *[!0-9a-f]*) echo 'Invalid runtime identity digest.' >&2; exit 1 ;; esac
			if [ -z "$$skm_reference" ]; then skm_reference=$$skm_identity; fi
			[ "$$skm_identity" = "$$skm_reference" ] || { printf 'Backend configuration mismatch: %s\n' "$$skm_service" >&2; exit 1; }
		done
	done
	printf 'Backend images and configuration identities match. No model or business job was executed.\n'
