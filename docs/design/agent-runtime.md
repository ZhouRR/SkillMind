# ProjectMind Agent Runtime 与交互式执行规范

本文定义 ProjectMind 如何使用 Claude Agent SDK 作为默认执行引擎，同时通过 `AgentEngine` 保持模型/引擎可替换性；也定义 Agent 自主边界、持续多会话 Run、用户交互、Tool 权限、外部效果、证据和故障恢复。

## 1. 设计结论

- Claude Agent SDK 是默认引擎，不是业务层唯一可依赖的类型系统。
- ProjectMind 负责目标、资源、权限、交互、审计和终态；AgentEngine 负责模型循环、会话和工具选择。
- Run 是持续的业务执行线程，不等于一次 SDK query 或一个进程。
- Agent 在安全边界内自主规划步骤、选择证据和调整分析策略；平台不把 Skill 的建议步骤机械编译成固定工作流。
- 多轮用户交互和多个顺序 Session 可以属于同一非终态 Run。
- Skill 的写入意图不等于授权；外部变更必须经过提案、批准/预授权、幂等执行和回读验证。
- PostgreSQL 是 Run、Session、Interaction、Approval、Evidence 和 Result 的审计正本。

## 2. 责任边界

### 2.1 ProjectMind 负责

- 读取已发布 SkillVersion 中由 Interpreter 生成的 CapabilityBlueprint，再生成任务与 Brief 投影。
- 绑定 Project 资源，冻结 Run/Segment 的 AgentTaskBrief。
- 认证、Project 授权、Tool registry、Integration scope 和 Secret 使用。
- Worker lease、RunSegment、RunAttempt、SessionStore、恢复和取消。
- UserInteraction、ChangeProposal、批准策略和 EffectExecution。
- ToolCall、Evidence、Artifact、Result、Evaluation、RunEvent 和 Outbox。
- wall timeout、budget、max turns、保留策略和错误分类。

### 2.2 AgentEngine 负责

- 根据 AgentTaskBrief 规划和执行分析。
- 在允许的 Tool 集合中选择工具和参数。
- 维持/恢复模型会话，产生公开文本、工具请求、用量和结构化交互请求。
- 在证据不足或需要业务选择时请求用户输入。
- 生成 Outcome、Artifact 或 ChangeProposal candidate。

### 2.3 Agent 不能决定

- 增加 Tool、扩大 Integration scope、读取 Secret 或改变 Project。
- 把 Skill 文本、Ticket/代码内容或模型建议提升为平台权限。
- 直接批准自己的 ChangeProposal。
- 绕过 budget、timeout、审计、结果校验或终态规则。
- 在未注册 Provider 上执行外部写入。

## 3. AgentEngine 抽象

业务层只依赖稳定接口和平台事件：

```python
class AgentEngine(Protocol):
    def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]: ...
    def resume(self, context: ResumeContext) -> AsyncIterator[AgentEvent]: ...
    def fork(self, context: ForkContext) -> AsyncIterator[AgentEvent]: ...
    async def interrupt(self, session_ref: AgentSessionRef) -> None: ...
    async def health(self) -> EngineHealth: ...
```

以上为 [agent/domain.py](../../PJM/backend/src/projectmind/agent/domain.py) 的接口摘要；execute/resume/fork 返回异步迭代器，不是额外 await 后再迭代的 coroutine。

实现可以使用 Claude Agent SDK 的 query/resume/fork/session storage，但必须把 SDK message 映射为平台 `AgentEvent`。新增其他模型时通过 adapter 实现相同接口，不向 Run service 泄漏 provider-specific message。

一个 Run 同时只允许一个活动的**主** AgentSession（`session_kind = PRIMARY`）。多 Session 的顺序延续（`RESUME`/`FORK`/`REPLACE`）与并行子 Agent 是两件事，后者的资源、冲突与审计设计见[并行子分析](subagents.md)：

