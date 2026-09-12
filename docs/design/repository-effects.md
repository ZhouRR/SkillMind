# 受控外部写入

本页定义提案、批准、远端效果与恢复；实现状态见[计划 R08](../planning/roadmap.md#开发任务)，续行见 [Runtime](agent-runtime.md)，处置见[Runbook](../operations/runbook.md)。

## 先分清四种事实

Git push/回读成功而 PR 超时：commit 已成立，PR/平台保存仍可能未知；重启、再批准、取消 Run 均不撤销 commit。

| 对象 | 状态含义 |
| --- | --- |
| ChangeProposal | 冻结变更；APPROVED 是获准，不是已写入 |
| ChangeApproval | 对精确 version/checksum 的决定，不扩其他权限 |
| EffectExecution | APPLIED 是已保存 Provider 完成结果；FAILED 不证明远端未变，VERIFICATION_FAILED 属于此对象 |
| Run / Evidence | 分别是分析流程与审计依据，不代替远端执行事实 |

Effect attempt_no 非 RunAttempt。before_ref/after_ref 可空，仓库 before 只定位基线、不保证逆向补丁。模型 effects 的[保存记录核验](results-evaluation.md#效果摘要核对到哪一步)不证明远端原执行或解决可靠性缺口。

## 调用与批准链路

```text
observe → Evidence → change.propose → 精确批准/允许的预授权
  → TX 1：批准 / Effect / Outbox
  → TX 2：提案与 binding 校验 / claim
  → 锁外：凭据 / 前置检查 / 写入 / read-back / 可选 PR
  → TX 3：结果 / Evidence / 事件 / 后续调度
```

锁序 Run → Segment → Proposal → Effect，远端 I/O 在锁外，无跨 DB/仓库/forge 原子性。拒绝不产生可执行 Effect；批准后 Run 可仍 WAITING_FOR_APPROVAL，Effect Worker 独立执行。

有效 finalize 的结果/不可重试失败交新 Segment，临时失败沿原 Segment/Proposal/Effect 重调度。Agent 仅提案；[catalog](../../SKM/backend/src/skillmind/effects/catalog.py)统一 Provider/validator/预授权资格。

## 已注册写入

| capability | 批准与执行边界 |
| --- | --- |
| issue.update/v1 | Redmine CAS adapter；仅 system ADMIN 配置 LOW 风险精确 scope 可预授权，discovery → 前置 revision → 条件写入 → 回读 |
| repository.write/v1 | Git/SVN，始终人工批准，不 force；精确 CAS/恢复仍有缺口 |
| database.write/v1 | PostgreSQL 单行 INSERT/UPDATE，始终人工批准；独立部署开关、可信原行 Evidence、精确表/列范围与同事务回执 |
| document.write/v1 | 项目文档库 CREATE，始终人工批准；Provider、阶段事务及 API/Worker/解释器装配已有，服务端保证与真实验收待补，保持独立关闭 |

标准 Redmine REST 不具本协议 CAS/幂等，须通过版本化 discovery 及真实竞争验收。批准复验 actor/Project/version/checksum/Integration/binding/scope；仅 Run 发起人或组织 system ADMIN 决策。HTTP 决策在共享事务内锁定当前账户、原 Session 和项目，复验 CSRF、当前角色及成员关系，提交前再次验证；不能沿用请求开始时缓存的管理员身份。

## 首版所需的存储和数据库写入

rv-reviewer 的完整验收需要 PostgreSQL 执行/文档/成果记录和 MinIO 成果上传；数据库 Provider 已注册，MinIO 成果 Provider 已有但尚未在生产配置中开放。按通用能力实现，业务表、字段、PASS/FAIL 规则仍属于 Skill 与项目绑定，不能写入平台通用 Schema 或执行器。

数据库写入只接受显式表、列、值及精确行条件，范围来自冻结 binding；参数化执行，不接受模型 SQL 或连接信息。MinIO 写入固定 bucket、允许 prefix、对象 key、内容 hash 和原 Effect 身份，字节来自已冻结的 Run 成果；禁止无条件覆盖和越界路径。两者均沿 observe → propose → 精确批准 → apply → read-back，低风险自动批准须另有已实现的范围约束，不能由 Skill 指令、远端 tool 注解或全局扩展开关代替。

数据库和对象存储不跨系统原子提交。每次外部调用保存原身份和阶段回执，响应丢失先只读核对；同名/同值不能证明原写入，不换 ID 重做。取消保留已发生记录，恢复只补已证明未发生的步骤。引入新能力前同步公开契约、权限/预算、原凭据复验、独立门禁和 Worker 恢复；未经批准的操作、旧 Worker、目标漂移、部分成功及未知费用均须失败停止并可核对。

### PostgreSQL 单行事务与原执行回执

[`database_write`](../../SKM/backend/src/skillmind/effects/database_write.py) 与 [`postgres_write`](../../SKM/backend/src/skillmind/effects/postgres_write.py) 提供单行事务客户端，[`database_provider`](../../SKM/backend/src/skillmind/effects/database_provider.py) 经共享工厂接入 approved-effect Worker。`SKILLMIND_DATABASE_WRITES_ENABLED` 默认关闭，与后置功能开关独立；能力目录、Run/context、提案/批准、claim/阶段复验及恢复入口统一检查 capability 和操作，旧队列不能绕过。启用数据库不开放其他外部写入、调度或子 Agent，旧扩展开关也不能开启数据库。公开契约见 [database.write/v1](../../SKM/contracts/tools/database.write/v1/request.schema.json)，实际数据库验收仍待完成。

提案使用通用 `change.propose/v1` 的单个 `/row` SET，值包含 `key`、`values`、`expected`；目标是 `schema.table`，revision 是原行摘要或插入的 `absent`。表、操作、可写列采用显式列表，列名为 `schema.table.column`。新建 Run 对写入资源仍只公开已声明的读取 Tool，冻结 binding 保留写入能力；缺少读取声明直接拒绝，不改写历史快照。

创建及使用提案时复验 Evidence：须来自同一 Run、Integration 和冻结 binding 的成功 `database.read/v1` ToolCall，按完整主键精确过滤、无列投影、零 offset、未截断，且完整原行摘要或不存在结果与提案一致。读取响应附加 `row_hashes`，保留既有 content_hash 计算语义；旧 Evidence 缺少这些事实时须重新观察。数据库 effect 不是可直接调用的 Agent Tool，也不允许预授权。

客户端把 Project/Run/Integration/Effect、表、完整主键、操作、值和原行冻结到 checksum。只允许显式 `INSERT`/`UPDATE`：插入须原行不存在，更新须完整主键和观察到的原行一致；只写允许列，禁止主键更新和显式写 generated/identity 列。原执行权 callback 由共享 `EffectService.authorize_effect_step` 提供，连接前、等待/观察后及提交前复验，不能由模型提供。该入口先重新解析相同 binding/凭据，再按 Organization → User → Project → Run → Segment → Proposal → Effect 的锁序检查当前发起人、批准者、项目成员、取消、原批准 version/checksum、完整 claim 和 lease；所有等待后再次核对到期时间。后台沿持久人工批准检查当前账户和项目权限，不伪造浏览器会话；此入口不接受预授权，也尚未替换旧 Provider 的执行路径。

单次专用连接内，READ COMMITTED 事务按原 Effect 取得事务级 advisory lock，先查回执，再核对原值、参数化修改、回读并保存回执，一同提交。UPDATE 在原行观察时加锁；INSERT 用普通 SELECT 确认不存在，由完整主键的唯一约束拒绝并发插入，插入后沿同一事务的写锁回读，因此仅需 SELECT/INSERT 权限。值通过目标表类型转换，SQL 标识符独立验证并引用；命令及单行结果有 1 MiB 上限，连接/语句/锁等待/整次操作分别限时。已存在同 Effect 且 checksum 一致的回执返回原前后事实，即使目标行后来改变也不再写入；同值但无原回执不能认作成功。commit 或连接结果未知返回需核对状态，不生成新身份。Provider 再 claim 时先只读 lookup；原回执存在即返回，不再写入。未检出不能证明旧请求未到达，后续仍以同一命令取得原 Effect lock、重新查回执并核对原行，等待先行事务结束；不以一次未检出或今日同值绕过此协议。次数耗尽、取消及批准过期后的未知处置仍待闭合。

[回执 DDL](../../SKM/scripts/sql/postgres-effect-receipts.sql)只定义独立 `skillmind_effects` schema，不创建业务表或凭据，也不由 Worker 自动执行。配置数据库时由独立 owner 安装，执行账号仅获得 schema USAGE、回执 SELECT/INSERT 与明确业务表权限，不授予回执修改/删除/截断或 schema CREATE。回执表示原事务已保存的事实，不是当前业务行状态，也不提供 MinIO 跨系统原子性。

新建 RV 业务库可使用独立的[业务 DDL](../../SKM/scripts/sql/rv-reviewer-schema.sql)和[列权限脚本](../../SKM/scripts/sql/rv-reviewer-grants.sql)，它们不属于平台 migration，也不由 Worker 执行。三张表保存运行、文档和成果；评价与 `rv_result` 的判定、原 ID 和版本必须匹配，结束状态与结束时间成对保存，`spec_status` 仅导出原 Skill 指定的 FAIL 状态。成果用原 Run/文档/种类作为完整主键，并检查文档、文档库和 bucket 归属；成果表仅授予 SELECT/INSERT，运行的输入条件和文档的来源路径不授予 UPDATE。此 DDL 是尚未配置的目标库的初始化契约，不表示现有项目已部署；既有库须先核对兼容性，不能直接覆盖。它不证明对象已上传、RV 质量正确或跨表件数已核对。

隔离 PGlite 探测覆盖 SQL、类型转换、回执、撤权回滚与最小回执权限；可追加 RV 业务 DDL、列权限及记录约束验证。它只有单个连接，不能替代真实 PostgreSQL 的 asyncpg/TLS、并发锁、断连提交及角色部署验收，命令见[本地验证](../development/local-development.md#postgresql-写入的隔离-sql-探测)。

### MinIO 条件创建与原结果核对

[`library`](../../SKM/backend/src/skillmind/documents/library.py) 定义项目文档库的 Run binding：`project-library` Provider、`document.write/v1` 能力和空 Integration，冻结 Project、存储世代、bucket 与项目专用 prefix；使用时须与当前配置重新核对，不创建占位连接或凭据。API 候选与 API/Worker 的 Run 选择、同事务冻结已接入，复用既有 FileStorage 的持久存储归属；选择与历史投影见[资源快照](resource-snapshots.md#公开选择与读取投影的实施契约)。API、Worker、解释器共用配备能力转换；文档写入采用独立的内部门禁，现有数据库与后置开关均不能开启。Settings 尚不提供文档写入开关，生产解释器目录和 API 可用性索引仍排除此能力；完整目录中的 Effect 说明不等于开放，也不授予直接 Tool。

保存提案仅允许 `CREATE`、原目标 `absent` 及 `/document` 回读，内容为同 Run 已存 Artifact 的引用、摘要、字节数和批准的保存 MIME；相对路径来自提案，物理 key 由冻结的项目文档库推导。模型不能提供正文、bucket 或任意 object key。[document.write/v1 契约](../../SKM/contracts/tools/document.write/v1/request.schema.json) 与共享提案、批准、认领、阶段复验已接入：每次验证原 binding/snapshot、当前存储配置及 Artifact 实际字节的 size/hash。Artifact 当前由 producer 以 text/plain 保存；批准的 Markdown/JSON MIME 是输出格式，不转换或替换原字节。

0046 仅允许文档库 CREATE 提案的 Integration 为空，其他 capability 仍要求有效 Integration；公开响应与 Web 同样限制此空值；开放前须同步升级 API/Web/Worker，旧 Web 无法读取此新提案形状。旧提案的非空 ID 和 checksum 不变，不补造历史关联，降级在排他锁下拒绝含文档库提案的数据库。文档库始终人工批准，阶段授权复用当前账户、项目、原批准和 lease 校验，使用当前配置的保存目标，不解析虚构的 Integration 凭据。[运行上下文](resource-snapshots.md#公开选择与读取投影的实施契约)支持保存槽位及其提案 Tool，这些校验不代表远端效果已执行。

[`effect_write`](../../SKM/backend/src/skillmind/storage/effect_write.py) 将同 Project/Run 的已存 Artifact 字节、原 Effect、存储世代、bucket/key、MIME 与内容摘要固定为一个请求。路径须与批准 prefix 精确匹配，不从可变 workspace 重新取正文，也不把同名文件当原 Artifact。此内部命令不授予写权限；[`document_provider`](../../SKM/backend/src/skillmind/effects/document_provider.py) 通过[成果台账与发布事务](document-lifecycle.md#run-成果的保存与发布)取得一次发送结果，随后调用锁外存储客户端。共享 Effect 工厂仅在独立内部门禁及明确的 Service/存储客户端同时提供时注册，缺少依赖直接拒绝，不要求虚构的 Integration Secret。Worker 装配复用现有 MinIO 设置与文档上传限额，启动时将 writer 的规范化 endpoint、bucket 和 namespace 与同一 FileStorage 比对，未绑定或不一致直接拒绝；未开放时不构造 writer。Outbox、job 与恢复入口使用同一配备能力判断，普通 dispatch 开关仍控制新执行。

[`s3_effect`](../../SKM/backend/src/skillmind/storage/s3_effect.py) 提供独立低层客户端，不改变现有文档上传的 UNCONDITIONAL_V1 行为。单次调用只发一个签名覆盖 `If-None-Match: *` 和原请求 metadata 的 PUT，不走 multipart、自动重试、redirect 或环境代理；固定已配置 namespace，正文限于 Artifact 上限，响应有界且整次网络操作限时。成功响应仍须 GET 核对原 metadata、MIME 和实际字节；有 Version ID 时读原版，不把 ETag 当内容摘要。412 只有核对出原请求才能返回原事实，同值但原 metadata 不同为冲突。

连接/提交响应丢失、409、服务拒绝、保存后失权或回读不一致均保留未知，不自动再 PUT 或补偿 DELETE。独立 lookup 只 GET；仅明确 NoSuchKey 或指定版本的 NoSuchVersion 表示本次读未检出，bucket 缺失、权限拒绝和代理 404 不算对象不存在。核对未检出不证明旧 PUT 停止；取消关闭本地 coroutine 也不证明远端无写入。

Provider 将获批的 `absent` 标为前置条件，不伪称已观察远端不存在；成功证据来自原对象回执及已提交的文档元数据。文档回读同时保留库内逻辑路径和原命令的 bucket/object key、原回执 version/ETag，经[续行回执](agent-runtime.md#agenttaskbrief)传给后续登记步骤；重放使用原保存事实，不重新查询当前对象或推测文档库 ID。条件 header 的[标准 S3 语义](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)不替代所部署 MinIO 的服务端保证；保护 marker、版本/权限隔离、迟到写和故障封闭要求仍见[文档存储边界](document-lifecycle.md#持久清理仍待补齐)。SQLite 事务、合成 HTTP 与本机 TCP 验证不证明真实 PostgreSQL 锁或 MinIO 的竞争、重建、故障行为；不得据此启用完整成果保存。

## 仓库写入模式

| write_mode | 目标与限制 |
| --- | --- |
| direct（默认） | Git 默认目标普通 fast-forward push；SVN 绑定 URL，不据此宣称精确 revision CAS |
| branch | skillmind/ 保留分支；SVN 为仓库根 branches/skillmind/，只应新建或证明重放原提交 |

Git branch 可经 forge 建 PR/MR，未配 forge 仅分支/commit；direct/SVN 不调 forge，PR 失败不撤 commit。

## 变更载体和冻结内容

提案冻结 binding、capability/operation、风险、Evidence、revision、预览、幂等身份。仓库只接受逐文件全文 SET/REMOVE，不执行 diff/Shell；Provider 使用独立可写副本，不从只读 input/ 提交。

### 契约与幂等身份

[提案契约](../../SKM/contracts/tools/change.propose/v1/request.schema.json)由 capability validator 收窄；[decision](../../SKM/backend/src/skillmind/api/routes/effects.py)携带 Idempotency-Key，内部 ClaimedEffectExecution 不暴露凭据/config/lease。

提案/决定/Run 创建键分开；决定重放须原 key/proposal/version/checksum/decision/reason，远端重试沿原 Proposal/Effect，不随 Worker 换键。[旧 repository.write Schema](../../SKM/contracts/tools/repository.write/v1/request.schema.json)仍限 skillmind/ branch，非 Effect Worker 输入/direct 示例；须审查版本/历史再同步，不据此开放直接 write。

## 并发、重试和失败

| 情况 | 当前处理及限制 |
| --- | --- |
| 决策版本/checksum 不符 | 拒绝旧预览，重新读取 |
| 目标陈旧/冲突 | STALE/target_stale 或 FAILED/target_branch_conflict，不改已批准内容 |
| 可重试 transport 错误 | 记错后 REQUESTED/Outbox 重进原 Provider；次数耗尽、取消、过期不自动继续，已 claim 的存储写入保留待核对状态 |
| 回读不匹配 | VERIFICATION_FAILED，不自动重试；外部可能已变，证据未必保存 |
| 回读断连/commit 后 PR 失败 | 按 transport 分类；暂无持久“commit 完成待 PR”阶段 |
| claim 前取消 | 拒绝 Provider；claim 后仍有窗口 |
| Provider 中取消 | PostgreSQL/文档保存的阶段复验与心跳会停止失权的本地执行；已返回的结果仍由有效 lease finalize 保存，不做逆向写入。未知结果的取消后核对待补 |

reversible/rollback 不提供回滚操作；撤回须验真实 revision 再走新修正流程。禁止 force、自动合并、自批、repository 免审、跨仓库原子写及无 scope 更新。

### 写入未知时停止主处理

PostgreSQL 与文档保存一旦交付 claim，失败分类、lease 到期或当前未查到结果都不足以证明未写入。未取得原 Provider 的完整成功结果时，Effect 的 `error.code` 使用 `effect_result_unknown`，`cause_code` 保留原分类，`reason_code` 表示此次停止/恢复原因，`retryable` 仅控制同一 Effect 的技术重试。首次 claim 前的取消仍是未开始；旧无标记记录不回填，在实际恢复时保留原错误码并补充未知语义。旧 Provider 的失败语义不因此改变。

取消、批准过期、次数耗尽或不可重试失败停止已 claim 的存储写入时，保留原 Effect/台账/回执，Tool 与事件同样标记待核对，Run 以 FAILED 或 CANCELLED 停止并保存原 Effect ID；不生成新 Segment 或自动模型续行。Run 已离开等待状态的迟到回收只更新原 Effect/Tool，历史详情重新读取可见；不改写原 Run 终局，也不在终态快照后追加 Run 事件。执行状态的 FAILED/STALE 不能证明远端失败；页面在折叠审计之外显示待核对，结果核验也拒绝将此标记匹配成确定的失败摘要。

获准技术重试仍沿原 Effect 进入其回执协议；原 Provider 确认成功后保存 Evidence、清除未知并按 APPLIED 续行。停止后的人工处置及接续入口尚未接齐；当前待核对标记是保留事实和阻止自动重写，不是核对已经完成。不要通过新建 Run、换 ID、再次批准或删除台账绕过。

[`reconciliation_service`](../../SKM/backend/src/skillmind/effects/reconciliation_service.py) 已提供 Worker 内部只读单元：根据原 Effect/提案/批准与冻结 binding 重建同一请求，文档与写入共用 Artifact 字节和路径构造，数据库复用原 binding/Secret 解析。每次外部读取前后复验当前核对者的账户、原会话和项目成员关系；归档项目仍可按当前读权限核对，不借用已过期的写入 lease、旧批准者会话或部署写入开关。所有数据库锁在外部 I/O 前释放，30 秒期限触发本地取消并等待资源清理；撤权、目标或凭据改变会拒绝返回观测。

该单元只调用原回执 READ ONLY SELECT 或原对象 GET，返回 CONFIRMED、NOT_OBSERVED 或 CONFLICT；数据库事务确认与对象确认分开，对象已确认也不表示文档已发布。读取异常/超时不返回确认结果，未查到不证明旧请求已停止。它不 claim、续租、写入、发布文档或修改 Effect/Run。

0047 的独立核对台账与 [`reconciliation_request_service`](../../SKM/backend/src/skillmind/effects/reconciliation_request_service.py) 在当前会话、CSRF 和项目读权限的同一事务内保存原会话、目标摘要和仅含请求 ID 的 Outbox；API 层不解析业务凭据。每个 Effect 最多一个 QUEUED/RUNNING 请求，重复受理只接受同一请求与原会话；当前项目读权限可查询历史，不换发原 owner。查询仅认领一次，60 秒 owner 期限内保存原观测；重复完成只接受相同 owner 和同一观测，旧 Worker 不能改写已关闭记录。SUCCEEDED 表示查询完成，不将 Effect 改为 APPLIED；查询失败后可另建只读请求，原写入身份和未知状态保持不变。观测与 Run 事件分开保存，降级拒绝删除任何核对历史。

[`reconciliation_execution`](../../SKM/backend/src/skillmind/effects/reconciliation_execution.py) 与 Worker 装配、Outbox、job 和期限回收已连接，原会话与目标只从持久要求取得。只有认领提交已确认才开始查询，读取前后校验受理目标和当前 owner，观测提交前及 flush 后重验会话、目标与期限；保存后响应丢失保留原观测。只读调度受 Worker dispatch 开关控制，独立于外部写入开关；已有文档库 client 的 lookup 共用当前存储归属。认领最多等待 15 秒，随后执行器 45 秒内完成读与保存；读取单元仍受 30 秒期限约束，停止记录最多尝试 5 秒，数据库不可用则留待回收。这些本地期限不证明远端旧写入停止。RUNNING 超过 owner 期限、QUEUED 超过 30 分钟均只关闭核对请求，不重发查询或写入，避免丢失队列任务永久占用核对位置。

[`effects` HTTP 入口](../../SKM/backend/src/skillmind/api/routes/effects.py) 与结果页已接入：POST 仅接收原请求 UUID，复验会话、Origin/CSRF 与当前项目读权限，并在 Worker dispatch 关闭时拒绝受理；GET 可按原请求确认或只取指定 Effect 的最新核对。两种读取都重验当前权限，归档不妨碍核对。响应采用[明确字段](../../SKM/contracts/effects/reconciliation/v1.schema.json)，不返回内部 receipt、会话、owner、目标配置或凭据。

页面在未知写入提示处显示核对操作与最新观测，区分查询完成和写入成功、未检出和未写入、对象确认和文档发布。提交前仅把原 UUID 与非敏感归属保存到当前 tab；响应丢失或页面刷新后先用原 ID 确认，未检出仍保留同一请求，重试不换 ID。正在查询时只刷新观察结果，账户/会话/目标切换后丢弃旧响应；这不表示旧查询或写入已在远端停止。核对结果不会清除原写入未知标记或自动接续 Run，人工处置与实际环境恢复仍待完成。

## 可靠性修正要求

以下区分当前保护与剩余目标，不因 Provider 存在而视为完成。

### 执行身份与远端前置条件

当前 Git/SVN matches 只比变更路径内容，Redmine 不同 revision 同值也可报 replayed；同内容/同名 branch 不证明原执行。

目标重放须证原 Effect/请求、目标、批准基线、实际 revision/变更集；已知未执行但基线失效拒绝 stale，未知先核对。Git direct/branch 须在远端更新点校验精确旧 ref/不存在条件，GET/本地锁不足，仍禁 force。

SVN 当前 checkout 未固定 revision，回读取事后值；须固定 direct/branch 基线、提交回执并按对应 revision 回读。out-of-date 不保护 checkout 前变化，旧记录不补造回执。

### 阶段回执与不确定结果

当前 Provider 完整返回才存 before/after：forge 失败会丢 commit 返回，SVN copy/文件 commit 是两次写；首个同 source open PR 未完整验 target/提案/并发。

目标同 Effect 追加调用前身份、写后回执、回读，区分未执行/已执行待验证或 PR/未知。恢复查原身份、仅补未发生阶段；PR/MR 验仓库/source/target/提案，分别处理关闭/合并/并发/丢响应。

新载体未冻结，须同步存储/Provider/事件/消费者。旧空值不证明未执行，不靠改 SQL、删 Effect、换 key 修复；阶段记录不消除 DB/远端断连窗口。

### 执行权与取消

claim 在锁及校验等待后按新时间建立 lease，期限不超过原批准到期时间。PostgreSQL 与文档保存 Provider 通过共享 catalog 声明阶段授权，Worker 对其执行及 finalize 维持同一心跳：默认间隔为 lease 时长的三分之一，每次以共享阶段授权重验当前账户、项目、取消、原批准和完整 claim，提交确认后才采用新期限；已过期 lease 不可续活。续租请求卡住时也受上次确认的租约剩余时间限制。

这两个 Provider 的本地监督默认在 300 秒发出超时取消；续租失败或上层取消也停止所有权内的 coroutine，并等待清理。清理耗时不保证有界，本地取消不证明远端请求已停止或未写入。监督失败不伪造 finalize 结果，原 Effect、台账与回执保留供恢复核对；finalize 已返回后迟到的心跳拒绝不覆盖原结果。旧 Redmine/Git/SVN 路径仍无此贯穿监督，不能因共享 claim 的计时修正而开放。

每个新外部步骤仍须重验批准、取消与执行权。失权/超时后的只读核对必须与新写分开，第二 Worker 不直接重写；停止后的接续缺口见[写入未知时停止主处理](#写入未知时停止主处理)。真实数据库等待、断连和远端迟到执行仍需验收。

## 验收

覆盖决定丢响应/同键改 reason、同内容不同提案、预读后目标变化、SVN copy 中断及固定 revision 回读、commit 后 PR 超时/并发、Provider 超 lease/取消/旧 Worker 晚到、finalize 响应未知。原身份不重建，不误认他人提交，部分成功可追溯。

入口：[Provider](../../SKM/backend/tests/effects/)、[Worker](../../SKM/backend/tests/worker/test_effect_executor.py)。真实 DB/远端/forge/CAS 与[审批 UI](workspace.md#审批请求与执行结果)分别验收。
