# ProjectMind 运维与故障排查

本页负责按症状排障和选择验收场景。首次准备见[Quickstart](quickstart.md)，版本更新见[发布与迁移](deployment.md)，数据恢复见[备份与恢复](backup-recovery.md)。运行规则由[Runtime](../design/agent-runtime.md)维护；旧部署/恢复章节仅保留跳转，不复制另一套操作步骤。

> 文档中的目标不等于现成恢复工具。部署版本、工作副本与未完成事项分别在[计划](../planning/roadmap.md#13-当前执行状态)核对；本页不证明某个镜像已通过恢复演练。

## 按问题找入口

| 需要处理的情况 | 先做什么 | 手册位置 |
| --- | --- | --- |
| 登录正常却不能提交，或认证突然失败 | 只核对原响应、凭据类型与环境，不重放业务动作 | [认证分诊](#认证故障的只读分诊) |
| 项目消失、归档后仍在运行、删除失败 | 核对原项目 ID、成员、状态及引用，不删除配置或重建同 key 项目 | [项目分诊](#项目与归档的只读分诊) |
| 准备更新镜像或迁移 | 确认维护窗口，保存同一恢复点的 DB/blob/workspace 与镜像 | [备份](backup-recovery.md#配备前备份)、[配备](deployment.md#迁移前置与执行) |
| 更换环境配置文件 | 区分 Compose 插值与容器配置，不能只改 ENV_FILE | [配置边界](deployment.md#环境文件与配置边界) |
| 新环境没有管理员 | 使用普通 bootstrap CLI，不执行清空 volume 的初始化目标 | [首次 ADMIN](quickstart.md#最初の-admin-を作成する) |
| Run 卡在运行中或不断重试 | 先区分输入准备、模型等待、执行权失效，不改 SQL 状态 | [准备分诊](#准备故障的只读分诊)、[接管验证](#42-worker-喪失と接管)、[日志](#7-log-確認と-incident-記録) |
| 正在等待回答/批准 | 核对待办版本、身份与独立期限，不当成 Worker 卡死 | [WAITING](#81-waiting-状態) |
| 外部写入结果不明 | 先查原幂等身份的结果/read-back，保留已发生的变更 | [Effect 排障](#87-incident-と-recovery) |
| 已结束但结果可疑、评价是否保存不明 | 区分原结果、平台效果记录与评价历史；不重放评价或改写原值 | [结果与评价分诊](#结果与评价的只读分诊) |
| 输入文档缺失或内容不一致 | 保留 manifest/现场，确认运行版本与冻结清单 | [资源排障](#9-资源快照排障) |
| 上传部分失败、列表消失但附件仍在 | 区分元数据、存储和原请求结果，不重传整批或清 bucket | [文档分诊](#文档保存与删除的只读分诊) |
| 定时任务没有生成 Run | 分开看 created/skipped/failed/missed，不手动补造触发 | [调度监视](#10-schedule-と-recovery-の監視) |
| 必须恢复旧版本或数据 | 先确认完整恢复点及新旧兼容性；这是有数据损失风险的操作 | [DB restore](backup-recovery.md#数据库恢复)、[版本回退](backup-recovery.md#应用版本回退) |

以下命令不会因为写在手册里就获得执行授权。尤其 restore、镜像替换、停止 Worker 和 smoke 写入，应在已确认的专用环境或获批维护窗口执行。

## 项目与归档的只读分诊

先确认原 project_id 与调用者身份，再看服务端状态；当前页面可能在失效深链接后回退到另一个项目，不能从导航选中项推断原项目仍可访问。404 不区分不存在和越权，不用其他账号探测资源是否存在。

| 症状 | 只读核对与停止条件 |
| --- | --- |
| 被移出项目后仍能访问 | 检查当前系统角色；ADMIN 不依赖成员关系，不能用移除 membership 代替降权。账户变更走独立用户管理 |
| 归档后仍有 Run 或外部动作 | 归档不是取消或维护停写；核对原 Run / Effect 事实，按独立停止协议处理，不把状态改成终态 |
| 无 Run 但删除失败 | 当前删除清单遗漏 TaskSchedule 的 RESTRICT 引用；核对是否保存过调度及原错误，不能拆外键、清调度历史或循环 DELETE |
| 已返回 204，却仍有附件或备份 | 项目删除只处理所列元数据，不证明 blob/backup 已消除；按独立清理与恢复策略核对，不手动递归删存储目录 |

具体[归档入口](../design/project-lifecycle.md#归档的实际边界)与[删除限制](../design/project-lifecycle.md#删除与数据保留)由设计维护。此处不提供实际删除或恢复命令；需要写操作时先确认目标、权限、维护窗口和可恢复性。

## 文档保存与删除的只读分诊

先记录原 Project/document ID、请求时点、HTTP status/Problem code 和部署版本；上传未取得 ID 时保留原路径等受控本地信息，不复制正文、存储凭据或内部 key 到共享报告。使用当前有权访问的元数据/内容确认事实，不以管理员换号探测。

| 症状 | 核对与停止条件 |
| --- | --- |
| 同名 409，但存储占用增加 | blob 写入先于元数据约束；可能留下失败上传对象，不把 409 当作零副作用，不重复上传来验证 |
| 目录只成功一部分 | 按文件分清已确认与未知，done 不是成功数；不重新发送整个目录，也不从同名项推断原请求已成功 |
| 删除报错后列表已无原 ID | 元数据可能已提交；404 不能让当前 DELETE 恢复 blob 清理，不循环重试或改删同路径新 ID |
| 204 后对象仍在 | 当前 S3 适配器可能吞掉权限等错误；由存储负责人在授权范围核对原对象，不递归删除 Project 前缀 |
| 上传可用，下载失败或内容不符 | 区分非 Latin-1 文件名 header、元数据/实际字节差异与存储可用性；不要修改 checksum 来掩盖损坏 |

规则正本是[保存与清理](../design/document-lifecycle.md#删除与历史引用)。目前没有持久清理回执和通用修复 CLI；需要删除孤立对象、调整配额或恢复数据时先取得明确目标和维护授权。Run 已冻结输入的问题转[资源排障](#9-资源快照排障)，不以重传同名文件修复原 Run。

## 1. 運用原則

- PostgreSQL 是业务与审计正本；Redis 队列或通知不替代 Run、Attempt、Event、Evidence、Result 和认证记录。
- 镜像替换与迁移前保留完整恢复点及实际 image ID；相同 tag 不证明 image 内容相同。
- 不用 `stamp` 绕过迁移失败，不手工删除审计行、改 Run 状态或改幂等键来消除错误。
- `.env`、密码、session/CSRF token、KEK、Provider 凭据及业务正文不进入普通备份清单、日志或截图。
- 本地恢复不回滚外部系统；有结果不明的 Effect 时先隔离自动执行并核对外部事实。

以下命令都在正式代码目录 `PJM/` 执行，默认只使用该环境自己的 `.env`。排障先使用已有响应、日志和获准的只读查询；需要启动、写入或故障注入时按下面各节的专用环境前置重新确认范围。

## 4. Generic task acceptance と障害接管

### 4.1 通常 smoke

smoke 会调用模型、创建 Run/Evaluation 并测试取消，属于有费用和审计写入的验收，不是只读健康检查。使用已批准的测试环境和专用 Project；显式指定其中的 published task，不使用业务默认项目或已退役的 bootstrap seed。先按[发布手册](deployment.md#启动与放行)完成 Worker 放行检查，再启用 dispatch 和必要模型凭据。

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

### 结果与评价的只读分诊

先保留发生时间、环境版本、Run / Result / Evaluation ID 和原 HTTP status/Problem code，不把完整结果、评价正文或凭据复制到共享日志。仅用当前有权访问的 detail/评价历史核对：

- SUCCEEDED 与 PARTIAL/BLOCKED 可以并存；[执行成功与业务判断](../design/results-evaluation.md#先分清四种事实)分开看。不为修摘要改 SQL、重开终态 Run 或重新执行外部写入。
- 模型摘要写 APPLIED 时，仍以平台 Proposal/Effect/read-back 为准；引用字符串与 Artifact 计数不是内容可读的证明。缺口按[结果校验边界](../design/results-evaluation.md#结果校验的实际保证)登记，不按自由 URL 下载来“补证据”。
- 评价 400 先核对 pointer 相对的[原结果内容](../design/results-evaluation.md#修订指向哪份原值)，409 核对 Result 是否存在；401/403 返回认证分诊。没有评价与没有 Result 是不同情况。
- 响应丢失后按[评价结果未知](../design/results-evaluation.md#提交未知与界面责任)处理。刷新历史只辅助判断，不能按同文/时间推断是哪次提交；不自动重发或删除疑似重复评价来修复界面。

这些是只读排障入口，不会执行模型、评价追加、外部修改或数据库修复；真实故障注入另走专用环境验收。

### 认证故障的只读分诊

先记录发生时间、公开 path（不含业务参数）、HTTP status、Problem code、request_id 与环境/image 版本。只看已发生请求中 header 是否存在、cookie 属性和页面刷新顺序，不复制密码、Cookie / Set-Cookie 原值、CSRF、HAR 全量导出或凭据表单截图。

| 原响应 | 排查方向与停止条件 |
| --- | --- |
| 401 凭据错误 | `invalid_credentials`：隐藏账号存在性；核对登录环境与错误类型，不通过批量换账号探测或重跑 bootstrap 处理 |
| 400 当前密码错误 | `current_password_rejected`：工作副本改密 API 的操作拒绝，不是登录 401；保留仍有效的会话，按[账户失败语义](../design/user-lifecycle.md#生效与界面)处理 |
| 401 需要认证 | `authentication_required`：核对 cookie 是否发送、期限/登出/用户状态，以及升级重登、角色变化和 API 版本是否一致；v2 协议或保存 hash 不匹配也会拒绝。不公开具体内部原因 |
| 403 写凭据拒绝 | `csrf_rejected`：核对 Origin 的 scheme/host/port、header 类型及是否换过登录 cookie。同一 v2 会话的正常读取不使另一页 CSRF 失效；不关闭 CSRF，不改成不安全 cookie |
| 403 管理权限不足 | `administrator_required`：需要 system ADMIN；ProjectMember 不等于 ADMIN，不修改角色数据绕过 |
| 404 资源不存在 | 也可能是越权的隐藏结果；只核对已有授权的 Project/资源上下文，不自动切换项目或枚举其他 ID |
| 409 项目已归档 | `project_archived`：要求 ACTIVE 的业务写入被拒绝；不代表所有管理/取消入口均关闭，见[归档边界](../design/project-lifecycle.md#归档的实际边界)。不为诊断擅自恢复项目 |
| 429 暂时限流 | `login_rate_limited`：工作副本含来源/账号/组合和有限退避；读取 Retry-After 后停止快速重试，不判断是哪个维度或账号状态。按[登录防护分诊](#登录防护的排查与恢复)核对版本和来源 |
| 503 防护不可用 | `login_protection_unavailable`：登录短期防护无法确认；不当成密码错误。检查指定实例的 Redis 可用性/权限/超时与版本，不重跑 bootstrap 或删除配额 |
| 422 请求校验错误 | `validation_error` 或账户用例的 `invalid_user_request`：缺失必需 header、结构或业务输入不合法；先按契约修正，不展示可能带密码的原输入 |

`GET auth/session` 在 v2 不轮换 CSRF，但认证仍会锁行并可能更新 idle；`GET auth/login-context` 占用来源配额并创建短时 challenge。它们不是完全只读的健康探针。若部署仍有旧 API，则先处理[协议切换条件](deployment.md#会话协议切换检查)，不要用反复刷新掩盖版本混合。需要重新登录时先保全未确认请求的 ID/key；刷新可能丢失页面内记录，应先核对已提交事实。

现有行为与未完成流程分开见[会话读取与多页面](../design/authentication.md#会话读取与多页面)，业务结果未知见[原要求确认](../design/run-creation.md#提交结果未知时的界面责任)。403 不能一律触发自动业务重试，也不证明之前的请求未提交。API 的 no-store 不证明代理自产错误或整个站点禁止缓存；只检查已发生响应的属性。

[用户管理](../design/user-lifecycle.md#工作副本与公开入口)已有 Backend 工作副本，但快照/Web/整体验证尚未完成。这里不提供改密、停用或批量撤销 CLI，也不把这些状态变更当作只读排障；不能用直接 SQL 或 bootstrap 代替。409 的账户版本、末位 ADMIN、重复 email 分别按[账户错误说明](../design/user-lifecycle.md#生效与界面)处理，不统一建议换版本重试。

### 登录防护的排查与恢复

这里沿已发生请求和获准的监控做只读排查，不通过持续请求登录接口制造样本。先按上一节保留 status/code/request_id；Retry-After 的整数秒数可记录，不采集 Cookie、challenge、密码或 Redis key 全量。

1. 确认 API 镜像和限流配置是否一致，判断部署使用旧组合计数还是[三维防护](../design/login-protection.md#来源识别与上线边界)。工作副本或 Schema 的版本不是部署证据。
2. 对 429：核对共享出口、重复获取 challenge、重定向与可信代理设置。一轮正常登录通常有两次来源请求；工作副本本人改密也共用配额，但不取 challenge。429 不证明密码错误，也不说明账号被停用。等待不会保证下次成功，不轮询 challenge 或换来源绕过限制。
3. 对 503：先区分有 login_protection_unavailable 的应用拒绝与代理/未知响应；Web 的通用“登录服务暂不可用”不直接给出根因。确认属于防护后，再检查指定 Redis 的健康、连接/命令权限、容量/驱逐、错误与超时记录；PING 成功不证明 EVAL、SET 或 GETDEL 可用。损坏/无 TTL 状态、challenge 保存未确认或标记异常也会拒绝，不能把每种 503 都解释为短时网络失败。
4. 需要写入修复、改配置、重启或切换 Redis 时停止只读诊断，转为经环境负责人确认的维护操作。保存故障证据，评审配额丢失、队列/challenge 和其他短期状态的影响；不提供通用 FLUSHDB/删 key 的“解锁”命令。

修复后的主动登录验证另需专用账号、批准的环境与明确范围。先核对实际 proxy 的来源恢复，再确认正常登录、429 等待、错误来源不可伪造和 503 不放行密码验证；这不是通过已打开会话能读页面就足够。登录防护本身不撤销既有会话，Redis 恢复也不证明用户生命周期、Worker 或外部效果已恢复。

详细计数/TTL 留在[设计正本](../design/login-protection.md#计数与退避如何恢复)。限流版本切换与[会话协议切换](deployment.md#会话协议切换检查)分别审核，不用清除会话审计、关闭 CSRF 或扩大代理信任范围来排障。

排查 UI 时，区分“未显示秒数”和“服务端没有限流”：当前 Web 只把合法的 1–300 秒用于静态等待提示，没有倒计时，也不强制锁定按钮到期。先核对实际 Web/API 版本和已发生响应，不通过快速点击验证封禁。密码发送后若只观察到网络失败或离页，按[结果未知](../design/login-protection.md#提交离页与结果未知)处理，不能从页面未跳转断言登录没有提交。

## 7. Log 確認と incident 記録

ProjectMind 使用单行 JSON 日志。先按 API 的 trace/request ID 或 Worker 的 Run/Attempt ID 缩小范围，再关联 Segment/Session 与外部 Effect。以下只在受控终端查看：

```bash
docker compose --env-file .env logs --no-log-prefix api worker \
  | jq -c 'select(.run_id == "<RUN_ID>" or .trace_id == "<TRACE_ID>")'
```

当前工作副本由服务器重新生成 X-Request-ID，客户端发送的同名 header 不再作为审计关联值。使用已收到响应中的 ID 与 Problem.request_id 对照；代理自己的关联方式另查，不从任意外部 ID 推断已发生用户安全事件。request ID 不是重放键，详见[审计关联](../design/user-lifecycle.md#审计与请求关联)。

对外交接保留 UTC 时点、image ID、migration revision、脱敏对象 ID、公开错误码和已完成的检查。不要转录密码、token、凭据、票据正文、Tool 原始参数/结果或 Evidence 摘录。普通 incident 不直接附 `.env`、完整容器配置或整个 workspace。

## 8. 対話型 Run と外部 Effect の運用契約

本节是排障索引，状态与批准规则以[Runtime](../design/agent-runtime.md)和[受控写入](../design/repository-effects.md)为准。已注册的写入边界包括 Redmine `issue.update/v1` CAS adapter 和 Git/SVN `repository.write/v1`；不通过普通 Redmine 更新接口或任意命令绕过原子前置条件、幂等与 read-back。repository 始终需要人工批准。

### 8.1 WAITING 状態

- `WAITING_FOR_INPUT` / `WAITING_FOR_APPROVAL` 不是终态，也不是需要强制重启 Worker 的故障。人工等待应释放主执行 lease，不继续占用模型进程；Interaction 的期限与 active wall timeout 分开。批准后的 Effect 可持有独立 lease，Run 仍可能显示 WAITING_FOR_APPROVAL，须查 Effect 状态而不是再次批准。
- 核对持久的 Interaction、Brief/checkpoint、Session 和事件。接收有效回答后追加 Segment；技术故障重试才在原 Segment 追加 Attempt。
- 普通 Interaction 超期记录 `INTERACTION_EXPIRED`，通过 `INTERACTION_TIMEOUT` Segment 继续，不代选推荐答案。Effect 批准超期则使 Proposal 失效，不调用写入 Provider。
- 取消、成员资格失效或 scope 改变不能靠复用旧答案/批准继续执行。等待期限不等于数据保留期限；自动清理仍属独立设计。

排障关联 Run/Segment/Attempt/Session/Interaction ID。PRIMARY 同时最多一个 ACTIVE；符合[子分析设计](../design/subagents.md)的 SUBAGENT/BRANCH 可以并行，不能只凭 ACTIVE 总数判定重复执行。

普通答复的[410 与过期提交](../design/user-interactions.md#过期与拒绝响应)不同于事务回滚。先读取已保存的交互状态、答复、Segment 和事件，再判断是回答成功、过期续行还是仍在等待；不凭页面报错补造答案或更换 key。外部批准继续按 Proposal / Effect 核对，Evaluation 不负责恢复 Run。

若只有 EFFECT_APPROVAL Interaction 而没有对应 Proposal，可能落入已识别的[悬空批准入口](../design/user-interactions.md#普通提问不能代替外部批准)。保留脱敏 ID 与已有事件，核对创建路径；不要调用通用答复、手写审批记录或把它当成普通超期恢复。该风险的源码核对不是现成修复工具，处理需按获准的代码修正或取消流程进行。

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

MANAGED 当前用部署主密钥直接执行 AES-256-GCM，不是 DEK/KEK 两层信封结构；历史 KEK 设置名保持兼容，见[实际保存形式](../design/secret-storage.md#managed-的实际加密结构)。KEK 丢失会使对应凭据无法恢复，DB 备份本身不能补救。

keyring 使用逗号分隔的 `version:base64key`，每个 key 为 32 字节随机值；首项用于新加密，后续旧版本用于解密。通过环境的 Secret 管理流程生成和注入，不将实际密钥输出到录屏、聊天或公共命令示例。

执行前按[切换与恢复顺序](../design/secret-storage.md#切换与恢复的顺序)确认维护窗口、备份、所有读写进程的 keyring 与旧备份解密条件。API/Worker 在启动时加载 cipher；只修改 CLI 环境并不更新运行中的实例。新版本标签必须指向新 key，禁止沿用标签替换 key bytes。

在获批窗口执行 `python -m projectmind.ops.rotate_secrets`；检查退出状态/数量并验证所需解封。此 CLI 会锁定并改写现有材料，不是只读检查，也不改变 SecretReference/Integration/Run 身份。当前 active version 的行只记 skipped、不验证解密；全部 skipped 或仅退出成功均不足以证明完整恢复可用。没有现成的全量只读解密验证 CLI，不输出凭据原值作替代。

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

工作副本已有输入回执、物化器/ContextBuilder、Worker/Tool 接线及准备期监督；消费者、本地多根边界和文件级故障已有回归，真实 DB 与整条恢复链仍待验收。当前范围见[计划](../planning/roadmap.md#13-当前执行状态)，不能据本地测试通过升级生产。`input/` 的逻辑路径不等于旧同名物理目录，恢复须核对 Run 回执所指的世代。

没有补签/修复 CLI，也没有完整运行链验收。按[中断与再次使用](../design/resource-snapshots.md#准备中断与再次使用)区分缺回执、PREPARING、READY、提交结果未知和文件不匹配；目录看似完整或 migration 成功都不成为放行依据。现场不能由运维手工填成 READY、把更晚文件移入旧世代或删除后让原 Run 自动重建。

### 准备故障的只读分诊

先在已有授权范围内查 Run/Attempt 记录、脱敏日志和对应输入回执；不要为定位问题启动模型或重做物化。内部回执没有公开管理 API，以下是查阅顺序，不是新的运维 CLI。

| 观察 | 下一步核对 |
| --- | --- |
| 已进入运行态，但没有会话 | `RUNNING` 与 `run.execution.prepared` 在 ContextBuilder 前产生；依次查准备错误、输入回执、Brief 与 Worker，不当作模型启动证据 |
| 准备超时 | `preparation_timeout`：核对准备配置、各资源耗时与回执提交结果；模型 wall timeout 不是该配置的替代，不直接提高所有上限 |
| 准备失败 | `context_build_failed`：查脱敏异常类型、冻结来源、物化消费者与 binding；Provider timeout、调用参数缺失也可能归到此处，不一律归因于网络 |
| 首次准备拒绝创建世代 | 核对 `.projectmind-inputs` 是否已有旧命名空间；空目录、其他 UUID 和 symlink 也会被拒绝。不删除、改名或填 READY 来绕过检查，按输入中断协议保留现场 |
| 执行权异常或不断接管 | 查 lease 到期、token 不匹配与 Run/Segment/Attempt 时间线；旧 Worker 不应另写终态，当前状态交给合法恢复流程判断 |
| 已接受取消，但还没有终态 | 查持久取消意图、当前 lease 和 Session 事件；首事件前没有 Session event 不证明模型未启动。已有等待取消接线，真实停止仍需核对，不能以按钮响应或 CANCELLED 证明进程已退出；见[停止事实的区分](../design/run-supervision.md#一个例子点击取消之后) |
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

## 旧部署与恢复入口

以下保留旧章标题和锚点，供已有链接跳转。新读者从页首按问题导航，部署/恢复命令只在新的正本维护。

### ProjectMind 部署与恢复 Runbook

旧标题入口保留。现在从[按问题找入口](#按问题找入口)选择排障路径，发布与数据恢复分别进入对应手册。

### 环境文件与配置边界

配置来源、混用风险和静态检查已移至[发布手册](deployment.md#环境文件与配置边界)。

### 2. 配備前 backup

备份步骤统一维护在[备份与恢复](backup-recovery.md#配备前备份)，本节保留旧入口；后续阅读不必沿旧编号逐章进行。

#### 一致恢复点包含什么

DB、blob/workspace、image/配置引用、KEK 与外部对账资料的关系见[恢复点清单](backup-recovery.md#一致恢复点包含什么)。

#### 取得并检查数据库备份

停写前置、pg_dump、校验值和 archive 检查见[数据库备份步骤](backup-recovery.md#取得并检查数据库备份)。

### 3. Migration と配備

现有环境更新统一按[发布、迁移与放行](deployment.md)操作；dispatch=false 不等于停写，先读[已有工作仍会执行的例子](deployment.md#一个例子关闭-dispatch-后仍有工作)。

#### 迁移与回退审查

具体迁移及历史兼容限制见[迁移审查](deployment.md#迁移与回退审查)。

#### 会话协议切换检查

旧实例排空、0031/v2 与真实入口检查见[会话切换](deployment.md#会话协议切换检查)。

#### 启动与放行

依赖就绪、只启动 API/Web 与最终 Worker 放行见[分阶段启动](deployment.md#启动与放行)。

### 5. Database restore

数据库替换是破坏性操作，完整步骤只在[恢复手册](backup-recovery.md#数据库恢复)维护。

#### 恢复前的停止条件

环境、资产、当前现场和外部变更须同时满足[恢复前置](backup-recovery.md#恢复前的停止条件)。

#### 替换数据库

仅在已确认的维护窗口执行[数据库替换](backup-recovery.md#替换数据库)，失败后保持隔离。

#### 恢复后验证

权限、数据引用、可续行性和外部事实分别按[恢复后验证](backup-recovery.md#恢复后验证)举证，不以 preflight 代替。

### 6. Application version rollback

先按[应用版本回退](backup-recovery.md#应用版本回退)区分兼容镜像替换与完整数据恢复；两者不是同一种操作。
