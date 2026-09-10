# 备份、恢复与版本回退

命令在目标 `SKM/` 执行，先确认环境、受控资产和维护窗口，材料不明即停。起动见[Quickstart](quickstart.md)，发布见[迁移手册](deployment.md)。

## 配备前备份

先关新业务/触发、核清在途 Effect，未知按[原身份对账](runbook.md#incident-与-recovery)。全部 API/Worker 写入者停止后，在同一窗口取得恢复点；dispatch=false、停单实例或文件同时间戳不证明一致。

### 一致恢复点包含什么

| 资产 | 必须保留的关联 |
| --- | --- |
| DB dump | 数据库身份、migration revision、Run/快照/Outbox/批准/安全审计、上传意图/占用/清理要求、附件原字节/回执、评价原请求绑定与停写时点 |
| blob snapshot | SkillSource、文档等原对象 key、字节/hash及 DB 引用；新文档的 namespace UUID/连接描述摘要与实际存储世代须一起保留 |
| Run 文件 | 必要 workspace/transcript、输入副本、Session 恢复材料；缺文件不能标为可续行 |
| 镜像与配置 | Backend/Web 与依赖 image ID、context path、配置版本 |
| KEK | 所需旧版本的独立受控保管引用，不与 dump 放一起 |
| 外部对账事实 | 原 Proposal/Effect、目标 revision、apply/read-back 及恢复点后的变化 |

dump/blob/workspace 含敏感材料，须访问控制、不作公开附件。清单只留恢复点 ID、UTC 窗口、资产引用/校验值、操作者、结果/缺项；KEK 独立保管。负责人确定 RPO/RTO/保留期，当前无自动跨存储备份或时效保证。

以下关联不能只靠公开字段或旧 dump 重建：

- Redis 不是 DB 正本；旧队列、Outbox 重投、调度在途另定恢复方案，不清未知 Redis 全库。
- [附件](../design/results-evaluation.md#可信附件的发布与读取)：Evidence 私有原字节与 Tool/Run、size/hash 同恢复，不能用 output 文件替代；历史无绑定仍未发布。
- 评价：保留原 Result、用户和 submission_key/request_hash。恢复点后提交可能缺失，原键 GET 未见不证明从未提交，不换键/合并相似记录补历史。
- 预算：账户、预留、调用绑定、启动所有权 hash、观察/核对同恢复。hash 不重建原协调器 token，也不授新 Worker 启动权；旧 RESERVED/无绑定不证明后来未启动，未知不释放占用或换 invocation/owner 重跑。
- [文档](../design/document-lifecycle.md)：意图/原回执/占用、清理记录、关闭标记/审计同恢复；标记与审计须双向一致。旧恢复点可能漏掉后来关闭/迟到 PUT，核清前不发布、清理或结算，关闭不证明远端停止。原 key 查询不授权重放 PENDING，未来精确版本/清理凭证也纳入恢复点。

存储 UUID 不证明服务端身份。换 endpoint/bucket 或重建存储须核原资产并做受控归属迁移，当前无该工具；不复用 UUID、改摘要绕过，未绑定旧文档保留元数据并拒绝 blob 操作。目录总量不等于 bucket 占用，不能据列表删对象。

### 取得并检查数据库备份

以下仅备份 DB。两块在同一 shell 执行，mktemp/pg_dump 失败即停；新终端重新确认目录，不猜变量。密码不入命令行。

```bash
umask 077
SKM_BACKUP_DIR="$(mktemp -d ./skillmind-backup-XXXXXXXX)" &&
sh scripts/compose.sh exec -T postgres sh -ceu \
  'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
  > "${SKM_BACKUP_DIR:?Backup directory is required}/database.dump"
```

成功后查非空、archive 目录及 checksum；失败文件只作现场，不作有效备份。

```bash
(
  set -eu
  : "${SKM_BACKUP_DIR:?Specify the directory created for this backup}"
  test -s "$SKM_BACKUP_DIR/database.dump"
  sh scripts/compose.sh exec -T postgres pg_restore --list \
    < "$SKM_BACKUP_DIR/database.dump" > "$SKM_BACKUP_DIR/database.toc"
  docker image inspect skillmind/backend:0.1.0 skillmind/web:0.1.0 \
    --format '{{.RepoTags}} {{.Id}}' > "$SKM_BACKUP_DIR/images.txt"
  cd "$SKM_BACKUP_DIR"
  sha256sum database.dump > database.dump.sha256
  sha256sum --check database.dump.sha256
)
```

子 shell 遇错即停，TOC 受控保存；全部资产同清单转存。checksum 不证明来源或可恢复性；完整恢复点就绪前不删旧镜像/迁移，更不删除数据卷。

## 数据库恢复

**以下会删除并替换数据库，丢失恢复点后的本地数据。** 经负责人确认并保全当前现场后，先在隔离环境演练。

恢复旧 DB 不撤销远端变更，还可能复活撤销的 session/权限。恢复点后的撤权和 commit 均须独立对账；未核清保持隔离。没有通用权限修复/Effect 补账 CLI，不以 SQL、bootstrap 或新键补造结果。

### 恢复前的停止条件

须同时确认：环境/project/DB/角色/镜像/revision；可信 dump/checksum/archive 及关联 blob/workspace/config/KEK；当前现场备份；全写入者停止；恢复点后安全/外部事实的原身份对账方案。

明确将完整备份目录设为 `SKM_RESTORE_DIR`，保留原文件名/相对路径，先只校验材料：

```bash
(
  set -eu
  : "${SKM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$SKM_RESTORE_DIR/database.dump"
  sh scripts/compose.sh exec -T postgres pg_restore --list \
    < "$SKM_RESTORE_DIR/database.dump" > /dev/null
  cd "$SKM_RESTORE_DIR"
  sha256sum --check database.dump.sha256
)
```

成功后，在全局停写窗口停止本目录服务并核实 DB/角色：

```bash
sh scripts/compose.sh stop api worker migrate
sh scripts/compose.sh exec -T postgres sh -ceu '
  psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc \
    --set=ON_ERROR_STOP=1 --command="SELECT current_database(), current_user"
'
```

### 替换数据库

结果与批准目标完全相符才执行；示例用原应用角色，多 owner/自定义权限须另有已验证方案。

```bash
(
  set -eu
  : "${SKM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$SKM_RESTORE_DIR/database.dump"
  sh scripts/compose.sh exec -T postgres sh -ceu '
    dropdb --force --username="$POSTGRES_USER" "$POSTGRES_DB"
    createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" "$POSTGRES_DB"
  '
  sh scripts/compose.sh exec -T postgres sh -ceu '
    pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
      --no-owner --single-transaction --exit-on-error
  ' < "$SKM_RESTORE_DIR/database.dump"
)
```

单事务仅保护 pg_restore，不撤销 dropdb/createdb、blob 或外部效果；失败可能留空库，保持业务关闭。其他大库策略先演练，不删保护参数，见 [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)。

### 恢复后验证

恢复 blob/workspace、兼容镜像/配置与旧 KEK，核 revision 后按[迁移审查](deployment.md#迁移与回退审查)决定前进；Worker 保持隔离，不用普通 up 绕检查。

分别验旧权限未复活、资产字节/引用一致、终态可读与非终态可续、队列/lease/Schedule/Effect 已对账。缺回执/输入/transcript 拒绝而非补签；记录耗时、损失、范围、残余问题和放行人。未知继续隔离，preflight 不替代验收，后台与普通入口[分阶段放行](deployment.md#启动与放行)。

## 应用版本回退

旧 API/Web/Worker 能理解当前 schema、数据、队列和非终态快照才可保留数据回镜像。未知不启动旧 Worker；无可信恢复点保全现场、选 forward fix，不试跑破坏性 downgrade。

按[发布阶段](deployment.md#迁移前置与执行)重验旧 archive/checksum/image ID、当前 schema/配置及 daemon/project。保留旧镜像不保证 archive、第三方镜像或恢复点齐全，同名 tag 不算方案；旧 migration-plan/head 不识别当前 DB 时保持隔离，评审 forward fix/完整恢复，不 stamp 或删审计。API/Web、后台、普通入口分别放行，无自动回滚。
