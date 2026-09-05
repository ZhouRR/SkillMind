SHELL := /bin/sh

ENV_FILE ?= .env
COMPOSE ?= docker compose
DOCKER ?= docker
IMAGE_ARCHIVE ?= images/projectmind-images.tar
APP_IMAGES := projectmind/backend:0.1.0 projectmind/web:0.1.0
COMPOSE_CMD = $(COMPOSE) --env-file $(ENV_FILE)

# 利用できる target:
#   make                 service 状態を表示 (既定)
#   make run             配置済み image から application を起動
#   make stop            container を保持したまま停止
#   make down            container と Compose network を削除 (永続 volume は保持)
#   make deploy          旧 application image を tar file の内容で置換して再起動
#   make bootstrap-admin 全 data を削除して初期化し直し、最初の ADMIN を作成 (破壊的)
#   make logs            最新 100 行の log を表示
#   make logs-follow     application log を継続表示
#
# 環境 file の変更例: make run ENV_FILE=.env.production

.DEFAULT_GOAL := status

.PHONY: status check-env run stop down deploy bootstrap-admin logs logs-follow

status: check-env
	$(COMPOSE_CMD) ps

check-env:
	@test -f "$(ENV_FILE)" || { \
		echo "環境 file が見つかりません: $(ENV_FILE)" >&2; \
		echo "作成方法: cp .env.example $(ENV_FILE)" >&2; \
		exit 1; \
	}

# Server 上では事前配置済み image のみを使い、誤って source build しないよう明示する。
run: check-env
	$(COMPOSE_CMD) up -d --no-build
	$(COMPOSE_CMD) ps

stop: check-env
	$(COMPOSE_CMD) stop

down: check-env
	$(COMPOSE_CMD) down

# Container を先に削除し、使用中 image の強制削除や旧 layer の誤使用を防止する。
deploy: check-env
	@test -f "$(IMAGE_ARCHIVE)" || { \
		echo "Image archive が見つかりません: $(IMAGE_ARCHIVE)" >&2; \
		exit 1; \
	}
	$(COMPOSE_CMD) down
	@for image in $(APP_IMAGES); do \
		if $(DOCKER) image inspect "$$image" >/dev/null 2>&1; then \
			echo "Remove old image: $$image"; \
			$(DOCKER) image rm --force "$$image"; \
		else \
			echo "Old image is not present: $$image"; \
		fi; \
	done
	$(DOCKER) load --input "$(IMAGE_ARCHIVE)"
	@for image in $(APP_IMAGES); do \
		$(DOCKER) image inspect "$$image" >/dev/null 2>&1 || { \
			echo "Loaded archive does not contain required image: $$image" >&2; \
			exit 1; \
		}; \
	done
	$(COMPOSE_CMD) up -d --no-build
	$(COMPOSE_CMD) ps

# 初期化からやり直す破壊的 target。PostgreSQL/Redis/object storage を volume ごと捨て、
# migration と bucket 作成を最初から流し直してから ADMIN を作る。
#
# `down --volumes` は run-data (Run workspace) も一緒に消す。PostgreSQL を捨てた時点でその
# workspace が属する Run は存在しなくなり、残しても参照先の無い file が積み上がるだけになる。
#
# 名前だけ見ると「ADMIN を作るだけ」に読めるため、既定では実行前に確認を求める。自動化からは
# FORCE=1 で省略できる。password は argv/environment/pipe に載せず TTY から読む。
bootstrap-admin: check-env
	@echo "警告: この操作は $(ENV_FILE) が指す環境の全 data を削除します。" >&2
	@echo "  削除対象: PostgreSQL (Run/Evidence/Result/Evaluation/Skill/Project/User)、" >&2
	@echo "            Redis (queue/session)、object storage (Skill/文書 blob)、Run workspace。" >&2
	@test "$(FORCE)" = "1" || { \
		printf "続行するには wipe と入力してください: " >&2; \
		read -r answer; \
		test "$$answer" = "wipe" || { echo "中止しました。" >&2; exit 1; }; \
	}
	$(COMPOSE_CMD) down --volumes
	$(COMPOSE_CMD) up -d --no-build
	$(COMPOSE_CMD) exec api python -m projectmind.ops.bootstrap_admin

logs: check-env
	$(COMPOSE_CMD) logs --tail=100

logs-follow: check-env
	$(COMPOSE_CMD) logs --tail=100 --follow