- 子 Session 记 `session_kind = SUBAGENT`、`continuation_mode = BRANCH`，与父**共用同一个 RunAttempt** 且可多本同时活动。`agent_sessions` 的两个一意约束因此按 `session_kind = 'PRIMARY'` 限定（migration `0027`）——限定而非解除，否则「一个 Attempt 有两个主 Session」也会通过。
- 子 Session **不各自写 RunEvent**。它们的活动收敛为主 Session 上的一次 ToolCall + Evidence，因此「Run 内 sequence 严格单调增」不需要改成分层序号，SSE 契约不变。子 Session 行是这一路唯一的追溯锚点。
- 子 Agent 永远只读，能力不超出主 Agent 且不含扇出能力本身。单次分配与 Run 总预算的实现差距见[并行子分析](subagents.md)。

## 4. AgentTaskBrief

每个 RunSegment 启动前由平台生成不可变 `AgentTaskBrief`：

| 内容 | 说明 |
| --- | --- |
| Identity | Run、Segment、Task、SkillVersion、Manifest/Skill checksum |
| Objective | 当前目标、成功条件和本 Segment 要解决的问题 |
| Skill guidance | 原始 Skill 引用、required rules、recommended steps、quality criteria、prohibited actions |
| Resource bindings | 已绑定 Ticket、repository、documents 等资源及允许 scope |
| Tool policy | 可用 capability、参数限制、调用/成本上限和效果等级 |
| Interaction policy | 允许提问的类型、必答项、默认值、超时和审批人 |
| Effect policy | `observe/propose/apply` 上限、默认询问和可选项目预授权 |
| Output expectations | 交付物、证据要求、Artifact 类型及可选 Schema |
| Checkpoint | 之前 Segment 的公开摘要、已确认事实、未决问题、Proposal 和 Evidence refs |

表中为内容分组，实际字段和必填项以 [AgentTaskBrief Schema](../../PJM/contracts/agent-task-brief/v1.schema.json) 为准。

Worker 必须把 Skill 中的指导和平台边界完整传给 Agent，而不是只发送一个短 prompt。为避免上下文无限增长，平台可以生成 checkpoint，但必须保留原始 transcript 和 source trace；压缩摘要不能改写用户已确认的事实或批准范围。

AgentTaskBrief 不包含明文 credential、隐藏系统配置、其他项目数据或 Tool 实现细节。

## 5. 自主执行等级

ExecutionProfile 是策略合成模型；当前按冻结 Manifest/Brief 执行，表格不表示每个选项都已作为独立 Web 控件开放：

| Profile | Agent 自主范围 | 典型用途 |
| --- | --- | --- |
| `GUIDED` | 优先遵循显式步骤；遇到关键歧义即询问 | 高规范、低容错流程 |
| `SUPERVISED` | 默认；自主选择读取顺序、检查方法和报告结构，在业务选择或效果前询问 | Ticket 分析、代码 Review |
| `DELEGATED` | 在预授权资源和低风险效果范围内自主完成更多循环，关键变更仍按策略批准 | 成熟且重复的内部流程 |

Profile 只能收窄或使用平台已批准的能力，不能覆盖硬拒绝规则。Skill 可推荐 Profile，Project ADMIN 决定上限，Run 发起人可在上限内选择更保守等级。

### 5.1 Agent 可自主决定

- 将目标拆分为实际步骤并动态调整顺序。
- 在已绑定资源中选择先读取哪些 Ticket、文件或代码位置。
- 追加必要的只读检查和交叉验证。
- 在预算内继续同一 Session，或建议平台在下一 Segment 恢复/分支会话。
- 选择报告结构、Evidence 组织和需要用户确认的问题。
- 生成工作区内的临时文件、报告或补丁候选（受 Profile 和 workspace policy 限制）。

### 5.2 必须暂停的情况

- 已绑定范围内缺少会影响结论的事实、对象 locator 或业务选择。必需 ResourceRequirement 未绑定时应在创建阶段拒绝，不能靠一次澄清扩大原 Run 权限。
- 用户业务观点、取舍或风险接受度是继续工作的前提。
- 即将执行 Project policy 要求批准的外部效果。
- 目标、权限、数据源或预算需要突破当前 Run 快照。
- Evidence 相互冲突，Skill required rule 无法同时满足。

