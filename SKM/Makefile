SHELL := /bin/sh
ENV_FILE ?= .env
COMPOSE_PROJECT_NAME ?= skillmind
# make 変数を shell へ渡し、dotenv は実行しない。
export ENV_FILE COMPOSE_PROJECT_NAME DAEMON_ID RELEASE_SHA256
export MAINTENANCE_CONFIRMED BACKGROUND_APPROVED WEB_ONLY_CONFIRMED
COMPOSE_CMD = sh scripts/compose.sh
DEPLOY_CMD = sh scripts/deploy.sh
.DEFAULT_GOAL := status
.PHONY: status config build run stop down deploy deploy-help deploy-load deploy-migrate deploy-api deploy-worker deploy-web bootstrap-admin logs logs-follow

status:
	@$(COMPOSE_CMD) ps
config:
	@$(COMPOSE_CMD) config --quiet
build:
	@$(COMPOSE_CMD) build api web
run: deploy-api
stop:
	@$(COMPOSE_CMD) stop
down:
	@$(COMPOSE_CMD) down

# 全 writer の停止は事前確認。Worker は独立批准まで起動しない。
deploy:
	@$(DEPLOY_CMD) all
deploy-help:
	@$(DEPLOY_CMD) help
deploy-load deploy-migrate deploy-api deploy-worker deploy-web:
	@$(DEPLOY_CMD) $(patsubst deploy-%,%,$@)

# 管理者作成は volume 削除・全 service 起動を伴わない。
bootstrap-admin:
	@$(COMPOSE_CMD) exec api python -m skillmind.ops.bootstrap_admin
logs:
	@$(COMPOSE_CMD) logs --tail=100
logs-follow:
	@$(COMPOSE_CMD) logs --tail=100 --follow
