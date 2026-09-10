SHELL := /bin/sh

ENV_FILE ?= .env
PYTHON ?= python3
COMPOSE_PROJECT_NAME ?= skillmind
IMAGE_ARCHIVE ?= images/skillmind-images.tar
# Shell の再解釈で空白や引用符が source/target を変えないよう、一引数ずつ渡す。
shell_quote = '$(subst ','"'"',$(1))'
COMPOSE_CMD = $(call shell_quote,$(PYTHON)) scripts/compose.py --env-file $(call shell_quote,$(ENV_FILE)) --project-name $(call shell_quote,$(COMPOSE_PROJECT_NAME)) --
DEPLOY_CMD = $(call shell_quote,$(PYTHON)) scripts/deploy.py
DEPLOY_OPTIONS = --env-file $(call shell_quote,$(ENV_FILE)) --project-name $(call shell_quote,$(COMPOSE_PROJECT_NAME)) --daemon-id $(call shell_quote,$(DAEMON_ID)) --backend-image-id $(call shell_quote,$(BACKEND_IMAGE_ID)) --web-image-id $(call shell_quote,$(WEB_IMAGE_ID)) $(if $(filter 1,$(MAINTENANCE_CONFIRMED)),--maintenance-confirmed,) $(if $(filter 1,$(BACKGROUND_APPROVED)),--background-approved,)

# 利用できる target:
#   make                 service 状態を表示 (既定)
#   make run             deploy-api と同じ検査で API/Web だけを起動
#   make stop            container を保持したまま停止
#   make down            container と Compose network を削除 (永続 volume は保持)
#   make deploy          独立した配備段階の help を表示 (状態変更なし)
#   make deploy-load     承認済み archive を照合して load (旧 image は保持)
#   make deploy-migrate  全 writer 停止/graph を照合して migration
#   make deploy-api      head/Redis を検査し API/Web だけを起動
#   make deploy-worker   独立の背景再開承認後に Worker を起動
#   make bootstrap-admin 全 data を削除して初期化し直し、最初の ADMIN を作成 (破壊的)
#   make logs            最新 100 行の log を表示
#   make logs-follow     application log を継続表示
#
# 対象・確認変数と実行順は docs/operations/deployment.md を参照する。

.DEFAULT_GOAL := status

.PHONY: status config build run stop down deploy deploy-load deploy-migrate deploy-api deploy-worker bootstrap-admin logs logs-follow

status:
	$(COMPOSE_CMD) ps

config:
	$(COMPOSE_CMD) config --quiet

build:
	$(COMPOSE_CMD) build

# 一括起動で Worker の独立門禁を迂回しない。
run: deploy-api

stop:
	$(COMPOSE_CMD) stop

down:
	$(COMPOSE_CMD) down

# 自動的な全再起動や前段階の再実行を行わない。失敗時は現場を保全して停止する。
deploy:
	$(DEPLOY_CMD) --help

deploy-load:
	$(DEPLOY_CMD) load $(DEPLOY_OPTIONS) --archive $(call shell_quote,$(IMAGE_ARCHIVE)) --archive-sha256 $(call shell_quote,$(ARCHIVE_SHA256))

deploy-migrate deploy-api deploy-worker:
	$(DEPLOY_CMD) $(patsubst deploy-%,%,$@) $(DEPLOY_OPTIONS)

# 初期化からやり直す破壊的 target。PostgreSQL/Redis/object storage を volume ごと捨て、
# migration と bucket 作成を最初から流し直してから ADMIN を作る。
#
# `down --volumes` は run-data (Run workspace) も一緒に消す。PostgreSQL を捨てた時点でその
# workspace が属する Run は存在しなくなり、残しても参照先の無い file が積み上がるだけになる。
#
# 名前だけ見ると「ADMIN を作るだけ」に読めるため、既定では実行前に確認を求める。自動化からは
# FORCE=1 で省略できる。password は argv/environment/pipe に載せず TTY から読む。
bootstrap-admin:
	$(COMPOSE_CMD) config --quiet
	@printf '警告: この操作は %s が指す環境の全 data を削除します。\n' $(call shell_quote,$(ENV_FILE)) >&2
	@echo "  削除対象: PostgreSQL (Run/Evidence/Result/Evaluation/Skill/Project/User)、" >&2
	@echo "            Redis (queue/session)、object storage (Skill/文書 blob)、Run workspace。" >&2
	@test $(call shell_quote,$(FORCE)) = "1" || { \
		printf "続行するには wipe と入力してください: " >&2; \
		read -r answer; \
		test "$$answer" = "wipe" || { echo "中止しました。" >&2; exit 1; }; \
	}
	$(COMPOSE_CMD) down --volumes
	$(COMPOSE_CMD) up -d --no-build
	$(COMPOSE_CMD) exec api python -m skillmind.ops.bootstrap_admin

logs:
	$(COMPOSE_CMD) logs --tail=100

logs-follow:
	$(COMPOSE_CMD) logs --tail=100 --follow
