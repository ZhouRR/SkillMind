# 发布、迁移与分阶段放行

[运维入口](runbook.md#按问题找入口) · [首次启动](quickstart.md) · [备份与恢复](backup-recovery.md) · [工程脚本](../../PJM/scripts/README.md)

本页负责已有环境的发布顺序、兼容审查和放行条件；备份资产与破坏性恢复只在恢复手册维护。操作需确认环境及维护窗口，本文不是自动部署工具，也不是已经完成的部署记录。

## 先分清四种操作

| 操作 | 能证明什么、会改变什么 |
| --- | --- |
| config / preflight | 前者解析 Compose；后者连接 DB/Redis 检查迁移 head 与 PING。都不证明业务可以放行 |
| build / export / load | build 生成 image；export 打包本地已有 image；load 导入 archive。三者均不等于迁移、应用验收或备份恢复 |
| migration | 按目标 image 的迁移链改变数据库；失败后需核对实际提交 revision，不使用 stamp 掩盖失败 |
| 启动 / 放行 | 启动进程可能接收请求或消费旧工作。逐项检查成功后才允许业务，不从容器 healthy 推导完整恢复 |

命令均在目标部署目录 `PJM/` 执行。配置中不得混入另一个环境；凭据、完整 config 和业务资料不放进公开报告。实际 image ID、revision、检查范围与决策人应留在受控发布记录中。

## 一个例子：关闭 dispatch 后仍有工作

假设停写前已将一个 Effect 放入 Redis，且一个 Schedule 即将到期。仅关闭 Worker dispatch，然后启动 Worker，并不能保证两者都不再变化。这里的关闭指把 `PROJECTMIND_WORKER_DISPATCH_ENABLED` 设为 `false`。

| 实际入口 | 当前开关的作用 |
| --- | --- |
| Outbox relay | false 时不选择新的 Run/Effect dispatch topic；lifecycle 通知仍可配送 |
| 已有 Run/Effect job | job 入口不检查该开关；已入队工作仍可进入各自的 claim/executor 和权限校验 |
| 定时触发 | cron 不检查该开关，仍调用 ScheduleService；可能创建 Run 和更新触发记录 |
| 期限恢复 | cron 不检查该开关，仍处理 Run、Effect、Interaction、Proposal 的期限和恢复 |
| Skill 解释/调整 job | 单独注册的 Worker job，不由这两个 dispatch topic 控制；仍可能调用模型 |

具体入口是 [WorkerSettings](../../PJM/backend/src/projectmind/worker/settings.py) 中的 relay_outbox、execute_run / execute_effect、trigger_due_schedules、recover_expired_leases，以及解释/调整 job。以上是调用关系核对，不是实际恢复演练。关闭开关既不是维护模式，也不终止已发出的远端请求；修改环境文件也不会使运行中的进程即时重载设置。

需要停写时，先在经过批准的环境阻止业务入口和新触发、处置在途工作，再确认所有 API/Worker 写入者停止；只停止本目录的一个 service 不能证明其他实例也停止。无法确认远端 Effect 结果时保持隔离，按[原执行身份对账](runbook.md#87-incident-と-recovery)，不换键重做。

## 环境文件与配置边界

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

## 迁移前置与执行

先确认[完整恢复点](backup-recovery.md#一致恢复点包含什么)、维护窗口与目标 archive，保留当前 image ID；API、Worker、migrate 与 Web 必须按经过验证的兼容组合发布，不能只替换 API。数据库兼容、公开契约兼容与非终态 Run 可续行是三个独立门禁，见[契约版本边界](../development/contract-workflow.md#历史数据兼容不等于前后端版本兼容)。

```text
独立验收环境：完成目标版本的 smoke / 恢复场景
    ↓
目标环境：确认恢复点 → 停写与在途对账 → 加载目标镜像
  → 核对 revision / head → 单独迁移 → API/Web 只读核对
  → 批准恢复后台 → 目标验收 → 开放普通业务入口
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

## 迁移与回退审查

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
| 0030 Run budget ledger（工作副本） | Run 账户、执行预留与核对回执 | [downgrade](../../PJM/backend/migrations/versions/0030_run_budget_ledger.py)在三个表任一有数据时拒绝删表，包括已结算记录。只建空表不代表执行接入、共享预算启用或真实恢复已通过 |
| 0031 auth session credentials（工作副本） | 会话协议版、登录角色与旧会话失效事实 | [迁移](../../PJM/backend/migrations/versions/0031_auth_session_credentials.py)使旧会话要求重登；存在任何 v2 行时拒绝丢列，包括已撤销记录。切换前停止旧 API，不能混跑或删除审计行凑降级条件 |
| 0032 user lifecycle（工作副本） | 账户版本与追加安全事件 | [迁移](../../PJM/backend/migrations/versions/0032_user_lifecycle.py)给旧用户初始化版本但不伪造操作历史；有任意安全事件时拒绝删表降级。[管理 API 与初始化审计](../design/user-lifecycle.md#工作副本与公开入口)已有接线，OpenAPI/Web 和实 DB 验证未齐。不能只凭 migration 文件发布功能 |

有新状态/数据格式而旧代码不理解时，保持停写，按[完整恢复](backup-recovery.md#数据库恢复)和[版本回退](backup-recovery.md#应用版本回退)处理。历史非终态 Run 尤其要逐类验证；“历史页面能打开”不能证明新 Worker 能安全继续它。

预算切换还要满足[计量与混合 Worker 门禁](../design/run-budgets.md#上线门禁与接线顺序)。0030 不为旧 Run 补造账户；不能手工插入零余额、删除回执绕过 downgrade，或因 Run 已终态就清除未决占用。没有预算核对/退款 CLI，不把内部 store 方法改写成运维操作步骤。

## 会话协议切换检查

以下是 0031/v2 的发布审查条件，不是已经执行迁移的记录，也不授权在未知环境操作。按上面的备份、停写与迁移步骤准备；协议参数由[会话设计](../design/authentication.md#会话凭据-v2-与切换要求)维护。

1. 明确所有 API 实例的版本及流量入口；通知旧会话将需重登，先保全未确认业务动作的原 ID/key。排空并停止旧 API，不能仅让部分流量绕开它们。
2. 在授权的隔离环境先验证旧行升级、新登录、撤销/过期与回退拒绝；保留会话审计。正式迁移成功且 head 符合目标之前，不启动新 API 接收业务请求。
3. 全部新实例使用同一 v2 参数和迁移后的数据库。通过真实 HTTPS 入口检查登录、两个页面/多实例的稳定 CSRF、失效拒绝以及响应 no-store；不采集完整 Cookie/CSRF 或复用真实用户口令做探测。
4. 失败时保持停止放行，按已保全现场决定 forward fix 或完整恢复。存在 v2 审计行就不能直接 downgrade；只回滚 image、改 version 默认值或删除会话都不是兼容方案。

新旧 Web 沿既有字符串字段接收凭据，不等于整个旧 Web 版本已验收兼容。恢复旧备份还须确认旧会话不会重新成为有效登录，不能把“数据库恢复成功”当作安全切换完成。Worker/Run/Effect 的放行继续使用各自门禁，不与浏览器重新登录混为一项操作。

## 启动与放行

维护期间保持所有 Worker 停止、业务入口关闭。先分别确认 DB/Redis/object storage 已就绪、bucket 初始化与目标迁移已完成，且目标 image 已在本地；之后只启动 API/Web：

```bash
docker compose --env-file .env up -d --no-build --no-deps --pull never api web
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
make
```

这里的 [--no-deps / --pull never](https://docs.docker.com/reference/cli/docker/compose/up/)分别避免此次启动连带处理依赖服务、自动拉取缺失镜像；不验证依赖已就绪或隔离旧实例。前置不满足时停止，不临时去掉参数绕过。无这些限制的 `up api web` 可能同时处理 migrate/object-storage-init 等依赖，不属于单纯的进程检查。

`preflight` 只查 PostgreSQL 连通/迁移 head 和 Redis PING，详见[实现](../../PJM/backend/src/projectmind/ops/preflight.py)；不检查 blob、KEK、认证、模型、外部写入或 Worker 恢复。此时先核对抽样 Run/Result/Evidence 与 blob、恢复后的权限和历史输入缺失，不启动模型。

这里的“只读核对”指不提交业务变更；授权读取仍可能更新 Session idle，不能与取得一致备份时的完全停写窗口混为一谈。

模型和取消等 [smoke](runbook.md#41-通常-smoke) 必须有运行中的 Worker。通常先在独立验收环境完成：数据库、Redis/队列、存储、Worker 与凭据均有明确隔离，只建立另一个 Project 不足以隔离旧 job 或 cron。目标环境的放行再分三步：

| 阶段 | 进入条件与允许的动作 |
| --- | --- |
| 只读核对 | Worker 继续停止，普通入口关闭；完成上述基础与关联检查 |
| 恢复后台并验收 | 旧队列与在途 Run/Effect/Schedule 已核对，且获准恢复该环境全部后台工作后，才启动 Worker；入口仍只允许批准的验证，目标 smoke 属于会写入/计费的操作 |
| 开放普通业务 | 目标验收满足本次发布要求后开放入口；失败则不放行，按故障范围重新隔离并保留已产生事实，不把先前的写入说成已经回滚 |

当前没有仅让 Worker 消费某个测试 Project 的发布隔离模式；后台启动前条件不满足时，保持停止，不能为了跑 smoke 而跳过对账。`make deploy` 会直接重启整套服务，不实现这些阶段门禁；需要分阶段放行的恢复现场不要直接使用它。

## 后续开发约束与验收

当前没有统一维护/分阶段发布编排。后续工程改造由[计划 R11](../planning/roadmap.md#r11-运维与工程工具)管理，应保留以下可观察条件，不把现有 dispatch 开关改称全局停写开关：

| 边界 | 实施与验收要求 |
| --- | --- |
| 停写覆盖 | 明确 API、已入队 job、Schedule、recovery、解释 job 和多实例的控制面；演练关闸前后已有/新增工作的处理，不能只测试 Outbox 不 enqueue |
| 发布阶段 | 环境/资产预检、停写、迁移、只读核对、后台恢复许可、目标验收与普通入口放行分开；阶段失败不自动进入下一阶段，不制造“Worker 未启动却先通过模型 smoke”的循环前提 |
| 配置与镜像 | 默认/自定义配置来源一致；检查 shell 覆盖、缺失文件、缺失 image、失败 archive 与实际 image ID，不依赖同名 tag |
| 恢复后安全 | 验证旧会话/权限、在途 Effect、旧队列和非终态 Run；“preflight 成功”和“旧页面可读”都不是恢复执行许可 |

实现调整同步 Make/Compose/脚本、Settings/startup、受影响 job/服务及其测试，并更新本页操作步骤。只有公开接口或持久数据发生变化时才同步相应契约/迁移，不为文档中的阶段名称机械建表。