暂停不意味着随后一定能在原 Run 内解决：补充已有 scope 内的 Ticket ID、说明判断偏好可以继续；增加 Integration、改换 Project、扩展文档集合或权限上限则必须创建新 Run。可选资源在创建时未选，也不能由模型在后续对话中隐式启用。

## 6. Tool 与工作区边界

### 6.1 Tool 分类

| 等级 | 含义 | 默认策略 |
| --- | --- | --- |
| `observe` | 读取已绑定资源，不产生业务副作用 | 参数校验后自动允许 |
| `workspace` | 在隔离 Run workspace 内搜索、生成报告、补丁或临时文件 | 按 ExecutionProfile 允许 |
| `propose` | 形成外部变更提案，但不提交 | 自动允许并记录 Proposal |
| `apply` | 实际改变 Redmine、Git/SVN 或其他系统 | 默认询问；仅显式预授权可自动 |

所有能力必须有版本化 ID 和平台注册 handler。Provider 使用 SecretReference 完成认证，Agent 不接触凭据。

当前开放 issue/repository/document 读取、workspace read/search/write、interaction.request、change.propose、subagent.dispatch。外部 apply 仅由 Effect Worker 调用已注册的 issue.update/repository.write。sandbox command、任意 Shell/网络、issue.search、tabular/report.export 等旧规划名称不属于当前注册能力。具体契约见 [tools](../../PJM/contracts/tools/)。

### 6.2 文件、Shell 和 Web

基础执行闭环为收紧风险而禁用了 SDK 内置 Read/Glob/Grep/Bash/Write/Edit/Web。后续能力不再把“全部禁用”当作模型能力上限，而按能力和隔离条件开放：

- 文件读取/搜索的等价功能通过 `workspace.read/search` 提供，作用于平台资源快照或 Run workspace（该快照的产生方式见 §6.4）。
- 文件写入的等价功能通过 `workspace.write` 提供，只能写 Run workspace，不能直接写宿主机、其他 Run 或 Integration 目标。写入面进一步限定为 `workspace/` 与 `output/`；`input/` 下的资源快照是冻结证据，始终只读。文档产出同界：新文档写入 `output/` 供下载，而原位覆盖 `input/` 文档、Project 文档库或 Integration 目标均非自由写（前者破坏物化快照不变式，写回外部经 ChangeProposal 与审批）；不存在名为 `document.write` 的自由资源能力。
- Bash 只可通过独立 sandbox capability 执行允许的命令/镜像/资源限制；不暴露宿主 Docker socket、Secret 或任意网络。
- Web 只能通过注册的 fetch/search Provider 和 allowlist 使用；不把浏览器或模型内置全网访问当作默认能力。原始网络出口不因任何 Skill 声明而开放：Skill 需要读取的外部系统一律经注册 Provider 访问，使凭据、scope 与 Evidence 三者都留在平台侧。
- `cwd` 只是执行上下文，不是安全边界；真实边界由 mount、sandbox、Tool gateway 和 scope 强制。

SDK 内置 Read/Glob/Grep/Bash/Write/Edit/Web 仍由 hard deny 列表拒绝；以上是平台等价 capability 的边界，不是重新开放内置 Tool。workspace.write 已实现，sandbox command 与通用网络能力尚未开放。

### 6.3 PreToolUse 判定

每次 ToolCall 按以下顺序校验：

1. capability 是否在平台 registry。
2. 是否在 SkillVersion requirement、Project policy 和 Run permission snapshot 中。
3. ResourceBinding、Integration、路径、对象 ID、revision 和网络范围是否匹配。
4. 参数是否通过通用 Tool contract，并已移除 credential-like 字段。
5. 是否命中调用次数、成本、数据量和时间上限。
6. 若为 `apply`，是否存在有效批准或匹配的显式预授权。

任何失败都在 Provider 调用前拒绝，并保存脱敏审计。禁止全局 `bypassPermissions`。

### 6.4 资源快照的物化

完整范围、文件结构、skipped/超限策略与验收见[资源快照与工作区](resource-snapshots.md)。

