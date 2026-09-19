# 备份、恢复与版本回退

本页用于数据保护、故障恢复和版本回退。日常部署见[发布](deployment.md)；按数据价值、实际迁移和恢复需求安排备份。

## 取得恢复点

取得跨数据库、对象存储与运行文件的一致恢复点时，先关闭新业务/触发，核清在途写入并停止全部写入者；未知外部效果按[原身份对账](runbook.md#incident-与-recovery)。仅停一个实例或设 dispatch=false 不能证明全局停写。

### 一致恢复点包含什么

| 资产 | 保留内容 |
| --- | --- |
| PostgreSQL | 完整 dump、数据库身份、migration revision、停写时点；保留快照、原请求、批准、Outbox 与审计 |
| 对象存储 | Skill/文档/附件原字节、key/hash、DB 引用及存储 namespace/配置 |
| Run 与 Codex 卷 | 需要恢复的 workspace、输入、transcript、原生会话；登录材料按凭据保护 |
| 镜像与配置 | 应用与依赖 image ID、兼容配置及 context path |
| KEK | 当前及旧备份所需版本，独立于 dump 受控保管 |
| 外部事实 | 原操作 ID、回执、远端 revision 和恢复点后的写入/撤权变化 |

记录恢复点 ID、UTC 窗口、资产引用和 checksum、缺项与恢复演练结果。Redis 不是业务正本，旧队列/lease/调度须独立对账；不清空 Redis 代替恢复。当前没有跨存储自动备份或统一恢复工具，DB dump 本身不能恢复全部系统。

### 取得并检查数据库备份

在目标部署目录执行，只备份 DB；两块使用同一 shell。失败文件保留为现场，不认定为有效备份。

```bash
umask 077
SKM_BACKUP_DIR="$(mktemp -d ./skillmind-backup-XXXXXXXX)" &&
docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres sh -ceu \
  'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
  > "${SKM_BACKUP_DIR:?Backup directory is required}/database.dump"
```

成功后检查 archive 目录并生成校验值；密码不入命令行：

```bash
(
  set -eu
  : "${SKM_BACKUP_DIR:?Specify the directory created for this backup}"
  test -s "$SKM_BACKUP_DIR/database.dump"
  docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres pg_restore --list \
    < "$SKM_BACKUP_DIR/database.dump" > "$SKM_BACKUP_DIR/database.toc"
  docker image inspect skillmind/backend:0.1.0 skillmind/web:0.1.0 \
    --format '{{.RepoTags}} {{.Id}}' > "$SKM_BACKUP_DIR/images.txt"
  cd "$SKM_BACKUP_DIR"
  sha256sum database.dump > database.dump.sha256
  sha256sum --check database.dump.sha256
)
```

dump、TOC、运行文件和配置均受控保存；checksum 证明完整性，不证明来源或可恢复性。完整恢复能力须在隔离环境演练。

## 数据库恢复

**以下会删除并替换数据库，丢失恢复点后的本地数据。** 确认恢复目标、保全当前现场，并核实 dump 及关联资产、兼容镜像、旧 KEK、停写范围后执行。先设置 `SKM_RESTORE_DIR` 为已核验备份目录。

### 恢复前的停止条件

先检查材料；失败即停：

```bash
(
  set -eu
  : "${SKM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$SKM_RESTORE_DIR/database.dump"
  docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres pg_restore --list \
    < "$SKM_RESTORE_DIR/database.dump" > /dev/null
  cd "$SKM_RESTORE_DIR"
  sha256sum --check database.dump.sha256
)
```

确认其他实例也已停写，再停止本 project 应用并核 DB/角色；输出须与恢复目标一致：

```bash
docker compose --env-file "${ENV_FILE:-.env}" stop api web worker maintenance migrate object-storage-init
docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres sh -ceu '
  psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc \
    --set=ON_ERROR_STOP=1 --command="SELECT current_database(), current_user"
'
```

### 替换数据库

示例以原应用角色恢复；多 owner 或自定义权限先验证对应方案：

```bash
(
  set -eu
  : "${SKM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$SKM_RESTORE_DIR/database.dump"
  docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres sh -ceu '
    dropdb --force --username="$POSTGRES_USER" "$POSTGRES_DB"
    createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" "$POSTGRES_DB"
  '
  docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres sh -ceu '
    pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
      --no-owner --single-transaction --exit-on-error
  ' < "$SKM_RESTORE_DIR/database.dump"
)
```

单事务只保护 pg_restore，失败可能留下空库；保持业务关闭。恢复 DB 不撤销远端写入，还可能恢复旧权限和已撤销会话。

### 恢复后验证

恢复关联 blob/运行文件、兼容配置与 KEK，核 migration revision、资产字节/引用、权限与会话、队列/lease/调度及原外部回执。缺输入、transcript 或可信回执的 Run 不能认定可续行；不换键重放 UNKNOWN，也不补签历史。

恢复核验期间不要执行会自动启动后台的 `make deploy`。通过原生 Compose 分步启动已核验服务，核清事实后再放行业务 Worker、维护 Worker 和普通入口；记录实际损失、恢复耗时与未解决事项。

## 应用版本回退

先确认旧 API/Web/Worker 能理解当前 schema、数据、队列和非终态快照，再使用已核验旧镜像。旧 tag 或旧镜像包不等于完整恢复方案；不兼容时保持隔离，选择向前修复或完整恢复，不试跑破坏性 downgrade、stamp 或删除审计。
