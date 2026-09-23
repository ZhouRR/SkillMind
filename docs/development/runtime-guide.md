# 运行与数据维护指南

本页保留维护现有链路时必须守住的边界；详细字段、状态和实现以代码与 [contracts](../../SKM/contracts/) 为准，能力进度见 [Roadmap](../planning/roadmap.md)。

## 身份与授权

- 入口复用 [actor dependency](../../SKM/backend/src/skillmind/api/auth_dependencies.py)：Cookie 写请求同时检查 Origin/CSRF；显式 API Key header 验证原 Key，重复、混用或无效时拒绝，不回退 Cookie。越权与不存在统一为 404，归档项目只开放获授权读取。
- API Key 当前授予创建者所在组织的 ADMIN 资源权限，没有逐 Key scope；创建者须保持有效 ADMIN。不能借 Key 绕过项目状态、冻结权限、部署开关或批准。
- 入口认证与业务提交分属不同事务。涉及持久写入时复用 [UserAccess](../../SKM/backend/src/skillmind/users/access.py)，在业务锁等待后及最终提交检查原资格、当前成员与项目状态；后台执行按持久发起人、批准与原 claim 复核，不能伪造浏览器会话。
- Run 冻结的是权限上限，后来的授权不能补进旧 Run；当前撤权仍须生效。凭据停用、业务取消、本地连接关闭分别处理，不据此宣称远端进程已停止。
- Secret 只经 [resolver](../../SKM/backend/src/skillmind/integrations/secrets.py)交给受控 Provider，不进入 Brief、日志、Evidence 或业务工作区。ENVIRONMENT/FILE 保存引用，MANAGED 使用与项目及引用绑定的[加密实现](../../SKM/backend/src/skillmind/core/secret_crypto.py)；缺 key、篡改或解析失败必须拒绝。轮换与恢复按[运行手册](../operations/runbook.md)和[一致恢复点](../operations/backup-recovery.md#一致恢复点包含什么)执行，数据库与原 keyring 缺一不可。

## Skill 与冻结原文

- Skill 是 Organization 资产，发布后由 Project 显式启用精确版本。沿 [SkillService](../../SKM/backend/src/skillmind/skills/service.py)处理导入、发布、启停与引用检查，不覆盖已发布内容或绕开现存引用删除版本。
- 新导入只提取输入、资源及操作的最小声明，并形成一个执行任务；完整冻结原文进入 Brief，业务顺序与条件由 Agent 按原文执行。入口为 [direct_candidate](../../SKM/backend/src/skillmind/skills/direct_candidate.py)、[execution](../../SKM/backend/src/skillmind/skills/execution.py)及 [task_brief](../../SKM/backend/src/skillmind/agent/task_brief.py)。旧 Blueprint 保留原值，不从 Manifest 逆算，不在续行时升级历史策略。
- 来源、脚本和模型建议不是权限。Tool 由 [catalog](../../SKM/backend/src/skillmind/agent/tool_catalog.py)统一装配契约和 Provider，经 [ContextBuilder](../../SKM/backend/src/skillmind/agent/context_builder.py)及 [ToolExecutionPolicy](../../SKM/backend/src/skillmind/agent/tool_policy.py)核验冻结绑定、当前开关和调用权限；不按相近名字放行未知能力。SDK builtin 保持拒绝，Shell/网络/文件不能绕过 Gateway，改变 cwd 不构成隔离。执行脚本须核对发布 checksum，业务专用 Schema、seed 和 renderer 不进入通用层。
- 异步解释保留原请求、来源 hash、模型配置和调用事实；超时、取消或响应未知不能靠换 ID 重新生成。修改解释流程先核对 [request_service](../../SKM/backend/src/skillmind/skills/request_service.py)与消费者，模型质量另用固定输入和独立预期验收。
- Codex 模型信息通过固定 CLI 获取：内置目录缺少所选模型或思考强度时，用专用登录和代理配置执行官方模型发现；保留返回的模型能力，仅覆盖平台工具限制。未找到或读取失败时明确报错，不猜参数、不换模型或降低思考强度；登录本身不依赖模型目录就绪。

## 输入与文档

- 冻结精确版本、资源选择、文档 ID/hash 与绑定。新增文件、同名重传或配置变化不改变原 Run；缺失或损坏不能用当前文件补齐。物化与复用沿 [workspace_materializer](../../SKM/backend/src/skillmind/agent/workspace_materializer.py)和 [input_snapshot](../../SKM/backend/src/skillmind/runs/input_snapshot.py)，校验原回执及完整输入树；`input/` 只读，写入限 `workspace/`、`output/`。
- 选择文档、读取正文、转换与保存成果是独立能力。按需准备仅冻结清单，后续工具核验原内容；文档观察不等于取得正文，MCP 服务的只读标注不等于授权。能力与 Provider 是否可用，以 [Worker 装配](../../SKM/backend/src/skillmind/worker/settings.py)及冻结 binding 为准。
- 上传按原 actor/Project、幂等键和内容确认；数据库提交与对象 PUT 不原子。沿 [DocumentService](../../SKM/backend/src/skillmind/documents/service.py)保留预约、发布/关闭回执及原存储归属；关闭发布不证明 PUT 停止，未知不自动重传、删对象或退配额。
- 改名/移动只改变展示路径，保留 ID、原字节、存储引用和原上传回执；清理时校验不变身份，不能要求当前路径仍等于上传路径。回收站仍占用存储；完全删除先检查回收状态、生成执行已结束、无未决操作及其他引用。[引用查询](../../SKM/backend/src/skillmind/documents/reference_repository.py)覆盖 Run、Schedule、occurrence，未知历史拒绝删除。
- 删除须在同一事务保存原对象清理要求，再在确认提交后尝试删 blob。204、目录消失或一次读不到对象都不证明全部版本、在途 PUT 和配额已结清；持久清理重试与结算仍待补齐。执行履历 purge 沿 [history_purge](../../SKM/backend/src/skillmind/runs/history_purge.py)，保留共享成果与最小删除审计，不删除外部业务数据或重做操作。

Excel→Markdown 的 `.xlsx` 路径使用 `excel-styles/v1`：同一文档保留原行列、静态整格/局部删除线和任意填充色，重复样式按实际连续范围合并。色值保留原 RGB/theme/indexed/tint 表示；无法解色不假定白色。条件格式与表格样式只标记范围和未求值状态，不据此自动排除业务步骤。合并区域记录 anchor 与范围，不展开复制正文。旧 `.xls` 仍为值转换，并在保存的 Markdown 明示未检查样式。转换 profile 写入 Evidence，原文件/冻结版本、Artifact 原字节校验及既有限制不变。

## 执行与恢复

- 按[架构与术语](../overview/architecture.md)区分 Run、Segment 和 Attempt；生产入口为 [worker/settings](../../SKM/backend/src/skillmind/worker/settings.py)与 [executor](../../SKM/backend/src/skillmind/worker/executor.py)。同阶段技术恢复使用新 Attempt，不替换 Run，不改原快照、权限上限或 Result。
- 提交前冻结原身份、幂等键、输入、资源选择与批准同意；同键只确认同一内容。复用 [creation_request](../../SKM/backend/src/skillmind/runs/creation_request.py)、[creation_replay](../../SKM/backend/src/skillmind/runs/creation_replay.py)与 Web [runSubmission](../../SKM/web/src/lib/runSubmission.ts)，重放不读取当前资源重建快照。
- 断网、超时、abort、提交异常或损坏的成功响应均可能已经落库。保留原请求，先查询原回执；404 仅表示本次未见。协议允许显式重发时仍用原键、原内容和相同业务身份，不自动换键；新会话可按接口规则查询原事实，不能接管旧在途写入。晚到响应必须隔离于新 actor/Project/Run。
- 取消先持久保存意图；[RunRepository](../../SKM/backend/src/skillmind/runs/repository.py)在原 Run/Segment/Attempt 锁内核验 lease、取消和终态。旧 Worker 失去 lease 后不得补写成功或失败；Tool 登记、调用与保存沿 [evidence](../../SKM/backend/src/skillmind/agent/evidence.py)复核原执行权，未决调用不重新发放执行许可，成功重放只返回可核实的原回执。
- 接受取消、业务终态、SDK 清理返回和远端停止分别核对；保留协作取消与清理，本地退出不能证明远端停止或作为退额依据。
- 模型/会话恢复沿原记录与 [session_store](../../SKM/backend/src/skillmind/agent/session_store.py)，缺失依据时停止；不补造 transcript、自动改模型或从头重跑。定期执行同样保留原 occurrence 与创建身份，入口见 [schedules/service](../../SKM/backend/src/skillmind/schedules/service.py)。

## 外部效果

- 外部写入沿 observe → propose → approve → apply；Agent 提案不等于远端执行。批准绑定精确 Proposal version/checksum、Provider、操作与 scope；[EffectService](../../SKM/backend/src/skillmind/effects/service.py)与独立 Worker 核对当前资格、冻结 binding、lease、取消和期限，不在业务锁内等待远端 I/O。
- 自动批准只能来自原 Run 启动时明确记录的同意，或适用能力的既有预授权规则；Git/MCP 的额外同意不从旧 Run 补推。`repository.write/v1` 禁止 Project 预授权与 force，Git 仍核对精确 ref 和原提交，不把启动同意当作任意仓库写权限。
- 实际能力由 [ExecutionFeatures](../../SKM/backend/src/skillmind/effects/release.py)与 [Provider registry](../../SKM/backend/src/skillmind/effects/wiring.py)共同限定：PostgreSQL INSERT/UPDATE、文档 CREATE、Git commit、MCP 调用各有独立开关；开启一项不开放其余。旧队列也须检查当前开关，历史读取与获授权的原结果核对保持独立。
- 写入前观察、并发冲突检查、原操作幂等和 read-back 均须保留。原回执证明原操作；当前同名、同值或当前画面不能代替原回执。数据库、对象存储、仓库、MCP 与平台数据库之间没有统一原子事务。
- 写入结果未知时停止主处理，保留原 Effect 与已发生事实；失败状态不证明未写入，不据此重做写入或补偿删除。原结果只读核对走 [reconciliation_service](../../SKM/backend/src/skillmind/effects/reconciliation_service.py)。取消、lease 到期或核对未检出都不证明未执行；MCP 的回读重试不能重发修改操作，入口见 [mcp_provider](../../SKM/backend/src/skillmind/effects/mcp_provider.py)。

## 结果与评价

- 技术终态、结果完整程度与业务 PASS/FAIL 分开表达。结果通过 [ResultValidator](../../SKM/backend/src/skillmind/agent/result_validation.py)校验冻结 Schema、引用归属、附件字节及保存的 Effect 事实；模型自称成功不构成 APPLIED，结构有效不证明业务正确。
- Result/Evidence/Artifact 保留原值。人工修正追加 [Evaluation](../../SKM/backend/src/skillmind/evaluations/service.py)，不覆盖结果或自动采用建议；重复提交和未知响应沿原评价身份确认。
- 附件只能发布经工具审计确认的原字节，下载核验归属和 hash。`audit.export/v1` 导出已存事实，不重新执行操作，不把通用审计 JSON 当作 Skill 规定的业务成果；入口见 [audit_export](../../SKM/backend/src/skillmind/agent/audit_export.py)。
- 报告与预览按 [UI 指南](../design/workspace.md#报告与预览)展示；模板不替代 Skill 规定的业务成果，展示故障不重跑模型或改变业务状态。

## 后置能力

按生产接线和实际证据判断能力，不因存在数据表、开关或测试就扩大默认承诺。仅在真实需求涉及下列能力时核对，不把它们作为日常任务的新前置条件。

| 范围 | 当前维护边界 |
| --- | --- |
| Run 统一预算 | [primary_budget](../../SKM/backend/src/skillmind/worker/primary_budget.py)尚未接入生产 coordinator；Codex 启动前拒绝美元限额，局部 turns/output/timeout 不等于完整计费上限。接入时须主子共享、收费前预留、可信计量，未知费用不退额。 |
| 子 Agent | [Provider](../../SKM/backend/src/skillmind/agent/subagent_provider.py)仅局部实现，共享消费、停止与审计恢复未闭合；权限经 [resolve_subagent_capabilities](../../SKM/backend/src/skillmind/agent/subagent.py)收窄，禁止写入、交互和递归 dispatch。 |
| 生成 UI | [Manifest](../../SKM/contracts/runtime-manifest/v1alpha1.schema.json)仍不允许 frontend_module，builder/Host 未接。未来开放须验证隔离：bundle 使用 CSP sandbox allow-scripts，iframe 禁止 allow-same-origin；静态报告不算生成应用。 |
| 外部写操作序列 | [局部实验](../../SKM/backend/tests/worker/test_receipt_sequence_experiment.py)仍未接入生产。已接入的同 job SDK 复用和本地/只读短序列见下节；它们不等于取消逐次 Effect/Segment，也不执行批量外部写入。 |
| 其他扩展 | Task Flow 只读预览；完整编排、SVN/Redmine 写入交付、PR 自动创建及更广的 Shell/network 能力按实际需求明确权限、恢复与验收范围。 |

## 有限工具序列与暖续行

本地/只读短序列由 `agent/tool_sequence.py` 经原 `ToolGateway` 执行。注册项必须显式 `sequence_safe`，子工具仍须在原 Run 冻结集合中；改变注册不扩大历史权限。取消传播、未决不重放，外部写入与控制工具不能进入此序列。

Codex 的 `agent/warm_codex.py` 只在同一 Worker job 的原 Run 内复用 transport，绝不复用 Tool 权限或跨用户会话。`claim_for_execution(expected_previous=...)` 在原 Run 锁内校验相邻 Segment、原 Effect 和自动批准，仍经普通 claim 创建新 Attempt。回收及跨 Worker 处理保留原 Outbox。可选的直接回执见下节，它与暖续行分别验收；多写操作序列仍未交付。

## 自动批准操作的直接回执

`SKILLMIND_INLINE_EFFECTS_ENABLED` 默认关闭；数据库迁移 `0056_inline_effect_owner` 随正常部署执行后，可在隔离验证通过的 Codex Worker 上启用。未启用、无原 Run 自动批准同意、不支持分阶段授权或要求非 RESUME 的操作保留原延期路径，不新增用户确认。Claude 继续使用原延期方式。

`change.propose/v1` 保留业务目标、修改、前提、说明与依据；管理字段可省略。平台按原 SDK 调用身份生成幂等键（不按内容去重），补充风险下限、回读路径和默认恢复字段；显式业务条件不覆盖。提案、批准、Effect 与原 ToolCall 仍逐项保存。

启用时，当前原 Attempt 保持 RUNNING，通过共享 `ApprovedEffectExecutor` 认领并核验自动批准操作，只有原回执提交后才作为当前工具结果返回。不跳过权限、lease、读回或业务检查，也不承诺外部操作恰好一次。数据层用原 Attempt 反向引用和提案中的内部交付身份防止重复认领。

失败、取消、未知响应或 Worker 中断时保留原 ID：停止原 native turn 后迁回原 Effect 等待/核对流程；恢复不能创建一个新写操作。原 Agent lease 失效时，新的外部阶段不可沿旧权继续；回执已确认不等于业务 PASS。两次相同参数但不同 SDK 调用仍是两次独立意图。

直接回执只在原 Attempt 剩余时间足够容纳既有 Effect 监督上限与交付余量时尝试；不足时在创建操作前走原延期路径，不延长或重置期限，也不因开启优化把长任务强行塞进一个 Attempt。

## 同一任务的同类多资源

Run 可按 Skill 的不同资源槽位绑定多个同类连接。`database.*`、`mcp.*`、`repository.read` 和 `issue.read` 的顶层 `resource_key` 选择已经冻结的槽位；同一能力有多个绑定时必须明确选择，只有一个时可省略。平台不接受通过 selector 提供 URL、Integration ID 或凭据，也不按表名、远端工具名或路径猜连接。

SDK 每个能力仍只公开一个工具定义，并列出当前可选 key；内部执行与审计按 `(capability, resource_key)` 保留原 binding、当前授权和原调用身份。Provider 只收到去除顶层 selector 后的业务参数，嵌套的同名业务字段保留。`tool.sequence` 的每一步在 `arguments.resource_key` 中选择资源；全部静态选择先验证，子调用继续独立审计、计量及检查撤权。写入仍通过原 `change.propose.resource_key` 与 Effect，不把读取、目录发现或其他连接的证据当作目标写权限。

新增能力无需复制 Provider；接入原工具注册与资源声明即可复用路由。扩充项目连接或更新配置不补进已冻结 Run。上线后以新建 Run 验证至少两个数据库、两个 MCP 服务或两个仓库的准确选择、相同表名/工具名/path 的隔离、原回执重放及缺失 selector 的明确错误；测试替身不能代替真实多连接验收。