repository 按冻结 Run binding 的授权范围物化到 `input/<requirement_key>/`，具体内容 revision 在打开资源时解析；分支名并非不可变 commit。document 落在 `input/documents/`，工作副本正在从旧全集枚举切换为显式 ID/hash 清单，尚未完成跨层联调。两条路径的冻结时点、缓存和已知差距以资源规范为准。

每个物化根保存 manifest、内容 hash 与跳过原因，Brief 只描述实际生成的路径。输入对 Agent 只读；写入只到 workspace/output。重复 Attempt 验证并复用既有文件，不静默重建。

## 7. 持续 Run 与多会话

### 7.1 生命周期

```text
用户启动
  → Run + Segment 1 + AgentTaskBrief
  → AgentSession A 执行
  → 需要用户观点/资源/批准
  → 持久化 checkpoint，Run 进入 WAITING_FOR_INPUT/APPROVAL
  → 用户响应
  → Segment 2 + 新 AgentTaskBrief
  → resume A / fork A / 启动 Session B
  → 继续执行和验证
  → Result + terminal RUN_SNAPSHOT
```

一个 Run 可以包含多个 Segment 和多个顺序 Session。Session 的切分由上下文窗口、引擎兼容性、用户分支选择、模型切换或恢复策略决定，不改变 Run 的权限上限。

### 7.2 Segment、Attempt 与 Session 的区别

- Segment 回答“为什么又开始一段工作”：初次启动、用户答复、批准或 Review 反馈。
- Attempt 回答“同一段工作执行了几次”：Worker 重试、租约接管或基础设施故障。
- Session 回答“由哪个 Agent 对话上下文执行”：resume、fork 或新的上下文。

用户答复不能伪装成故障重试；故障接管也不能制造新的业务决策。

### 7.3 会话延续策略

- `resume`：同一引擎/模型兼容，目标未分叉，transcript 与 workspace 完整。
- `fork`：用户要求比较方案或保留原分析分支。
- `replace`：上下文损坏、引擎不兼容或需重新压缩；新 Session 接收可审计 checkpoint。

平台必须记录 continuation mode、parent session、模型和 checkpoint checksum。AgentSession transcript 用于恢复，RunEvent/ToolCall/Evidence/Interaction 才是平台审计事实。

## 8. 用户交互协议

Agent 不以任意自由文本“卡住”Run，而是产生结构化 UserInteraction：

| 类型 | 用途 | UI |
| --- | --- | --- |
| `CLARIFICATION` | 缺少必要事实或资源 locator | 文本/资源选择器 |
| `CHOICE` | 多种合法方案会影响范围或结论 | 单选/多选 + 推荐理由 |
| `REVIEW` | 请求用户反馈分析观点、报告或补丁 | 对话 + 预览/差异 |
| `EFFECT_APPROVAL` | 请求执行明确的外部变更 | Proposal 详情 + 批准/拒绝 |

交互必须包含：公开问题、为什么需要、候选项、默认/推荐项、影响、是否必答和期限。不得暴露 hidden reasoning、Secret、系统 prompt 或 Tool 原始敏感参数。

进入等待状态时 Worker 完成当前持久化事务、释放 lease 并停止计费型执行。用户响应经授权和版本检查后追加 InteractionResponse，创建新 Segment 并重新入队。普通 Interaction 过期时平台追加 `INTERACTION_EXPIRED` 和 `INTERACTION_TIMEOUT` Segment，不把推荐项推断为默认回答；Agent 只能把缺失输入列为限制或重新提问。效果批准过期时 Proposal 进入 `STALE`，不得沿用旧批准或调用 Provider。

## 9. 外部效果协议

### 9.1 外部效果处理流程（observe → propose → apply）

1. `observe`：读取 Redmine、Git/SVN、文档或文件并生成 Evidence。
2. `propose`：生成 ChangeProposal，例如 Ticket 字段 patch、评论草稿、代码 diff 或提交计划。
3. `apply`：平台调用注册 write Tool 执行已批准 Proposal，并回读验证。

