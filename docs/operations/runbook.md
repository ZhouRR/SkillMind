# ProjectMind 部署与恢复 Runbook

本书负责“操作前确认什么、按什么顺序处理、何时必须停止”。首次启动见[Quickstart](quickstart.md)，运行规则见[Runtime](../design/agent-runtime.md)，身份与凭据边界见[认证](../design/authentication.md)。正文按中文操作说明整理，保留已有章节标题和编号以兼容旧链接。

> 文档中的目标不等于现成恢复工具。部署版本、工作副本与未完成事项分别在[计划](../planning/roadmap.md#13-当前执行状态)核对；本页不证明某个镜像已通过恢复演练。

## 按问题找入口

| 需要处理的情况 | 先做什么 | 手册位置 |
| --- | --- | --- |
| 准备更新镜像或迁移 | 确认维护窗口，保存同一恢复点的 DB/blob/workspace 与镜像 | [备份](#2-配備前-backup)、[配备](#3-migration-と配備) |
| 更换环境配置文件 | 区分 Compose 插值与容器配置，不能只改 ENV_FILE | [配置边界](#环境文件与配置边界) |
| 新环境没有管理员 | 使用普通 bootstrap CLI，不执行清空 volume 的初始化目标 | [首次 ADMIN](quickstart.md#最初の-admin-を作成する) |
| Run 卡在运行中或不断重试 | 先区分输入准备、模型等待、执行权失效，不改 SQL 状态 | [准备分诊](#准备故障的只读分诊)、[接管验证](#42-worker-喪失と接管)、[日志](#7-log-確認と-incident-記録) |
| 正在等待回答/批准 | 核对待办版本、身份与独立期限，不当成 Worker 卡死 | [WAITING](#81-waiting-状態) |
| 外部写入结果不明 | 先查原幂等身份的结果/read-back，保留已发生的变更 | [Effect 排障](#87-incident-と-recovery) |
| 输入文档缺失或内容不一致 | 保留 manifest/现场，确认运行版本与冻结清单 | [资源排障](#9-资源快照排障) |
| 定时任务没有生成 Run | 分开看 created/skipped/failed/missed，不手动补造触发 | [调度监视](#10-schedule-と-recovery-の監視) |
| 必须恢复旧版本或数据 | 先确认完整恢复点及新旧兼容性；这是有数据损失风险的操作 | [DB restore](#5-database-restore)、[版本回退](#6-application-version-rollback) |

以下命令不会因为写在手册里就获得执行授权。尤其 restore、镜像替换、停止 Worker 和 smoke 写入，应在已确认的专用环境或获批维护窗口执行。

## 1. 運用原則

- PostgreSQL 是业务与审计正本；Redis 队列或通知不替代 Run、Attempt、Event、Evidence、Result 和认证记录。
- 镜像替换与迁移前保留完整恢复点及实际 image ID；相同 tag 不证明 image 内容相同。
- 不用 `stamp` 绕过迁移失败，不手工删除审计行、改 Run 状态或改幂等键来消除错误。
- `.env`、密码、session/CSRF token、KEK、Provider 凭据及业务正文不进入普通备份清单、日志或截图。
- 本地恢复不回滚外部系统；有结果不明的 Effect 时先隔离自动执行并核对外部事实。

以下命令都在正式代码目录 `PJM/` 执行，默认只使用该环境自己的 `.env`。示例中的备份变量属于同一个 shell 会话；新开终端必须重新明确恢复点，不以空变量或默认值推测目标。

### 环境文件与配置边界

当前 [Makefile](../../PJM/Makefile) 的 `ENV_FILE` 只被用于 `docker compose --env-file …`，而 [compose.yaml](../../PJM/compose.yaml) 的 Backend 公共配置仍是固定的 `env_file: .env`。因此 `ENV_FILE=.env.production` **不会自动把 API、Worker、migrate 的配置来源一起切换**。

| 配置入口 | 当前作用 | 操作风险 |
| --- | --- | --- |
| Compose `--env-file` / Make `ENV_FILE` | 提供 `${…}` 插值，例如 host、DB 容器参数 | 不能替换 service 内显式声明的 env_file |
| Backend `env_file: .env` | 给 API/Worker/migrate 注入应用设置 | 可能与另一个插值文件指向不同 DB、Redis 或凭据 |
| service 的 `environment` | 显式设置 context path、TZ 等值 | 优先于同名 env_file 值；还应核对 shell 对插值的覆盖 |

这是根据项目配置和 [Compose 插值规则](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)、[service env_file 规则](https://docs.docker.com/reference/compose-file/services/#env_file)推导的混用风险，尚未在实际部署复现。现阶段使用独立部署目录及其经确认的 `.env`；不要在同一目录仅换 `ENV_FILE` 就操作另一环境，也不要把生产配置复制覆盖进开发工作区。

只做配置合法性检查时使用：

```bash
docker compose --env-file .env config --quiet
```

[`--quiet`](https://docs.docker.com/reference/cli/docker/compose/config/)不打印完整配置。普通 `config`、`config --environment`、容器 `env` 或完整 `docker inspect` 可能包含 Secret，不作为可公开保存的诊断输出。检查通过仅说明配置可解析，不证明连接到了预期环境。

后续配置修正应让 CLI 插值与三个 Backend service 使用同一个显式配置来源，并同步 Make、Compose、导出/部署脚本和指南。验收用两套无敏感值的配置，分别核对默认/自定义路径、缺失文件拒绝、shell 覆盖和各 service 的实际注入；仅静态 YAML 检查不够。本轮只修正文档，未修改部署实现。

## 2. 配備前 backup

先阻止新业务请求与调度触发，等待正在执行的外部 Effect 结算；结果不明时转到[Effect 恢复](#87-incident-と-recovery)。随后停止 API/Worker 等写入者，在停写窗口内取得 DB、blob 和必要 workspace 的同一恢复点。仅停止新 Run 创建、仅关闭业务 dispatch 开关或只给文件相同时间戳，都不能证明已无写入。

### 一致恢复点包含什么

| 保存对象 | 为什么需要 | 必须核对的关联 |
| --- | --- | --- |
| PostgreSQL dump | 业务事实、不可变快照、Outbox、批准与审计 | 数据库身份、migration revision、停写时点 |
| object-storage snapshot | SkillSource、Project 文档及其他 DB 引用的 blob | 原对象键、字节和 hash；不能只备份最终报告 |
| 必要 Run workspace / transcript | 输入副本、运行文件和会话恢复材料 | 对应 Run/Session；缺文件不能标为可续行 |
| image archive 与配置版本 | 用兼容代码解释恢复后的数据 | Backend/Web image ID、依赖 image、context path；配置凭据另行保护 |
| KEK 及版本 | 解封 MANAGED 凭据 | 由独立受控保管系统保留，不与 DB dump 放在一起 |
| 外部 Effect 对账记录 | 防止恢复旧数据库后重做已发生的写入 | 原 Proposal/Effect 身份、目标 revision 与 read-back 状态 |

Redis 不是上述数据的替代品。恢复时如何隔离旧队列、处理 Outbox 重投和调度在途，应纳入专用演练；不能对未知 Redis 使用全库清空命令，也不能假定恢复旧 DB 后现有队列自然一致。

备份清单只记录恢复点 ID、UTC 停写窗口、上述受控资产引用/校验值、操作者、验证结果和未覆盖项。配置与 KEK 的清单记录版本引用，不记录值。保留期限、RPO（最多允许丢失的数据时间）和 RTO（目标恢复耗时）由环境负责人明确；当前没有自动跨存储备份、统一保留清理或已验证的 RPO/RTO 保证。

### 取得并检查数据库备份

下面是经确认的停写窗口内的 DB 备份示例，不替代其他存储的 snapshot。`mktemp` 创建本次独立目录，避免覆盖同名备份；父目录和资产存放位置也应由操作者控制。密码不写入命令行。

```bash
umask 077
PJM_BACKUP_DIR="$(mktemp -d ./projectmind-backup-XXXXXXXX)"
docker compose --env-file .env exec -T postgres sh -ceu \
  'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
  > "$PJM_BACKUP_DIR/database.dump"
```

只有 `pg_dump` 退出码为 0 才继续。失败留下的文件保留为故障现场，不标记为有效备份。随后检查非空、记录并核对字节校验值，验证 archive 目录可解析；单凭这些检查仍不能代替实际恢复演练。

```bash
(
  set -eu
  : "${PJM_BACKUP_DIR:?Specify the directory created for this backup}"
  test -s "$PJM_BACKUP_DIR/database.dump"
  docker compose --env-file .env exec -T postgres pg_restore --list \
    < "$PJM_BACKUP_DIR/database.dump" > "$PJM_BACKUP_DIR/database.toc"
  docker image inspect projectmind/backend:0.1.0 projectmind/web:0.1.0 \
    --format '{{.RepoTags}} {{.Id}}' > "$PJM_BACKUP_DIR/images.txt"
  cd "$PJM_BACKUP_DIR"
  sha256sum database.dump > database.dump.sha256
  sha256sum --check database.dump.sha256
)
```

此检查块在子 shell 中遇错即停，不更改当前终端的工作目录。TOC 可能含业务对象名，只保存在受控目录。将 DB、blob/workspace snapshot、配置引用和镜像纳入同一备份清单并转存到批准的保管系统。checksum 文件使用目录内相对路径，便于整体转存；它用于发现字节变化，不是备份来源的真实性证明。

完整恢复点就绪前不删除旧镜像或启动 migration。`make bootstrap-admin` 会删除所有 volume，不属于备份、更新或常规管理员创建步骤。

## 3. Migration と配備

先确认完整恢复点、维护窗口与目标 archive，保留当前 image ID；API、Worker、migrate 与 Web 必须按经过验证的兼容组合发布，不能只替换 API。数据库兼容、公开契约兼容与非终态 Run 可续行是三个独立门禁，见[契约版本边界](../development/contract-workflow.md#历史数据兼容不等于前后端版本兼容)。

```text
确认环境与恢复点 → 停写并核对在途 Effect → 加载目标镜像
  → 检查当前 revision / 目标 head → 单独迁移
  → 基础检查与只读核对 → 专用场景验收 → 恢复业务入口与 Worker
```

以下检查会启动一次性 migrate 容器并连接数据库，应使用已经确认的目标镜像和配置；`alembic heads` 来自该镜像，并非此文档写死的版本号。

```bash
docker compose --env-file .env run --rm migrate alembic current
docker compose --env-file .env run --rm migrate alembic heads
```

当前 revision 不在目标迁移链中、出现多 head 或迁移失败时停止操作。确认后单独迁移；命令成功前不启动应用：

```bash
docker compose --env-file .env run --rm migrate
```

迁移失败后记录失败 revision 和脱敏错误，用 `alembic current` 确认事务结果；不手动更新 `alembic_version`。迁移链已提交的较早 revision、非事务操作或外部数据不会因为后一项失败就全部撤销。决定使用修正版 forward migration 还是完整恢复点后，再继续。

### 迁移与回退审查

此表是查阅入口，不是支持随意跨版本 downgrade 的承诺。实际 revision 图与函数以[迁移目录](../../PJM/backend/migrations/versions/)为准；每次发布重新审查 upgrade、downgrade 和新数据的消费者。

| 迁移 | 要保护的事实 | 回退前必须知道的限制 |
| --- | --- | --- |
| 0018 Skill library scope | Organization 共享来源、Project 精确版本启用 | 多项目共享不能一一还原成旧 project_id；遇到组织/引用冲突不能删行凑齐 |
| 0019 OutcomeEnvelope | 原 Result 不改写，新旧结果格式并存 | 新包络已写入时，不能仅靠旧 image 读取兼容假设回退 |
| 0020 interactive Run / 0021 controlled effects | Segment、Interaction、lineage、binding、Proposal 与 Effect 审计 | 旧 Worker 不理解新续行与 effect Outbox；不可混跑或把批准改成普通重试 |
| 0022 UI language / 0023 MANAGED Secret | 用户偏好与加密凭据材料 | 降级会丢列/表；有密文时必须连同解封所需 KEK 评估 |
| 0024 remove bootstrap seed | 保留被真实 Run/Proposal/composition 引用的历史版本 | downgrade 不恢复被退役的 seed，不能当作可逆导入 |
| 0025 schedules / 0026 frontend module versions | 调度配置/计数与生成版本记录 | drop table 会丢失配置或审计；有表不等于生成模块已能构建和投放 |
| 0027 subagent sessions | 子会话与仅针对 PRIMARY 的唯一约束 | [downgrade](../../PJM/backend/migrations/versions/0027_subagent_sessions.py)包含删除无 SDK ID 会话，再恢复全会话唯一约束；不在有子会话数据的环境试跑 |
| 0028 source file index | Skill 来源文件索引与来源审计 | 回退会丢索引列，不能据此重算并改写已发布版本身份 |
| 0029 input snapshots（工作副本） | 独立 Run 输入准备回执 | downgrade 在存在任何回执行时拒绝删表，不仅检查 READY；不能先删审计行绕过。完整迁移/恢复尚待验收，不凭文件存在升级生产 |

有新状态/数据格式而旧代码不理解时，保持停写，按[完整恢复](#5-database-restore)和[版本回退](#6-application-version-rollback)处理。历史非终态 Run 尤其要逐类验证；“历史页面能打开”不能证明新 Worker 能安全继续它。

### 启动与放行

维护期间先保持 Worker 停止、业务入口关闭，只启动 API/Web 及其依赖进行核对：

```bash
docker compose --env-file .env up -d --no-build api web
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
make
```

`preflight` 只查 PostgreSQL 连通/迁移 head 和 Redis PING，详见[实现](../../PJM/backend/src/projectmind/ops/preflight.py)；不检查 blob、KEK、认证、模型、外部写入或 Worker 恢复。先确认抽样 Run/Result/Evidence 与 blob 可读，历史输入缺失被明确报告，再在隔离的专用项目执行 [smoke](#41-通常-smoke) 与所需恢复场景。启动 Worker 会同时开放该实例的后台任务，不只运行测试 Run；有未对账 Effect、旧 Run 或 Schedule 时不能直接启动。

检查全部通过且允许恢复该环境业务后，才启动 Worker、开放入口。`make deploy` 会直接重启整套服务，不实现上述逐项人工门禁；需要分阶段放行的恢复现场不要直接使用它。

## 4. Generic task acceptance と障害接管

### 4.1 通常 smoke

smoke 会调用模型、创建 Run/Evaluation 并测试取消，属于有费用和审计写入的验收，不是只读健康检查。使用已批准的测试环境和专用 Project；显式指定其中的 published task，不使用业务默认项目或已退役的 bootstrap seed。先按上一节完成 Worker 放行检查，再启用 dispatch 和必要模型凭据。

将测试项目 UUID 明确设为 `SMOKE_PROJECT_ID` 后执行。未设置时 CLI 拒绝运行，不自动找一个项目代替。

```bash
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
docker compose --env-file .env exec \
  -e PROJECTMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" \
  api python -m projectmind.ops.smoke
```

密码通过 TTY 输入。测试范围是登录/会话 CSRF、同键创建重放、任务终态/结果/证据/版本校验、追加 Evaluation 后 Result 不变、终态事件、SSE 断线重放和取消审计；通过不代表所有 Provider、页面或模型业务质量验收完成。

可用 `PROJECTMIND_SMOKE_SKILL_VERSION_ID` / `PROJECTMIND_SMOKE_TASK_KEY` 固定任务，并用 `PROJECTMIND_SMOKE_TASK_INPUT_JSON` / `PROJECTMIND_SMOKE_TASK_SOURCES_JSON` 提供符合契约的输入。不要把真实票据正文或凭据放进示例、共享 shell 历史或公开报告。

### 4.2 Worker 喪失と接管

仅在可停机的专用环境做故障注入。先确认测试 Run 已进入模型执行，记录 Run/Segment/Attempt ID、输入快照和当前 lease；仅看到 `RUNNING` 不足以证明物化已经完成。然后停止该环境 Worker：

```bash
LEASE_SECONDS="$(docker compose --env-file .env exec -T api python -c \
  'from projectmind.core.settings import get_settings; print(get_settings().run_lease_seconds)')"
docker compose --env-file .env stop worker
```

等待配置的 lease 时长和 recovery cron 的观察窗口后，再启动 Worker。以下 20 秒是检查余量，不保证服务在该时间内恢复；重启后仍须等待一次实际 recovery tick 并核对持久记录。

```bash
sleep "$((LEASE_SECONDS + 20))"
docker compose --env-file .env start worker
docker compose --env-file .env logs --no-log-prefix --tail=200 worker
```

成功条件是旧 Attempt 记录 `LEASE_EXPIRED`，Run 经 `RETRY_PENDING`，在同一 Segment 追加新 Attempt 后继续；输入、任务、权限和 SkillVersion 快照不变。次数超限则以 `retry_exhausted` 结束为 FAILED，不无限追加 Attempt。

当前工作副本已把 heartbeat/取消监督移到准备之前，并在 Brief 冻结和模型启动前重验执行权；部署中的旧镜像未必具有这些行为。核对[实际调用顺序](../design/agent-runtime.md#74-从领取到模型启动的边界)，分别演练“准备中失去 Worker”和“模型执行中失去 Worker”，不能用后者的成功排除前者风险。若原世代停在 PREPARING，新的有效 Attempt 也不能直接把它补签为 READY，须按[输入中断规则](../design/resource-snapshots.md#准备中断与再次使用)处置。

### 4.3 回帰 matrix

| 场景 | 检查入口 | 必须观察的结果 |
| --- | --- | --- |
| 输出不符合 Schema | `test_invalid_result_is_finalized_as_failed` | FAILED / `result_schema_invalid`，不把原值写入错误日志 |
| 未注册 Tool 或越界调用 | `test_pre_tool_denial_is_audited_without_argument_values` | 不调用 Provider，保留拒绝审计，不复制业务参数 |
| lease 失效后接管 | `test_worker_loss_recovery_creates_second_attempt_without_snapshot_drift` | 追加 Attempt，快照不变 |
| SSE 断线重连 | smoke | 只重放大于 Last-Event-ID 的持久事件 |
| 同一创建请求重发 | smoke | 原 Run ID、`idempotent_replay=true` |

这些是回归入口，不是本环境已通过的证据。真实 DB 锁竞争、准备期崩溃、跨存储恢复和外部副作用不由纯 fake 测试覆盖。

## 5. Database restore

这是删除并替换指定数据库的破坏性恢复，会丢失恢复点之后的本地数据。先获得环境负责人确认并保全当前现场；通常先在隔离环境演练，不直接试生产。恢复旧数据库不会撤销已经写到 Redmine、Git/SVN 或 forge 的内容。

### 恢复前的停止条件

以下任一项不满足，就不执行后面的数据库替换：

- 环境、Compose project、DB 名称/角色、目标镜像和预期 revision 都已明确，且不是凭目录名推断。
- dump 来源可信、checksum 与 archive 检查成功，对应 blob/workspace、配置版本及必要 KEK 齐全。
- 当前现场另有可恢复备份；维护窗口覆盖 API/Worker、调度和外部 Effect，新写入已停止。
- 已记录恢复点以后可能发生的外部变更，并有原幂等身份的对账方案。

把本次选定的完整备份目录设置为 `PJM_RESTORE_DIR`。只读校验块不会修改数据库；需要整体保留第 2 节生成的文件名和相对路径。

```bash
(
  set -eu
  : "${PJM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$PJM_RESTORE_DIR/database.dump"
  docker compose --env-file .env exec -T postgres pg_restore --list \
    < "$PJM_RESTORE_DIR/database.dump" > /dev/null
  cd "$PJM_RESTORE_DIR"
  sha256sum --check database.dump.sha256
)
```

校验通过后停写，并在受控终端核对当前连接的数据库与角色；不要把完整连接设置贴到公共报告：

```bash
docker compose --env-file .env stop api worker migrate
docker compose --env-file .env exec -T postgres sh -ceu '
  psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc \
    --set=ON_ERROR_STOP=1 --command="SELECT current_database(), current_user"
'
```

### 替换数据库

只有操作者确认上述结果与目标完全一致后，才执行此块。例子以原应用角色恢复对象；自定义多 owner/额外权限的环境需先制定对应恢复方案。子 shell 遇错即停，失败后保持业务关闭。

```bash
(
  set -eu
  : "${PJM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$PJM_RESTORE_DIR/database.dump"
  docker compose --env-file .env exec -T postgres sh -ceu '
    dropdb --force --username="$POSTGRES_USER" "$POSTGRES_DB"
    createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" "$POSTGRES_DB"
  '
  docker compose --env-file .env exec -T postgres sh -ceu '
    pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
      --no-owner --single-transaction --exit-on-error
  ' < "$PJM_RESTORE_DIR/database.dump"
)
```

[PostgreSQL 17 的 pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html) 默认遇到 SQL 错误会继续处理；这里指定单事务与遇错退出，避免把部分导入当成成功。单事务只覆盖导入，不撤销此前的 dropdb/createdb，更不回滚 blob 或外部系统；失败时可能只剩空数据库。大型数据库若需其他导入策略，先在恢复演练中验证，不临场删掉错误保护参数。

### 恢复后验证

恢复与 dump 对应的 object storage/workspace，加载兼容 image 和配置，让所需旧 KEK 可解封。先核对 revision，再依第 3 节决定是否 forward migration；保持 Worker 与外部写入隔离，不能直接运行 `make deploy` 自动放开后台任务。

| 验证层 | 放行前应取得的证据 |
| --- | --- |
| 基础与权限 | 目标 migration head、DB/Redis 连接；恢复的用户/成员/Session 状态已经核对，不把过期权限重新开放 |
| 数据关联 | 抽样 Skill/文档/Artifact blob 可读且内容匹配；Run/Result/Evidence 引用一致 |
| 可续行性 | 终态历史可读与非终态可继续分别验证；缺快照/回执/transcript 时明确拒绝，不补签或重新授权 |
| 外部与在途工作 | 对账恢复点后的 Effect；确认 Outbox/队列、Schedule 在途和旧 lease 的处置，不重发重复写入 |
| 专项与交接 | 专用环境 smoke/恢复场景、实际恢复耗时、数据损失范围、残余问题和放行责任人 |

任一不明项保持隔离，不能以 `preflight=ready` 代替业务放行。当前无统一恢复编排 CLI；上述关联验证和外部对账仍需经过授权的运维流程完成。

## 6. Application version rollback

先选择回退路径，不把 image 回退和数据恢复混成同一条命令：

| 已确认的条件 | 可采用的路径 |
| --- | --- |
| 旧 API/Web/Worker 理解当前 schema、数据、队列和非终态快照 | 保持数据，按维护流程切换到兼容旧镜像并重新验证 |
| 旧代码不理解新数据，或兼容性未确认 | 不启动旧 Worker；评估完整恢复点和外部对账，经确认后按第 5 节恢复 |
| 没有可验证的完整恢复点 | 停止回退；保全现场并选择 forward 修复，不试跑破坏性 downgrade |

只有第一行且允许整体重启时，可指定已经保留并验证的旧 archive：

```bash
make deploy IMAGE_ARCHIVE=images/projectmind-previous.tar
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
docker compose --env-file .env exec -e PROJECTMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" api python -m projectmind.ops.smoke
```

`make deploy` 会先移除容器和旧应用 image，再 load archive 并重启；如果 archive 缺失必要 image，服务不会自动回到原版本。必须事先保管可用的前后两代 archive/image ID，不把保留同名 tag 当作回退方案。

## 7. Log 確認と incident 記録

ProjectMind 使用单行 JSON 日志。先按 API 的 trace/request ID 或 Worker 的 Run/Attempt ID 缩小范围，再关联 Segment/Session 与外部 Effect。以下只在受控终端查看：

```bash
docker compose --env-file .env logs --no-log-prefix api worker \
  | jq -c 'select(.run_id == "<RUN_ID>" or .trace_id == "<TRACE_ID>")'
```

对外交接保留 UTC 时点、image ID、migration revision、脱敏对象 ID、公开错误码和已完成的检查。不要转录密码、token、凭据、票据正文、Tool 原始参数/结果或 Evidence 摘录。普通 incident 不直接附 `.env`、完整容器配置或整个 workspace。

## 8. 対話型 Run と外部 Effect の運用契約

本节是排障索引，状态与批准规则以[Runtime](../design/agent-runtime.md)和[受控写入](../design/repository-effects.md)为准。已注册的写入边界包括 Redmine `issue.update/v1` CAS adapter 和 Git/SVN `repository.write/v1`；不通过普通 Redmine 更新接口或任意命令绕过原子前置条件、幂等与 read-back。repository 始终需要人工批准。

### 8.1 WAITING 状態

- `WAITING_FOR_INPUT` / `WAITING_FOR_APPROVAL` 不是终态，也不是需要强制重启 Worker 的故障。人工等待应释放主执行 lease，不继续占用模型进程；Interaction 的期限与 active wall timeout 分开。批准后的 Effect 可持有独立 lease，Run 仍可能显示 WAITING_FOR_APPROVAL，须查 Effect 状态而不是再次批准。
- 核对持久的 Interaction、Brief/checkpoint、Session 和事件。接收有效回答后追加 Segment；技术故障重试才在原 Segment 追加 Attempt。
- 普通 Interaction 超期记录 `INTERACTION_EXPIRED`，通过 `INTERACTION_TIMEOUT` Segment 继续，不代选推荐答案。Effect 批准超期则使 Proposal 失效，不调用写入 Provider。
- 取消、成员资格失效或 scope 改变不能靠复用旧答案/批准继续执行。等待期限不等于数据保留期限；自动清理仍属独立设计。

排障关联 Run/Segment/Attempt/Session/Interaction ID。PRIMARY 同时最多一个 ACTIVE；符合[子分析设计](../design/subagents.md)的 SUBAGENT/BRANCH 可以并行，不能只凭 ACTIVE 总数判定重复执行。

### 8.2 Session resume / fork / replace

| 续行方式 | 审查条件 |
| --- | --- |
| resume | transcript/workspace 完整，engine/model 兼容 |
| fork | 明确的比较分支语义，保留 parent Session 与 checkpoint checksum |
| replace | 无法复用原会话时从已审计 checkpoint 建新会话，不静默从头执行 |

这是执行设计的区别，不代表运维有任意切换模式的 CLI。缺少可信恢复材料时明确拒绝；任何模式都不能改变 Run 的版本、Project、权限、资源范围或预算上限。需要更换这些条件时创建新 Run。

### 8.3 ChangeProposal と承認

按“观察 → 提案 → 批准 → 执行 → 回读”核对事实。提案不表示已经写入，批准也不表示执行成功；原提案 checksum、目标 revision、批准人/期限和实际 Effect 状态必须对应。

有效批准由 Run 发起者或 system ADMIN 提供；普通项目成员身份不自动授予批准权。低风险预授权还需满足能力、Integration、operation、risk、scope 和期限；repository 不在预授权范围内。写入已成功但 verification 失败必须保留部分成功事实，不能改 SQL 状态或以新键再做一次。

### 8.4 Redmine CAS adapter と Secret

Integration 配置连接位置，Project 的 SecretReference 指向 `ENVIRONMENT` / `FILE` / `MANAGED` 凭据。公开 API/Web 不返回 locator、配置正文或 Secret 值。更换凭据引用/Integration 配置版本不能改写原 Run binding；仅重封 MANAGED 密文则按 §8.8 处理。

Worker 写入前检查 `${base_url}/.well-known/projectmind-effect-provider.json` 的 protocol/provider、`issue.update/v1`、`atomic_precondition=revision` 和 `idempotency=key`。不符合协议就拒绝，不回退到普通 Redmine 更新接口。符合时才执行 pre-read、带精确 revision/原键的 apply 和 read-back；维护重试不手工新建 EffectExecution。

### 8.5 Repository Integration と資源快照物化

先查 [repository source/client](../../PJM/backend/src/projectmind/agent/repository_source.py) 和[资源设计](../design/resource-snapshots.md)，不从 `HEAD` 或目录名推断内容版本。

| 检查项 | 当前边界 |
| --- | --- |
| 连接方式 | Git 接受 http/https/file；SVN 另接受 svn；不接受 SSH 或 URI 内嵌密码 |
| 凭据 | Secret 使用 `username:secret`，无冒号时以 x-access-token 为用户名；只在第一个冒号分割。实际用户名要求由目标服务确认 |
| 凭据传递 | Git 通过环境注入认证 header，SVN 经 stdin 传密码；不写 argv。临时 checkout 不在 Agent workspace 内，但不据此宣称 host 管理者无法取得凭据 |
| scope / revision | paths 是硬边界；按冻结 allowlist/default_revision 选择，再记录实际 commit/SVN revision，歧义拒绝 |
| 运行依赖 | Backend image 包含 git/subversion；命令上限由 PROJECTMIND_REPOSITORY_COMMAND_TIMEOUT_SECONDS 控制 |
| 准备结果 | 完成 manifest 记录 provider、实际 revision、binding checksum、scope、统计与 skipped；准备失败不保证存在完整 manifest |

普通不可读文件可记 skipped；冻结来源/hash 不一致属于准备失败，不以 skipped 掩盖。Provider 原始 stderr 不进入 Agent/Evidence；必要时由有权限的操作者在目标系统查脱敏故障，不复制内部地址或凭据。

### 8.6 Repository への書き込みと PR

Integration 必须声明 `repository.write/v1` 并提供写入凭据；匿名读取成功不意味着可以 push。人工批准和范围校验仍是前提。

| write_mode | 落点与排障要点 |
| --- | --- |
| direct（默认） | 批准后写默认 branch/绑定 URL；Git 使用 fast-forward、不 force。SVN 当前 checkout 未固定批准 revision，不能保证拒绝之前已变化的基线 |
| branch | projectmind/ 预留命名空间；同名不同内容报 target_branch_conflict。内容相同也不足以证明它属于本次提案，须查原身份 |

Git 写入的 default_revision 必须是具体 branch，不能用 HEAD。需要分支评审或默认分支受保护时，显式配置 branch 模式；缺少 write_mode 的旧 Integration 会采用 direct，不会自动猜测保护策略。SVN 分支位于平台约定的 `<仓库根>/branches/projectmind/`。

自动开 PR/MR 当前只接在 Git branch 模式，需要完整的 `forge_kind / forge_api_base_url / forge_project` 配置；部分配置拒绝，未配置则只保留 branch/commit。SVN 与 direct 不调用 forge。PR 不是 apply 前批准的替代。`target_stale` 需要重新观察和提案；PR 开设失败不撤销已提交且回读的 commit，按部分成功对账。

### 8.7 Incident と recovery

记录关联对象 ID、image/migration、Provider/capability 版本、Proposal checksum、请求指纹、幂等键 hash、批准人/时间及 apply/read-back 状态，不记录 Secret 或原始业务参数。

先区分审批决定是否保存、远端是否写入、回读是否完成、PR 是否创建、平台 finalize 是否完成；[四类事实](../design/repository-effects.md#先分清四种事实)不能用一个 FAILED 代替。当前可重试异常会重新运行整个 Provider，没有“仅补 PR”的通用操作；Effect lease 过期也不证明旧远端请求停止。[阶段与执行权差距](../design/repository-effects.md#可靠性修正要求)未补齐前，结果未知应隔离自动执行并按原身份对账，不能仅重启 Worker 促使恢复。

| 能确认的外部事实 | 处理原则 |
| --- | --- |
| 原幂等身份已应用，回读一致 | 依原身份恢复已有结果，不再写一次 |
| 明确未应用 | 仍要重验批准、scope、期限和前置 revision，只沿原受控恢复路径处理 |
| 应用状态未知，或部分成功/验证失败 | 保持隔离并对账；不换键、强推、改 SQL 状态或盲目重试 |

这里的“查询/恢复”是 Provider 协议与运维处置原则，不是已经提供的通用补账 CLI。恢复旧 DB 后尤其要先核对远端事实；不能因为本地 Effect 尚未完成就认定外部未执行。人工修正外部状态时追加受控 incident 记录与适用的 Evaluation，不覆盖原 EffectExecution/Result。

### 8.8 MANAGED Secret の KEK 運用

MANAGED 保存 AES-256-GCM 封装后的密文；实现边界见[认证设计](../design/authentication.md#72-平台托管managed应用层信封加密)。KEK 丢失会使对应凭据无法恢复，DB 备份本身不能补救。

keyring 使用逗号分隔的 `version:base64key`，每个 key 为 32 字节随机值；首项用于新加密，后续旧版本用于解密。通过环境的 Secret 管理流程生成和注入，不将实际密钥输出到录屏、聊天或公共命令示例。

轮换步骤：先分别备份密文与所需旧 KEK；向相关服务提供“新 active + 旧 key”的完整 keyring；在受控窗口执行 `python -m projectmind.ops.rotate_secrets`；检查退出状态/数量并验证解封。此 CLI 会改写密文，不是只读检查，也不改变 SecretReference/Integration/Run 身份。

现用 DB 全部重封后仍不能直接销毁旧 KEK：只要受保留的旧备份需要它，就继续在独立保护位置保存，并把版本引用纳入恢复方案。KEK 缺失、版本不明或密文校验失败时拒绝解密，不降级明文存储。MANAGED 不防护同时取得 host/process 与 DB 管理权的攻击者；外部 KMS/HSM 不属于当前实现。

## 9. 资源快照排障

先记录 image/代码版本、Project/Run/Segment/Attempt ID 与公开错误，区分部署中的旧 document 全集路径和工作副本已接入的清单冻结路径。[资源设计](../design/resource-snapshots.md)是范围、冻结时点和失败策略的正本。

- 选择范围有疑问时核对 Run sources、manifest 的来源与 skipped，不以当前 Project 文档列表代替历史输入。
- 缺失原 ID、hash 不一致或副本混入文件时保留现场；不要重新上传同名文件、删 manifest 或改 snapshot 来让原 Run“恢复成功”。
- `HEAD`/分支名和实际 commit 是两个值。对比输入与 live Evidence 时查看各自的具体 revision，不只比较分支名。
- 同一 Run 的合法只读副本可按校验规则复用，但 hash 存在不代表所有权、来源身份和额外文件检查已经通过。
- 必须换资源或重建可信输入时创建新 Run，旧 Run/Result 保持可读。取消旧非终态 Run 仍通过正常取消流程。
- 对外报告只记录脱敏 ID/hash 和失败类型；不把 manifest 中的业务文件名、正文或内部路径直接复制到公共日志。

显式选择、API 投影、Web 清单与对应局部回归已有记录；不能继续把整条公开链路写成未联调。但这不证明资源隔离已经部署，也不替代真实事务、跨根总量和缓存真实性验收。

创建响应丢失时先区分“原请求已提交但未确认”与“已有 Run 准备失败”。前者保留原内容/键，按[创建重放](../design/run-creation.md#4-响应界面与历史兼容)确认；后者检查该 Run 的冻结输入与错误。不要以新键重发、删除缓存或改 snapshot 同时尝试恢复两种不同问题。

工作副本已有输入回执、物化器/ContextBuilder、Worker/Tool 接线及准备期监督；消费者联调与整条准备链验收仍未完成。当前通过和失败项见[计划](../planning/roadmap.md#13-当前执行状态)，不能据局部测试通过升级生产。`input/` 的逻辑路径不等于旧同名物理目录，恢复须核对 Run 回执所指的世代。

没有补签/修复 CLI，也没有完整运行链验收。按[中断与再次使用](../design/resource-snapshots.md#准备中断与再次使用)区分缺回执、PREPARING、READY、提交结果未知和文件不匹配；目录看似完整或 migration 成功都不成为放行依据。现场不能由运维手工填成 READY、把更晚文件移入旧世代或删除后让原 Run 自动重建。

### 准备故障的只读分诊

先在已有授权范围内查 Run/Attempt 记录、脱敏日志和对应输入回执；不要为定位问题启动模型或重做物化。内部回执没有公开管理 API，以下是查阅顺序，不是新的运维 CLI。

| 观察 | 下一步核对 |
| --- | --- |
| 已进入运行态，但没有会话 | `RUNNING` 与 `run.execution.prepared` 在 ContextBuilder 前产生；依次查准备错误、输入回执、Brief 与 Worker，不当作模型启动证据 |
| 准备超时 | `preparation_timeout`：核对准备配置、各资源耗时与回执提交结果；模型 wall timeout 不是该配置的替代，不直接提高所有上限 |
| 准备失败 | `context_build_failed`：查脱敏异常类型、冻结来源、物化消费者与 binding；Provider timeout、调用参数缺失也可能归到此处，不一律归因于网络 |
| 执行权异常或不断接管 | 查 lease 到期、token 不匹配与 Run/Segment/Attempt 时间线；旧 Worker 不应另写终态，当前状态交给合法恢复流程判断 |
| 已接受取消，但还没有终态 | 请求接受不等于已停止；查持久取消意图和 Session 事件。首事件前的模型中断仍有待验窗口，不能以按钮响应证明进程已退出 |
| 输入已就绪，但执行未成功 | 回执 READY 与 Run 失败/未启动可以同时成立；Brief/启动校验、完整性或模型执行可能失败，不删除已完成回执来“解锁” |

所用计时器及默认/可配范围集中见[执行限额](../design/run-budgets.md#现有计时器的覆盖范围)。只记录经过允许的配置项、对象 ID、时间和错误类型，不导出整个环境、Brief、manifest 或业务输入。若完成提交的结果未知，先保持输入不可用并保留现场；当前没有自动对账工具，不以另一个 Run/世代覆盖原记录。

## 10. Schedule と Recovery の監視

Schedule tick 的 `schedule.tick.completed` 分别记录 `created / skipped / failed`，不要合成一个“成功率”：重叠而跳过与权限/版本/资源失效是不同原因。FAILED_PRECONDITION 结果回写成功后 Schedule 进入 ERROR；数据库等基础设施异常可能中断 tick，不保证留下这个状态。不要因此自动换版本、来源或补造触发；通过正常配置/恢复入口修正，不能改数据库的计数或 next_run_at。

当前可能处理一个已经迟到的 occurrence，然后跳过后续过期时刻；`missed_count` 单次最多计 1000，并非永远精确的漏跑数。例如整点规则的 03:00 未处理，到 09:30 恢复时可能处理 03:00、跳过 04:00–09:00、下一次为 10:00。不要把“不补跑”理解成完全拒绝迟到执行，详见[调度恢复设计](../design/task-scheduling.md#停机恢复的实际行为)。

| 要查的事实 | 注意 |
| --- | --- |
| tick 正常结束 | 不等于 Run 创建、模型开始或业务成功 |
| last_run_at / last_outcome / last_run_id | 时刻与原因属于最近回写的 occurrence，Run ID 在跳过/失败时保留旧值；不能拼成同次 Run 的开始与结果 |
| run_count / missed_count | 都不是业务成功数；前者可能漏记/重复回写，后者可能截顶，不用它们反推完整历史 |
| next_run_at 已推进但没有 Run | 认领后、创建前存在崩溃窗口；持久在途台账尚未实现 |
| 停止/编辑与触发同时发生 | 旧触发回写可能影响新配置；核对操作时间、occurrence 和关联 Run |
| 同 Task 出现多个 Run | 当前仅查本 Schedule 的 last_run_id；手动/其他 Schedule 不在互斥范围，不先判定为原键重复创建 |
| Task Center 找不到原 Schedule | 核对 Project 授权与分页 API；前 100 条或当前 TaskCatalog 卡片可能漏显，不能当成保存失败后重建 |
| UI 时间与预期不同 | 同时记录带 offset 的时刻、规则时区与浏览器时区，不仅截取无时区的时间文本 |

恢复时先做只读核对：Schedule ID、带 offset 的 UTC occurrence、原键关联 Run 和脱敏日志；分页查询使用现有 Project 授权 API 的 limit/offset，不扫描其他 Project。仅靠 last_* 无法确认时保留未知，不更新配置或伪造已结算记录来“对齐”。[在途恢复与幂等计数](../design/task-scheduling.md#可靠性修正要求待实现)是待完成设计，不是已有运维补跑命令。既有 Schedule 编辑目前有 API/client、没有页面入口；不要引导用户点击不存在的编辑按钮。

Recovery cron 分开汇总 RunAttempt lease、EffectExecution lease、普通 Interaction 超期与批准超期。`recovered_runs / recovered_effects / recovered_interactions / recovered_proposals` 持续增长时，应调查 Worker、adapter 和通知路径。技术性 Effect 重试保持原 Proposal/EffectExecution/幂等键，在次数上限内回到 REQUESTED；未知外部状态仍遵守 §8.7，不视为新批准。
