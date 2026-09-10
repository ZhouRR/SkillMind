# 备份、恢复与版本回退

命令在目标部署目录 `PJM/` 执行，需确认环境、受控资产位置与获批维护窗口。首次起动见[Quickstart](quickstart.md)，发布顺序见[迁移手册](deployment.md)。目标或材料不明时停止。

## 配备前备份

先阻止新业务/触发、结算在途 Effect；结果未知按[原执行身份对账](runbook.md#incident-与-recovery)。确认所有 API/Worker 写入者停止后，在同一停写窗口取得恢复点。dispatch=false、仅停止一个实例或相同文件时间戳都不足以证明一致。

### 一致恢复点包含什么

| 资产 | 必须保留的关联 |
| --- | --- |
| DB dump | 数据库身份、migration revision、Run/快照/Outbox/批准/安全审计、上传意图/占用/清理要求、附件原字节/回执、评价原请求绑定与停写时点 |
| blob snapshot | SkillSource、文档等原对象 key、字节/hash及 DB 引用；新文档的 namespace UUID/连接描述摘要与实际存储世代须一起保留 |
| Run 文件 | 必要 workspace/transcript、输入副本、Session 恢复材料；缺文件不能标为可续行 |
| 镜像与配置 | Backend/Web 与依赖 image ID、context path、配置版本 |
| KEK | 所需旧版本的独立受控保管引用，不与 dump 放一起 |
| 外部对账事实 | 原 Proposal/Effect、目标 revision、apply/read-back 及恢复点后的变化 |

dump、blob 和 workspace 可含密码 hash、密文或业务正文，须访问控制，不进入公开附件。清单只记录恢复点 ID、UTC 窗口、资产引用/校验值、操作者、结果和缺项，不记录 Secret。RPO/RTO 与保留期由负责人确认，当前没有自动跨存储备份或恢复时效保证。

Redis 不代替 DB；旧队列、Outbox 重投与调度在途需要专用恢复方案，不对未知 Redis 全库清空。文档元数据总量也不等于 bucket 实际占用，不从列表推导可清除的对象；见[文档生命周期](../design/document-lifecycle.md)。

可信附件原字节保存在 Evidence 的私有列，随同原 Tool/Run 恢复；不能只导出公开元数据，也不能用恢复后的 output 文件替代原字节。旧无绑定行保持未发布，下载和结果核验共用原 size/hash；具体边界见[附件发布](../design/results-evaluation.md#可信附件的发布与读取)。

评价需连同原 Result、用户归属和 0041 submission_key/request_hash 恢复；只导出显示字段会丢失原提交确认能力。恢复点后新增评价可能不在旧 dump 中，原键 GET 未见不能证明它从未提交，不以换键重发或合并相似评价补造历史。

预算账户、预留、原调用绑定、0043 启动所有权 hash、观察与核对回执须一起恢复。原启动 token 只属于存活协调器，不从 dump 的 hash 重建或向新 Worker 发放。恢复较早的 RESERVED/无绑定行不能证明恢复点后未启动；先核对原执行，未知占用不释放，不靠换 invocation 或 owner 再跑。

存储 UUID 不是备份或服务端身份凭证。恢复到新 endpoint/bucket 或重新创建存储时，不能只改配置、复用旧 UUID 或改写文档摘要来让校验通过；须先核验原资产并完成受控归属迁移，当前尚无该工具。未绑定旧文档保留元数据并拒绝 blob 操作。0038 上传意图/原发布回执/占用、0039新旧文档清理记录、0042关闭标记及独立审计须随 dump 一起恢复；原 key 可查询，不据此自动重放 PENDING。关闭标记缺审计或反向不匹配不得修补放行；旧恢复点可能遗漏后来关闭，须对账后再开放发布。恢复点后的迟到 PUT 也须核对，关闭不证明远端停止。未来精确版本及清理凭证纳入同一恢复点，未对账前不清理或释放占用。

### 取得并检查数据库备份

以下只备份 DB，不替代 blob/workspace snapshot。两块在同一 shell 执行，mktemp 创建失败或 pg_dump 非零即停；新终端必须重新明确本次目录，不猜测变量。密码不写命令行。

```bash
umask 077
PJM_BACKUP_DIR="$(mktemp -d ./projectmind-backup-XXXXXXXX)" &&
python3 scripts/compose.py -- exec -T postgres sh -ceu \
  'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
  > "${PJM_BACKUP_DIR:?Backup directory is required}/database.dump"
```

成功后检查非空、archive 目录及字节校验值。失败文件保留为现场，不标作有效备份。

```bash
(
  set -eu
  : "${PJM_BACKUP_DIR:?Specify the directory created for this backup}"
  test -s "$PJM_BACKUP_DIR/database.dump"
  python3 scripts/compose.py -- exec -T postgres pg_restore --list \
    < "$PJM_BACKUP_DIR/database.dump" > "$PJM_BACKUP_DIR/database.toc"
  docker image inspect projectmind/backend:0.1.0 projectmind/web:0.1.0 \
    --format '{{.RepoTags}} {{.Id}}' > "$PJM_BACKUP_DIR/images.txt"
  cd "$PJM_BACKUP_DIR"
  sha256sum database.dump > database.dump.sha256
  sha256sum --check database.dump.sha256
)
```

子 shell 遇错即停。TOC 可能含业务对象名，仅受控保存。将全部资产纳入同一清单并转存；checksum 检查不是来源真实性或实际恢复演练。完整恢复点就绪前不删除旧镜像、不迁移，更不运行删除全 volume 的 `make bootstrap-admin`。

## 数据库恢复

**以下会删除并替换数据库，丢失恢复点后的本地数据。** 经负责人确认并保全当前现场后，先在隔离环境演练。

恢复旧 DB 不撤销 Git/SVN/Redmine 等远端变更，还可能恢复已撤销 session/权限。例如 10:00 备份、10:05 撤权、10:07 外部 commit，恢复 10:00 后两项后续事实都须独立核对。未核清前保持隔离；没有通用权限修复/Effect 补账 CLI，不靠 SQL、bootstrap 或新幂等键补造结果。

### 恢复前的停止条件

以下全部满足才继续：环境/Compose project/DB 名与角色/目标镜像/revision 已明确；可信 dump 的 checksum/archive 和关联 blob/workspace/config/KEK 齐全；当前现场另有备份；所有写入者已停；恢复点后的安全/外部变更有原身份对账方案。

将选定完整备份目录明确设为 `PJM_RESTORE_DIR`，保留原文件名及相对路径。以下只校验材料：

```bash
(
  set -eu
  : "${PJM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$PJM_RESTORE_DIR/database.dump"
  python3 scripts/compose.py -- exec -T postgres pg_restore --list \
    < "$PJM_RESTORE_DIR/database.dump" > /dev/null
  cd "$PJM_RESTORE_DIR"
  sha256sum --check database.dump.sha256
)
```

成功后在已全局停写的窗口停止本目录服务，并在受控终端核对实际 DB/角色：

```bash
python3 scripts/compose.py -- stop api worker migrate
python3 scripts/compose.py -- exec -T postgres sh -ceu '
  psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc \
    --set=ON_ERROR_STOP=1 --command="SELECT current_database(), current_user"
'
```

### 替换数据库

仅在上述结果与已批准目标完全相符时执行。例子以原应用角色恢复；多 owner/自定义权限环境须另有验证过的方案。

```bash
(
  set -eu
  : "${PJM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$PJM_RESTORE_DIR/database.dump"
  python3 scripts/compose.py -- exec -T postgres sh -ceu '
    dropdb --force --username="$POSTGRES_USER" "$POSTGRES_DB"
    createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" "$POSTGRES_DB"
  '
  python3 scripts/compose.py -- exec -T postgres sh -ceu '
    pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
      --no-owner --single-transaction --exit-on-error
  ' < "$PJM_RESTORE_DIR/database.dump"
)
```

单事务/遇错退出只保护 pg_restore 导入，不撤销 dropdb/createdb、blob 或外部效果；失败可能留下空库，须保持业务关闭。大型 DB 若需其他策略应先演练，不临场删保护参数。参考 [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)。

### 恢复后验证

恢复对应 blob/workspace、兼容镜像/配置及旧 KEK。核对 revision 后按[迁移审查](deployment.md#迁移与回退审查)决定是否 forward migration，Worker 继续隔离，不用普通 Compose up 绕过分阶段检查。

放行前分别确认：用户/成员/Session 没有重新开放旧权限；Skill/文档/Artifact 字节与 Run/Result/Evidence 引用一致；终态可读与非终态续行分别成立；缺回执/输入/transcript 时拒绝而非补签；旧队列、lease、Schedule 与远端 Effect 已对账。记录实际耗时、数据损失、验收范围、残余问题和放行人。

任一未知保持隔离，preflight 不替代这些证据。后台与普通入口按[分阶段放行](deployment.md#启动与放行)恢复。

## 应用版本回退

旧 API/Web/Worker 能理解当前 schema、数据、队列和非终态快照时，才可保持数据切换兼容旧镜像。兼容不明时不启动旧 Worker，评估完整恢复点和外部对账；无可信恢复点则保全现场、选 forward fix，不试跑破坏性 downgrade。

回退仍按[发布阶段](deployment.md#迁移前置与执行)重新核对清单：旧 archive/checksum 与旧 Backend/Web image ID、当前 schema、配置和 daemon/project 都须明确。deploy-load 保留本地旧镜像，但这不保证完整旧 archive、第三方镜像或数据恢复点可用；同名 tag 不算回退方案。旧镜像的 migration-plan/head 检查不能理解当前 DB 时保持隔离，选择已评审的 forward fix 或完整恢复，不跳过门禁、stamp 或试删审计。API/Web、后台和普通业务分别放行，命令不会自动回滚。