默认策略是 `apply` 前提示用户。Project ADMIN 可以为明确 capability、Integration scope、风险等级和参数范围配置无需询问的预授权；用户也可选择更保守策略。任意 Shell、未知 Tool 或无 scope 的写入不能通过配置变成自动执行。

### 9.2 ChangeProposal 必需内容

- 目标 Integration、对象 locator 和操作类型。
- 人可阅读的摘要、结构化 patch/diff 和依据 Evidence。
- 执行前置条件，例如 Ticket updated_at、Git commit、SVN revision。
- 风险等级、可逆性说明和预期验证方式。
- 稳定 idempotency key 和脱敏 request fingerprint。
- 提案所依据的 SkillVersion、RunSegment 和 AgentSession。

### 9.3 执行与验证

- 批准时重新校验 actor、Project、Proposal checksum、Integration 状态和策略。
- 初版 user approval 只允许 Run 发起人或 system ADMIN；Project membership 本身不授予外部写入批准权。
- Provider 写入时使用 optimistic concurrency 或等价前置条件；目标已变化时标记 `STALE`，不覆盖新内容。
- 重试使用同一幂等键，成功结果可重放，禁止重复评论/提交。
- 执行后必须 read-back，保存 before/after Evidence 和验证结果。
- 写入成功但验证失败时标记需要人工处理，不把 Run 静默判为成功。

已实现 Redmine `issue.update/v1` 与 Git/SVN `repository.write/v1`。Redmine 要求版本化 CAS discovery；Git/SVN 默认 direct，也可配置 branch，始终经人工批准。模式、幂等与跨系统失败边界以[受控写入](repository-effects.md)为准。

## 10. 结果与输出验证

成功产出新格式 Result 的 Run 使用 [OutcomeEnvelope](../../PJM/contracts/outcomes/envelope/v1.schema.json)；失败或取消的 Run 可以没有 Result。以下为内容摘要，完整必填字段（包括 outcome_version、needs_review、effects）与枚举以 Schema 为准：

- `summary`、`status`、`deliverables`。
- `findings` 或开放式内容引用。
- `evidence_refs`、`artifact_refs`。
- `open_questions`、`limitations`、`confidence`。
- `change_proposal_refs` 和已执行效果摘要。

任务特定 JSON Schema 是可选附加约束，不是成功的唯一形式。验证分为：

1. 通用包络、引用所有权和敏感信息校验。
2. Skill required rules 与 success criteria 由 Agent 执行并接受质量评审；确定性 validator 不能证明业务推理正确。
3. 若声明了 task-specific Schema，再执行结构校验。
4. 对外部效果校验 Proposal/Approval/EffectExecution 链路。

自由文本报告可以作为 Artifact/Markdown 交付，但关键 Evidence、限制和 Proposal 必须进入通用包络。原始 Result 不被人工修改，反馈追加为 Evaluation。

## 11. 事件、状态与可靠性

当前已定义的事件组包括（完整枚举见 [RunEvent](../../PJM/contracts/events/run-event/v1.schema.json)）：

- `SEGMENT_STARTED` / `SEGMENT_COMPLETED`。
- `INTERACTION_REQUESTED` / `INTERACTION_RESPONDED`。
- `CHECKPOINT_CREATED`。
- `SESSION_STARTED` / `SESSION_RESUMED` / `SESSION_FORKED` / `SESSION_REPLACED`。
- `CHANGE_PROPOSED` / `EFFECT_APPROVED` / `EFFECT_REJECTED` / `EFFECT_APPLIED` / `EFFECT_FAILED`。

Run 状态使用 `WAITING_FOR_INPUT` 和 `WAITING_FOR_APPROVAL` 表达非终态暂停。收到有效响应后新增 Segment 并回到 QUEUED；Run 的 task、SkillVersion、Project 和权限上限不变。

可靠性要求：

