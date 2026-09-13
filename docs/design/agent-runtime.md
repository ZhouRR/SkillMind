# Agent Runtime 与持续执行

本页负责已有 Run 的执行、会话与事件协议。创建身份见[Run 创建](run-creation.md)，输入一致性见[资源快照](resource-snapshots.md)，状态与剩余工作见[计划](../planning/roadmap.md#开发任务)。

## 设计结论

Run 是持续业务线程，不是一次 SDK query、Session 或进程。默认引擎为 Codex SDK，可通过部署配置切换为 Claude Agent SDK；业务层只依赖 AgentEngine。平台控制资源、权限、交互、效果与终态，PostgreSQL 保存审计正本。模型和思考强度按配置原值传递，失败不自动切换引擎、模型或降低强度。

## 责任边界

| 责任方 | 负责什么 |
| --- | --- |
| Skillmind | 从发布版本的 Blueprint 生成任务/Brief，冻结资源与权限；管理 lease、恢复、预算、交互、效果、证据、结果与事件 |
| AgentEngine | 在 Brief 内规划、选择允许的 Tool、维持会话，产生公开文本、用量、交互和结果候选 |
| Agent 无权决定 | 扩大 Project/scope、取得 Secret、增加 Tool、自批提案、绕过限额/校验或直接调用未注册 Provider |

Skill、Ticket、代码或模型建议都是输入，不是授权。业务规则不放进 route、queue job 或 SDK adapter。

## AgentEngine 抽象

[AgentEngine](../../SKM/backend/src/skillmind/agent/domain.py)提供 execute / resume / fork / interrupt / health；前三者返回异步事件迭代器，Adapter 将 SDK message 映射为 AgentEvent，不泄漏 SDK 类型。

每 Run 只允许一个活动 PRIMARY；SUBAGENT/BRANCH 共用父 RunAttempt，汇总到主 Session 的 ToolCall/Evidence，不另分 RunEvent sequence。详见[子分析](subagents.md)。

### Codex Adapter

Python SDK 与捆绑 CLI 固定为 0.154.0，启动时校验安装版本与 CLI 的 RECORD checksum。默认模型为 `gpt-5.6-terra`、思考强度 `max`；解释器与 Run 使用同一引擎选择。Worker 的专用持久目录保存 device login 与原生会话，不复用宿主 Codex 配置、登录或 Skills。操作入口见[部署配置](../operations/deployment.md#agent-sdk-与-device-code-登录)。

解释器与 Run 共用 Codex 输出适配：为 `const`/`enum` 补齐类型，基本联合类型直接传输，可选 nullable 字段用存在标记区分省略与显式 null；自由 JSON、复合类型和深层结构以 JSON 字符串传输，并保留对象/数组类型约束，返回后恢复原值。原始 Schema、系统 Skill 身份及业务校验不变，传输格式不作为发布或执行通过的依据。恢复失败按无效候选处理，解释器沿用一次修复上限。Provider 错误只记录允许的分类和 SDK 实际返回的 HTTP 状态，不保存错误正文或请求信息。

原生 shell、文件写入、浏览器、应用、插件和子 Agent 不开放。由于模型目录中的 Tool 模式可覆盖普通功能开关，Adapter 从固定 CLI 的内置模型目录派生平台配置，仅收窄 Tool 路由与附加能力；保留模型 ID、思考等级与上下文参数。模型的资源调用只连接本进程随机地址的 loopback MCP，逐次经过共享 ToolExecutionPolicy、授权 Gateway、Provider 校验和证据审计。原生批准请求拒绝；用户交互与外部写入仍走平台注册 Tool。

`change.propose/v1` 和 `interaction.request/v1` 保存原调用后停止原生 turn，再交 Worker 持久化等待状态，不在模型进程执行效果。续行核对 Run、模型、原生 Session 和原请求回执，恢复同一原生会话，并传入当前冻结 Brief；新提案仍暂停。PostgreSQL 保存平台开始/暂停/终止记录与业务审计，Codex 专用卷保存原生历史。任一恢复依据缺失时失败，不拼造历史或隐式从头运行。下文 Claude 的 deferred replay 机制仅适用于 Claude Adapter。

Codex 支持输出字节与 Tool 调用次数边界及原生取消，取消完成须等待所属 MCP 服务关闭。当前未接通美元预算预留/结算，带 `max_budget_usd` 或已准备计费调用的请求在启动模型前拒绝。局部消息/Tool 次数限制不等于完整 Run 的模型计费 turn 上限；共享预算与完整用量结算仍按[预算门禁](run-budgets.md)管理。

## AgentTaskBrief

每个 Segment 启动前冻结一份 [AgentTaskBrief](../../SKM/contracts/agent-task-brief/v1.schema.json)：

| 内容 | 必须说明什么 |
| --- | --- |
| 身份与目标 | Run、Project、Segment、Task、精确 SkillVersion/checksum，目标和成功条件 |
| 指导与资源 | Skill 必需规则、建议步骤、质量要求与禁止事项；合法 binding、来源与路径 |
| 策略 | capability/参数/scope、限额、交互和效果批准边界 |
| 输出与续行 | 交付要求、可选 Schema、checkpoint、已确认事实及 Evidence/Proposal 引用 |

Brief 保留必需指导，不注入 Integration 凭据、运行连接配置或跨 Project 数据；已绑定项目文档库的最小登记引用遵循[资源投影](resource-snapshots.md#公开选择与读取投影的实施契约)。Checkpoint 可压缩上下文，但须保留原 transcript/source trace，不改用户事实或批准范围。

新版 Manifest 的 `source_documents` 原样进入 Brief/checksum，并在初次执行和每次续行的共享提示中完整渲染；Codex 与 Claude 使用同一入口。先验正文 SHA-256 和导入字节上限，损坏或超限拒绝，不悄悄摘要或截断。原文作为来源材料保留，业务标识符不得猜测、改名或复数化；解析指导与原文冲突时停止并报告。原文中的命令、脚本、连接描述和工具声明不产生执行权，仍只能用冻结绑定和已注册工具，通过原审批/效果协议执行。

新 Run 默认遵守 Skill 的新业务执行规则，不能因路径或日期相同就沿用历史业务 ID。只有冻结用户输入明确指定恢复对象且 Skill 支持业务恢复时，才按当前 Project 和授权资源核对原记录并复用业务 ID；剩余写入仍在当前 Run 重新观察、提案和批准。业务恢复不继承旧 Run 的 Effect，也不能用旧回执满足当前 Run 的文档前置条件。

使用 OutcomeEnvelope 的任务在提交最终结果前，由共享提示要求 Agent 生成一份自包含 HTML 报告，放入 `kind=report` 交付项的 `content`。报告按请求语言呈现结论、已核实汇总、明细、证据与限制；区分业务判定和执行状态，不补造数量或成功效果。内嵌 CSS 可组织版面，禁止脚本、外部资源和导航。该展示要求不替代 Skill 的业务 JSON、成果保存或原效果回读，不引入额外外部写入，也不改变模型配置；自定义输出 Schema 不强加 HTML 字段。

Effect 成功确认为 APPLIED 时，同事务生成的下一 Segment checkpoint 带可选 `effect_result`：原 Effect/Proposal、before/after Evidence 引用、after 摘要、完整回读内容和 verification。Brief 保留并渲染这份原执行事实，不靠模型从引用或批准内容重建返回值；正文作为数据，不作为指令、当前外部状态或新写入权限。最终 effects 的引用须来自对应原回执，不能用提案前的查询 Evidence 替代 before_ref；旧回执缺少该可选字段时保持原值并省略摘要中的 before_ref，仍由结果校验核对原执行的两份证据。对象保存回执可含后续登记所需的 bucket/key/version/ETag，不含连接 endpoint 或凭据。该字段仅由平台 finalize 写入，模型的 propose/interaction checkpoint 不接受；失败续行不沿用上次回执，结果未知仍按效果协议停止。

回执采用规范 JSON、2 MiB 字节上限、原 Evidence hash 和共享敏感字段校验；超限或损坏拒绝交给 Agent，不截断为成功。每次仅携带本次回执，后续 checkpoint 通过既有事实和引用字段保存所需上下文。旧 checkpoint 缺少字段时保持原形，不补造历史值；无 summary 但有事实或引用时也必须渲染。API/Worker 须同步升级，旧 Worker 会丢弃新增字段，不能用于依赖返回值的完整业务验收。

`change.propose/v1` 暂停后的 RESUME 先只读核对当前 Segment 的 trigger、原 Proposal/Effect 或拒绝/过期记录，再以原请求 fingerprint、SDK Session 和 transcript 中的 tool ID 匹配原调用。只有这个已处理调用通过普通 Tool 审计读取结果；新提案仍 defer，不经该路径创建 Proposal 或调用外部写入 Provider。未知效果、缺失或不匹配的回执拒绝恢复。原 transcript 保持不变，旧 `{status: "success", deferred: true}` 响应形状继续有效。

固定 CLI 会在 deferred replay 后自动开始模型 turn，因此已处理调用返回 `deferred: false`、实际 outcome 和当前完整任务提示（含已冻结 Brief/回执），不再排队发送第二份 user prompt。原 tool_result 已保存的技术重试则使用通常 prompt 续行；缺少可核对的 transcript 不补造完成记录。此控制回复有独立 Evidence，仍受 Gateway 的敏感字段和大小限制。SDK 的 `tool_deferred_unavailable` 是引擎恢复失败，不能作为新提案再次入库。

## 自主执行等级

ExecutionProfile 合成策略，不表示各选项已有 Web 控件。

| Profile | 范围 |
| --- | --- |
| GUIDED | 优先显式步骤，关键歧义询问 |
| SUPERVISED | 默认；自主选择读取、验证和报告方式，业务取舍或外部效果前询问 |
| DELEGATED | 在明确预授权的资源/低风险效果内扩大自主循环，仍受硬拒绝限制 |

Skill 可推荐等级，system ADMIN 设置项目上限，发起人只能收窄；不存在 Project ADMIN。Agent 可调整分析顺序、交叉验证和生成产物；关键事实、业务选择或批准缺失、规则冲突时暂停。

原 scope 内补对象 ID/偏好可续行；改 Project、增加 Integration/文档/权限/预算须新建 Run。必需资源缺失拒绝创建，可选未选不在对话中隐式启用。

## Tool 与工作区边界

### Tool 分类

| 等级 | 边界 |
| --- | --- |
| observe | 参数校验后读取已绑定资源 |
| workspace | 仅在隔离 workspace/output 写临时文件、报告或补丁 |
| propose | 保存外部变更候选，不提交远端 |
| apply | 独立 Effect Worker 执行；默认人工批准，仅允许明确低风险预授权 |

能力必须匹配版本 ID 与注册 handler，凭据仅由 Provider 经 SecretReference 取得。读取、workspace、交互、提案、子分析与 Effect 的可用范围以 registry 和 [Tool 契约](../../SKM/contracts/tools/)共同判定，不按同名放行。

### 文件、Shell 和 Web

- SDK 内置 Read/Glob/Grep/Bash/Write/Edit/Web hard deny，受控能力由平台提供。
- input/ 冻结只读；workspace.write 只写 workspace/、output/，不覆盖文档库或外部目标。
- cwd 不是隔离；mount、路径、sandbox、Gateway/scope 共同拒绝 path/symlink 逃逸。
- Shell、sandbox command、通用网络未开放；须先定义威胁模型、允许命令/镜像/限额及注册网络 Provider，禁止暴露 Docker socket、Secret 和无限制网络。

### PreToolUse 判定

hook、Gateway、binding/Provider 和 Effect Worker 分担检查：注册 capability 与 Skill/Project/Run 权限、binding/scope/revision、参数与敏感字段、局部限额、apply 的批准。Run 累计限额仍待[共享预算](run-budgets.md)。

固定 CLI 的 `StructuredOutput` 单独作为结果传输通道：hook 按冻结的结果 Schema 校验，只解析本地 Schema 引用；不授予资源或文件操作权限，也不创建资源 Tool 授权/审计。它的内部调用不投影为资源 Tool 事件，用量、最终结果和失败仍保留。结果仍是候选，须由 Worker 完成[业务 Schema、引用与结果校验](results-evaluation.md)后才能提交成功；其他内置或未注册 Tool 不因这个通道获准。

兼容端点未返回 SDK structured output 时，Run 可将最终文本中的完整 JSON object 作为候选：允许整个对象被单个代码围栏包裹，或独立行对象前存在至多 4,096 字符且不含 JSON 容器符号、代码围栏的说明文字。带前言的路径记录为 `result_json_preamble_fallback`；不修复 JSON、不从说明中补字段，也不搜索嵌套对象来凑合通过。多个对象、数组、截断、后置正文或有歧义的前言均拒绝。候选仍经同一结果校验，历史失败 Run 不自动重写或重发外部操作；Skill 解释器的独立配置与解析规则保持不变。

Provider 前校验请求，返回后校验 Schema/敏感信息/大小；失败内容不交 Agent，只留脱敏审计，禁止 bypassPermissions。另按[调用提交与重放](run-supervision.md#tool-调用的提交与重放)验证原 Worker 权限：首次许可一次消费，未决/失败不重跑，成功只读重放。

### 资源快照的物化

repository 按冻结授权物化至 input/&lt;requirement_key&gt;/，打开时解析 revision；document 按创建清单映射至 input/documents/。全部根取得[可信准备回执](resource-snapshots.md#输入准备与可信缓存)后才交 Brief；后续只复用原世代，Tool 校验实际读取字节。分支名、公开清单都不能替代固定内容或授权。

## 持续 Run 与多会话

### 生命周期

```text
Run + Segment 1 + dispatch Outbox
  → Worker 准备输入 / 冻结 Brief → Session 执行
  → 持久化 checkpoint 与等待 → 普通答复或效果处理
  → Segment 2 + dispatch Outbox → 准备新的 Brief
  → resume / fork / replace → Result 与终态 RUN_SNAPSHOT
```

等待非终态，状态及 DEFERRED 见[速查](domain-model.md#run-状态速查)。答复先存 Segment/Outbox，后续 Worker 才准备并冻结 Brief。

### Segment、Attempt 与 Session 的区别

Segment 是新业务阶段，Attempt 是同段技术重试，Session 是模型上下文。故障不制造新决策，答复不算技术重试；均不得改变 Run 的 Skill、资源与权限上限。

### 会话延续策略

| 模式 | 条件 |
| --- | --- |
| resume | 引擎/模型兼容、目标未分叉，transcript/workspace 完整 |
| fork | 明确比较方案或保留分析分支 |
| replace | 上下文损坏、不兼容或需重压缩；新 Session 接收可审计 checkpoint |

保存 mode、parent、模型、checkpoint checksum；transcript 用于恢复，平台事件/ToolCall/Evidence/Interaction 用于审计。资料不完整时不得静默重跑。

### 从领取到模型启动的边界

当前 [Executor](../../SKM/backend/src/skillmind/worker/executor.py)按以下顺序执行：

```text
有效 claim → heartbeat / 取消监督
  → Run RUNNING → ContextBuilder（独立准备 timeout）
  → 验证 context 身份/sequence
  → 短事务冻结 Brief（重验 lease/取消）
  → 短事务启动校验（再验状态/lease/取消）
  → 事务外 execute / resume / fork
```

context 必须匹配 claim 的 Run/Attempt/Project/actor，sequence 由 repository 分配。按 Run → Segment → Attempt 取锁后，用当前时间和 DB lease 判定；网络、转换、模型调用在锁外。

RUNNING、输入 READY、Brief 冻结与 Session 活动分别证明状态推进、输入提交、指导固定和模型活动，均不证明成功。

### 取消、超时与失去执行权

取消意图、业务终态、进程退出与用量结清分别处理。等待拒绝和取消终态是两个事务，后者重新 fencing；阶段与恢复规则见[执行监督](run-supervision.md)。

## 用户交互协议

interaction.request 只接受 CLARIFICATION/CHOICE/REVIEW，答复在冻结范围内追加 Segment，不代替 Evaluation 或效果批准。期限、推荐、原作者确认及历史悬空批准统一按[普通交互](user-interactions.md)处理。

## 外部效果协议

observe → Evidence → propose → 精确批准/允许的预授权 → 独立 Effect Worker apply → read-back。Agent 不自批或直接写；repository.write 始终人工批准、不 force。冻结、审批身份、陈旧目标和部分成功恢复统一见[受控写入](repository-effects.md)，其可靠性缺口不由 Runtime lease 覆盖。

## 结果与输出验证

候选经 validator 后，终态事务再验 lease/取消；失败、取消可无 Result。结构、引用、业务正确性分别判断，见[结果与评价](results-evaluation.md)及[子输出责任](subagents.md#子任务指令与结果的边界)。

## 事件、状态与可靠性

- [RunEvent 契约](../../SKM/contracts/events/run-event/v1.schema.json)覆盖 Segment、Session、Interaction、checkpoint 与 Effect；Run 内 sequence 严格递增。
- 状态变更与 Outbox 同事务。Queue 重投由数据库领取抑制，Tool 重放与 Effect 幂等各用自身协议，不承诺模型/远端全局精确一次。
- 只有有效 lease 可提交；失效由 Recovery 接管，同段重试受 SKILLMIND_RUN_MAX_ATTEMPTS 限制。
- 终态 RUN_SNAPSHOT 是最后持久事件，SSE 据此结束。终态不可恢复，新目标创建新 Run。
- 模型 wall timeout 不含准备、排队、人工等待或全部 Attempt 累计；各计时器见[预算说明](run-budgets.md#现有计时器的覆盖范围)。

停写须覆盖所有 job/cron/实例，dispatch 开关不等于全局停止；见[发布边界](../operations/deployment.md)。

## Claude Agent SDK 实现要求

锁定 SDK/CLI 版本并记录模型/options checksum，SessionStore 镜像 transcript。Run 与 Skill 解释显式选用固定 SDK 的随包 CLI；[文件身份校验](../../SKM/backend/src/skillmind/agent/claude_build.py)核对安装归属及 RECORD 的大小/SHA-256，拒绝缺失、替换或系统 CLI 回退。配布目录在进程生命周期内须保持不可变；此校验不等于计量或停止证明。Hook 执行平台策略，SDK allowed tools 与 structured output 不替代参数和结果校验。人工等待须收束收费进程，不长期占 Worker。

升级或新增引擎先验消息映射、resume/fork/interrupt、旧 Session 恢复、Tool 拒绝/MCP、等待与错误分类；使用同一 conformance suite。

## 安全测试矩阵

覆盖指导/权限不漂移、缺资源拒绝、Segment/Attempt 分路、Session 续行、Tool/效果/路径与同 Run 引用边界、终态不可续行。监督按阶段观察 Run/Session/Result/Outbox；专项条件见各规则正本。

## 当前实现边界

执行入口已有接线，但完整资源/历史续行、持久停止证明与共享预算尚未闭合；逐项状态和验收登记在[计划](../planning/roadmap.md)。
