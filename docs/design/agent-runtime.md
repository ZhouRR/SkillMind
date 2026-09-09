# ProjectMind Agent Runtime 与交互式执行规范

> 定位：执行边界与持续 Run 协议。前置阅读：[领域对象与状态速查](domain-model.md#run-状态速查)。创建请求的重放由[创建与幂等](run-creation.md)负责，内容一致性由[资源快照](resource-snapshots.md)负责，限额与共享账户由[预算设计](run-budgets.md)负责；本页聚焦已有 Run 如何执行和续行。

ProjectMind 使用 Claude Agent SDK 作为默认执行引擎，通过 `AgentEngine` 保持模型/引擎可替换性；平台负责用户交互、Tool 权限、外部效果、证据和故障恢复。

按问题定位：[谁负责什么](#2-责任边界)、[Agent 可以调用什么](#6-tool-与工作区边界)、[一次执行如何续行](#7-持续-run-与多会话)、[模型启动前检查什么](#74-从领取到模型启动的边界)、[回答与过期](user-interactions.md)、[取消和超时如何区分](#75-取消超时与失去执行权)、[失败与恢复](#11-事件状态与可靠性)。章节号保留供代码中的旧引用使用。

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

Profile 只能收窄或使用平台已批准的能力，不能覆盖硬拒绝规则。Skill 可推荐 Profile，system ADMIN 管理项目策略上限，Run 发起人可在上限内选择更保守等级；不引入独立的 Project ADMIN 角色。

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

以下是逻辑检查清单，分别由 SDK hook、Gateway、binding/Provider 与 Effect Worker 执行，不表示单个 hook 已实现全部检查：

1. capability 是否在平台 registry。
2. 是否在 SkillVersion requirement、Project policy 和 Run permission snapshot 中。
3. ResourceBinding、Integration、路径、对象 ID、revision 和网络范围是否匹配。
4. 参数是否通过通用 Tool contract，并已移除 credential-like 字段。
5. 执行路径所支持的局部限额是否满足；Run 累计调用/成本/输出检查需接入[共享预算](run-budgets.md)，当前不能承诺已经强制。
6. 若为 `apply`，是否存在有效批准或匹配的显式预授权。

身份、权限、binding 与已知请求限制在 Provider 调用前拒绝；返回内容的 Schema、敏感字段和大小只能在取得内容后检查，失败时不得交给 Agent。相关错误保存脱敏审计。禁止全局 `bypassPermissions`。

### 6.4 资源快照的物化

完整范围、文件结构、skipped/超限策略与验收见[资源快照与工作区](resource-snapshots.md)。

repository 按冻结 Run binding 的授权范围物化到 `input/<requirement_key>/`，具体内容 revision 在打开资源时解析；分支名并非不可变 commit。document 落在 `input/documents/`，读取和物化使用显式 ID/hash 清单，创建重放沿用首次快照。选择 UI 和公开 detail 负责确认/展示，不成为 Provider 的新授权输入。两条路径的冻结时点、可信缓存回执与跨根总量设计以资源规范为准；完整验收范围见计划 R01。

每个物化根保存 manifest、内容 hash 与跳过原因，Brief 描述经完整准备验证的逻辑路径。`input/` 是 Tool 路径空间，物理世代由平台映射，不能让 Agent 选择或切换准备目录。输入只读；写入只到 workspace/output。重复 Attempt 仅复用通过[Run 级回执验证](resource-snapshots.md#输入准备与可信缓存)的完整输入，不静默重建。工作副本已有实际 Tool 的回执读取接线；启动前校验、每次读取与准备恢复仍须分别验证，不能从其中一项推导其余都已安全。

## 7. 持续 Run 与多会话

### 7.1 生命周期

```text
用户启动
  → Run + Segment 1 + dispatch Outbox
  → Worker 准备并冻结 AgentTaskBrief
  → AgentSession A 执行
  → 需要用户观点/资源/批准
  → 持久化 checkpoint，Run 进入 WAITING_FOR_INPUT/APPROVAL
  → 普通答复 / 外部效果链确认续行
  → Segment 2 + dispatch Outbox
  → Worker 准备并冻结新 AgentTaskBrief
  → resume A / fork A / 启动 Session B
  → 继续执行和验证
  → Result + terminal RUN_SNAPSHOT
```

一个 Run 可以包含多个 Segment 和多个顺序 Session。Session 的切分由上下文窗口、引擎兼容性、用户分支选择、模型切换或恢复策略决定，不改变 Run 的权限上限。

这是业务续行示意，不是完整状态机，也不表示 Brief 在收到回答的同一事务中已生成。当前创建/答复先持久化 Segment 与 dispatch Outbox，Worker 准备时构建并冻结该段 Brief。精确状态路径、等待 Attempt 的 DEFERRED 和技术重试见[领域状态速查](domain-model.md#run-状态速查)。

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

### 7.4 从领取到模型启动的边界

Worker claim 获得 Attempt lease，不表示输入已经准备好。当前 `prepare_execution` 先把执行推进为 `RUNNING`，之后 ContextBuilder 才准备资源和 Brief；因此 Run 的 `RUNNING` 不能作为“可信输入已 READY”或“模型已开始”的证据。

2026-09-08 核对的 [Executor](../../PJM/backend/src/projectmind/worker/executor.py)已在 `prepare_execution` 前启动同一组执行、heartbeat 和取消监督任务，最终启动校验由 `verify_execution_start` 执行。下面是当前调用顺序；输入内部事务由[资源协议](resource-snapshots.md#一次准备的提交边界)负责，不在本页再定义一份回执。

```text
有效 claim → heartbeat / 取消监督
  ↓
Run → RUNNING
  ↓
ContextBuilder → PreparedInput
  本阶段使用独立准备 timeout
  ↓
校验 context 身份 / sequence
  ↓
短事务：冻结 Brief
  重验 lease / 取消
  ↓
短事务：启动校验
  重验状态 / lease / 取消
  ↓ 事务结束
execute / resume / fork
  ↓
记录事件 → 等待 / 终态
```

context 身份必须匹配 claim 的 Run / Attempt / Project / actor，事件序号沿用 repository 的分配。准备可能慢于一个 lease，所以需要 heartbeat；心跳成功之后仍可能被取消或失去执行权，所以每个提交/启动边界还需独立校验。

相关 repository 按 Run → Segment → Attempt 加锁，拿到锁后再用当前时间和数据库 lease 校验；不能使用 claim 时的旧到期值判断后续延长，也不能用等锁之前的时间放行已过期执行。资源网络/转换和模型调用都在事务外，不持锁等待它们完成。

| 已观察到的事实 | 能说明什么 | 还不能说明什么 |
| --- | --- | --- |
| `RUNNING` / `run.execution.prepared` 日志 | 已推进执行状态，准备即将进行 | 输入完整、Brief 已冻结、模型已启动 |
| 输入回执 `READY` | 原世代的完整输入记录已提交 | 文件永不变化、仍有执行权 |
| Brief 已冻结 / `run.execution.brief` 日志 | 本 Segment 的指导与路径已固定 | 启动校验已通过或 Session 已建立 |
| Session 与对应模型活动事件 | 已记录该 Session 的活动 | 全部输入都被读过、任务已成功 |

这四类事实不合并成“准备成功”的推测，也不新增一套 Run 状态机。准备、模型、job 与人工等待的计时边界集中见[计时器说明](run-budgets.md#现有计时器的覆盖范围)。本地监督测试不能替代真实事务、物化接口联调和旧 Run 续行验收；当前证据见[计划](../planning/roadmap.md#13-当前执行状态)。

### 7.5 取消、超时与失去执行权

取消请求、Run 终态、client 清理与用量确认是不同事实。完整规则已集中到[执行监督与停止](run-supervision.md)，本节保留旧引用入口；本页继续负责整体 Run、Session 和事件协议。

先用[点击取消的例子](run-supervision.md#一个例子点击取消之后)理解状态，再看[阶段与首事件前路径](run-supervision.md#不同阶段如何收束)、[提交的判断点](run-supervision.md#提交时谁决定最终状态)、[收尾与晚到信息](run-supervision.md#收尾终态与晚到信息)。准备/模型/job 的秒数只在[预算计时器](run-budgets.md#现有计时器的覆盖范围)维护。

首事件前取消、无意图 interrupted 的失败分类、终态复查，以及等待/普通事件写入前的取消检查已有工作副本接线与局部回归。[等待拒绝与终态保存](run-supervision.md#等待提交仍是独立边界)使用两个事务，第二次仍需验证 lease；真实锁竞争、提交不明与进程停止继续分别验收。不得因此放宽 lease fencing、终态最后事件和未知用量占用规则。

## 8. 用户交互协议

详细协议集中到[用户答复、等待与续行](user-interactions.md)，本节保留旧 docs/06 §8 引用。先分清[普通答复、外部批准和结果评价](user-interactions.md#先分清三种人工参与)，再读[三个提交边界](user-interactions.md#三个提交边界)；普通 REVIEW 不等于 Evaluation，通用回答不能替代 Proposal decision。

普通交互只在原 Run 的冻结范围内补充事实。推荐不是默认回答，required=false 不代表自动跳过；[期限例子](user-interactions.md#一个例子回答超时不等于什么都没发生)说明为何 410 也可能伴随已提交的过期续行。等待释放 lease 与实际进程停止分别证明，不能从数据库等待状态保证费用已结清。

当前[普通批准入口的差距](user-interactions.md#普通提问不能代替外部批准)与[Web 原答复确认](user-interactions.md#答复界面与结果未知)均有待修正。模型应通过 change.propose 形成可验证 Proposal；不能为解除悬空等待而放开通用答复的批准权限。

## 9. 外部效果协议

### 9.1 外部效果处理流程（observe → propose → apply）

1. `observe`：读取 Redmine、Git/SVN、文档或文件并生成 Evidence。
2. `propose`：生成 ChangeProposal。当前可执行载体为受限 Ticket 字段 SET、仓库逐文件 SET/REMOVE；评论草稿、diff 或提交计划不自动成为可执行操作。
3. `apply`：独立 Effect Worker 调用注册 Provider 执行已批准 Proposal，并回读验证；不向 Agent 开放直接 write Tool。

默认策略是 `apply` 前提示用户。本组织 system ADMIN 可为允许预授权的 capability 配置 LOW 风险、精确 Integration/operation/scope 策略；repository.write 不可预授权。项目成员身份不等于 ADMIN。任意 Shell、未知 Tool 或无 scope 的写入不能通过配置变成自动执行。

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

已有 Redmine `issue.update/v1` 与 Git/SVN `repository.write/v1` Provider。上列为不变量，不是完整恢复已验收的声明；模式、事务、状态归属与[可靠性差距](repository-effects.md#可靠性修正要求)只在受控写入正本维护。尤其 Effect lease 与 RunAttempt lease 分开，不能把 Run Executor 已有的 heartbeat/取消监督推导为 Effect Provider 已有同样能力。

## 10. 结果与输出验证

本节保留旧编号入口，完整规则集中到[结果、证据与人工评价](results-evaluation.md)。先用[执行结束但仍需修订的例子](results-evaluation.md#一个例子执行结束结论仍需修订)分清技术终态、Outcome 与人工判断，再读[校验的实际保证](results-evaluation.md#结果校验的实际保证)和[保存边界](results-evaluation.md#保存与显示不是同一个提交)。

Runtime 负责把校验后的候选交给终态事务，并在锁后处理 lease/取消；失败或取消可以没有 Result。ResultValidator 的结构和部分引用检查不证明业务正确、附件可读或所有效果声明已核验。主/子共用校验也不改变[子任务输出的派生责任](subagents.md#子任务指令与结果的边界)。

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
- 当前 wall timeout 从 context/Brief 准备完成后的 engine stream 开始，不覆盖排队和用户等待，也不累计全部 Attempt。超时按 `wall_timeout` 失败收尾；准备阶段与 Run 总时长的边界见[预算设计](run-budgets.md#当前实现的实际口径)。Interaction 使用独立 expiry。
- Outbox 与状态变更同事务写入，Queue 消费以领取状态抑制重复；Tool 审计重放与外部 Effect 幂等各有独立键和协议，不因此承诺模型执行或外部副作用“全局精确一次”。
- 终态 RUN_SNAPSHOT 是最后一个持久化事件；SSE 根据它结束。
- 如果 transcript/workspace 不完整，不无声从头执行；按策略 replace Session 或 FAILED，并显示风险。

运维停写不由 Run 状态机或 dispatch 设置自动完成。当前开关只限制 Run/Effect 的新 Outbox 配送，job 与 cron 的具体边界见[发布手册](../operations/deployment.md#一个例子关闭-dispatch-后仍有工作)。恢复中不能仅关闭 dispatch 就启动 Worker，也不把 Worker 停止当作远端请求已停止的证据。

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
13. 准备持续超过一个 lease、准备 deadline 到期、Brief/启动前取消、锁等待期间过期分别验证；慢准备持续续租，超时/已成立取消/失效 lease 不启动模型，失效 Worker 不写终态。
14. 最终启动校验后、首事件前取消，持久化期间失去 lease，以及 stream 关闭失败分别验证；取消请求、持久终态与真实进程停止不混为同一个断言。

## 14. 当前实现边界

实现、部署基线与专项验收的状态统一见[计划 §13](../planning/roadmap.md#13-当前执行状态)。目前 Runtime 包括完整 Brief、交互式 Run、顺序主会话、只读子分析、workspace read/search/write 与受控外部效果。

document 的显式选择、创建时冻结和公开清单，以及准备监督、独立 timeout、Brief/启动校验、首事件前取消已有工作副本代码；这些入口不重复登记为整项待开发。完整物化链路、真实事务/历史续行和实际进程清理仍须验收；Run 预算已有[内部账本](run-budgets.md#持久账本的当前载体)，但创建、主子执行和核对方尚未接入。已有模块或局部回归不证明整条执行链可用，精确缺口见[计划](../planning/roadmap.md#13-当前执行状态)，停止专项见[验收矩阵](run-supervision.md#验收矩阵)。来源脚本的登记/checksum 校验也不等于已有通用脚本执行器。

## 15. 官方参考

- [Python Agent SDK Reference](https://code.claude.com/docs/en/agent-sdk/python)
- [Work with sessions](https://code.claude.com/docs/en/agent-sdk/sessions)
- [Persist sessions to external storage](https://code.claude.com/docs/en/agent-sdk/session-storage)
- [Configure permissions](https://code.claude.com/docs/en/agent-sdk/permissions)
- [Handle approvals and user input](https://code.claude.com/docs/en/agent-sdk/user-input)
- [Intercept and control behavior with hooks](https://code.claude.com/docs/en/agent-sdk/hooks)
- [Redmine REST Issues](https://www.redmine.org/projects/redmine/wiki/rest_issues)