- Worker 通过数据库 lease 领取 Attempt，heartbeat 失效后 Recovery Worker 才能接管。
- row lock 顺序固定为 Run → Segment → Attempt。
- 技术重试受 `PROJECTMIND_RUN_MAX_ATTEMPTS` 限制，并且只针对当前 Segment。
- wall timeout 只覆盖活动执行，不覆盖用户等待时间；Interaction 使用独立 expiry。
- Outbox 与状态变更同事务写入，Queue 消费和 Tool/Effect 调用均幂等。
- 终态 RUN_SNAPSHOT 是最后一个持久化事件；SSE 根据它结束。
- 如果 transcript/workspace 不完整，不无声从头执行；按策略 replace Session 或 FAILED，并显示风险。

## 12. Claude Agent SDK 实现要求

- 精确锁定 `claude-agent-sdk` 与内置 CLI 版本；Session 保存 SDK、CLI、模型和 options checksum。
- 使用外部 SessionStore 镜像 transcript，并验证 resume/fork/interrupt 行为。
- PreToolUse/Permission hook 必须调用平台策略，SDK 的 allowed tool 声明不能替代参数级检查。
- 用户等待时关闭或挂起计费型进程，不长期占用 Worker。
- SDK structured output 可用于 CapabilityBlueprint 或可选结果 Schema，但平台仍执行自己的确定性校验。
- 升级前运行 compatibility probe：消息映射、SessionStore、resume/fork、interrupt、Tool hook、MCP、交互暂停、错误 subtype 和旧 Session 恢复。

其他 AgentEngine 必须通过同一 conformance suite，特别是 Tool 拒绝、Session lineage、Interaction、ChangeProposal 和终态事件规则。

## 13. 安全测试矩阵

至少验证：

1. Skill 指导完整进入 AgentTaskBrief，且来源内容不能扩大权限。
2. Agent 可以在 SUPERVISED 下自主调整只读分析步骤，不依赖固定 workflow。
3. 必需资源未绑定时拒绝创建；已有绑定范围内缺少 Ticket ID 等事实时进入 WAITING_FOR_INPUT，答复后新增 Segment，但不能借回答换绑或扩大 scope。
4. 用户业务观点通过 REVIEW 进入 checkpoint，旧 transcript 和响应均可审计。
5. 同一 Run 顺序使用两个 Session，权限/Skill/资源快照不漂移。
6. Worker 丢失只新增 Attempt，不重复 Segment 或用户交互。
7. ChangeProposal 未批准时 Provider 不执行；拒绝、过期和 stale 均 fail closed。
8. 命中显式低风险预授权时可不询问，但 scope 外参数仍拒绝。
9. Redmine/Git/SVN 写入经批准且重试不重复副作用，read-back 与部分失败状态可审计。
10. Run 终态后不能恢复；新问题创建新 Run，父子关联尚非公开创建契约。
11. workspace 文件能力不能通过 path/symlink 逃逸；sandbox command 尚未开放，相关威胁模型属于后续门禁。
12. OutcomeEnvelope 不含 Secret，Evidence/Artifact/Proposal 引用均属于同一 Project。

## 14. 当前实现边界

实现、部署基线与专项验收的状态统一见[计划 §13](../planning/roadmap.md#13-当前执行状态)。目前 Runtime 包括完整 Brief、交互式 Run、顺序主会话、只读子分析、workspace read/search/write 与受控外部效果。

两项未兑现的保证必须在扩展前处理：document 的创建时冻结与范围选择、主子 Session 的 Run 共享预算。详见[资源快照](resource-snapshots.md)与[并行子分析](subagents.md)。来源脚本的登记/checksum 校验不等于已有通用脚本执行器。

## 15. 官方参考

- [Python Agent SDK Reference](https://code.claude.com/docs/en/agent-sdk/python)
- [Work with sessions](https://code.claude.com/docs/en/agent-sdk/sessions)
- [Persist sessions to external storage](https://code.claude.com/docs/en/agent-sdk/session-storage)
- [Configure permissions](https://code.claude.com/docs/en/agent-sdk/permissions)
- [Handle approvals and user input](https://code.claude.com/docs/en/agent-sdk/user-input)
- [Intercept and control behavior with hooks](https://code.claude.com/docs/en/agent-sdk/hooks)
- [Redmine REST Issues](https://www.redmine.org/projects/redmine/wiki/rest_issues)
