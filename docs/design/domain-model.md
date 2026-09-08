# ProjectMind 领域模型与权限设计

> 定位：核心实体、关系、状态与权限不变量。先读[核心术语](../overview/glossary.md)；执行流程见 [Runtime](agent-runtime.md)，当前实现与验收范围见[计划](../planning/roadmap.md#13-当前执行状态)。平台不把 JAF、代码评审等业务字段固化为领域对象。

## 1. 建模原则

- Skill 描述业务能力、目标、所需资源、指导规则、交付物和可能的外部效果；它不直接授予平台权限。
- 平台契约只固定跨 Skill 通用的控制协议，不预定义 Ticket、代码评审等业务 Schema。
- `Run` 表示一个具有明确目标的持续执行线程，可以包含多个顺序 Segment 和 Agent Session；一次模型会话不等于一个 Run。
- 用户补充信息、选择方案或批准外部效果时，在同一非终态 Run 中追加 Segment；Worker 故障重试才追加 Attempt。
- 外部写入必须先形成可审查的 ChangeProposal，再按平台策略批准、执行和回读验证。
- 已发布 SkillVersion、Run 快照、Result 和审计事件不可原地修改。

## 2. 组织、项目与资源

- `Organization`：组织边界。MVP 只启用一个组织，但核心数据保留 `organization_id`。
- `User`：平台用户，系统角色只有 `ADMIN` 和 `USER`。
- `AuthSession`：浏览器 opaque session，只保存 token hash、CSRF token hash、期限和失效记录。
- `Project`：Integration、任务、Run 和知识数据的主要隔离边界。
- `ProjectMember`：User 与 Project 的成员关系，不引入额外系统角色。
- `Integration`：项目中配置的 Provider 资源实例，例如 Redmine、Git、SVN。不是所有资源都需要 Integration 行；内置 Project 文档走文档目录与[Run 文档快照](resource-snapshots.md)。
- `SecretReference`：凭据定位信息，不保存明文 Secret。
- `ProjectKnowledge`：概念名称；当前持久化实体是 `ProjectDocument`，保存上传文档及 blob 元数据。自动知识同步/独立索引不是已实现服务。

Skill 资产（SkillSource、SkillInterpretation、Skill、SkillVersion、RuntimeManifest）归 Organization，
不归单个 Project：同一份 Skill 只导入、解释和发布一次，多个 Project 复用同一个 SkillVersion。这是
就绪度模型的前提——[Skill 就绪度](skill-contract.md#72-运行就绪度)要求同一 SkillVersion 在一个项目为 RUNNABLE、在另一个项目仍需
配置，若 SkillVersion 归属单个 Project 则该语义无法成立。

Project 通过 `ProjectSkillVersion` 显式启用精确的 PUBLISHED SkillVersion 才能发现和执行它：

- 启用粒度是 SkillVersion 而不是 Skill。Run 冻结精确版本（§7.2），平台也不自动切换 PUBLISHED 版本；
  若启用到 Skill，新版本发布会静默改变项目的可执行内容。
- 默认不启用。新发布的版本不会让任何项目突然多出可执行任务，扩大执行面必须是一次显式的 ADMIN 操作。
- 启用关系不授予权限，也不改变资源绑定：能否执行仍由 Project 资源、Tool policy 和 Run 权限快照决定。

该模式与既有的 `SkillComposition`（Organization 级）+ `ProjectComposition`（启用关系）一致，不引入
第二套作用域机制。

Integration 声明自己实际提供的版本化 Tool capability 和允许范围。Skill 只提出抽象资源要求，例如“一个可读取 Ticket 的 issue source”或“一个允许生成补丁但不直接提交的 repository source”。

## 3. Skill 导入、解释与发布

### 3.1 核心对象

- `SkillSource`：导入的原始目录式 Skill、附件和来源元数据，不可变保存。
- `SkillInterpretation`：Interpreter 对同一 SkillSource 的一次追加式理解结果。
- `CapabilityBlueprint`：解释得到的能力蓝图，描述能做什么、为什么做、需要什么、如何判断完成以及可能产生什么效果。
- `ResourceRequirement`：能力运行前必须或可选绑定的抽象资源要求。
- `OutcomeDefinition`：预期交付物、成功条件、证据要求和可选结构提示。
- `Skill`：稳定业务标识。
- `SkillVersion`：内容冻结、可发布和废弃的精确版本；生命周期元数据不等于版本内容。[回滚](skill-contract.md#11-版本回滚与评价)改变后续选择，不改写旧版。
- `RuntimeManifest`：SkillVersion 的平台运行投影，冻结能力蓝图、任务蓝图、Tool requirement、执行策略和可选动态 Schema。
- `SkillComposition`：多个 SkillVersion 的可配置组合，可展示为虚拟角色、模块或任务组，但不授予系统权限。
- `ProjectSkillVersion`：Project 对精确 PUBLISHED 版本的启用记录，不授予权限。当前停用后保留记录但不能重新启用；追加式恢复历史属于后续设计。

### 3.2 能力蓝图

`CapabilityBlueprint` 至少表达：

- 领域能力与用户可理解的用途。
- 触发意图和一个或多个目标。
- 必要/可选资源、访问方式和绑定条件。
- 来自 Skill 的指导、判断标准、禁止事项和证据要求。
- 预期交付物与成功条件。
- 需要用户参与的澄清、选择、审查和批准点。
- 可能的外部效果，例如更新 Redmine、提交代码或只生成报告。
- 假设、未决问题、置信度和 source trace。

领域能力可以是新的业务概念，不要求预先存在于 Tool catalog。只有实际 Tool 调用使用 `issue.read/v1`、`repository.read/v1` 这类平台注册 capability ID。

### 3.3 发布与可运行性分离

SkillVersion 的“可发布”与任务的“当前可运行”是两个判断：

- 安全且可理解、但缺少 Integration 的版本可以发布为可配置能力。
- 资源绑定完成且所需 Tool 全部可用后，任务才进入 `RUNNABLE`。
- 请求外部写入但项目没有对应 Provider 或批准策略时，任务可以运行到“生成提案”，不能执行写入。
- 信息不足的 Skill 可发布为 `GUIDANCE_ONLY`，由对话和人工步骤使用，不伪造业务字段。

这里的 `GUIDANCE_ONLY` 是任务就绪度，不是 SkillVersion 的发布状态；仍须有合法蓝图并通过发布门禁。完整[判断顺序](skill-contract.md#发布与就绪的判断顺序)区分 Preview、gate、发布、启用和 readiness，不能用其中一项替代其它项。

## 4. 任务、Run 与会话

### 4.1 任务定义

- `ExecutableTask`：从已发布 CapabilityBlueprint 投影出的项目任务。
- `ResourceBinding`：把 ResourceRequirement 绑定到具体 Integration、ProjectKnowledge 或用户输入。
- `AgentTaskBriefSnapshot`（早期称 AgentInstructionSnapshot）：Run/Segment 传给 Agent 的不可变执行说明，包括原始 Skill 引用、目标、资源、指导、权限和效果策略。
- `TaskSchedule`：任务的预约与周期规则。[调度规范](task-scheduling.md)定义 `ONCE`（指定时刻一次）与 `CRON`（周期），必带 IANA 时区，保存精确 SkillVersion、任务输入与资源选择规则；每次触发再创建独立 Run 快照。监控条件触发不做。

ExecutableTask 以目标和资源前提为中心。动态 input/output Schema 可以帮助生成表单或验证稳定结构，但属于可选派生产物，不是所有 Skill 的业务能力本体。

### 4.2 执行对象

- `Run`：一个有明确目标的持续执行线程，是审计、交互和最终结果的主记录。
- `RunSkillSnapshot`：Run 使用的 SkillVersion、Manifest checksum 和配置快照。
- `RunSegment`：Run 中由“初次启动、用户答复、批准、审查反馈”等业务事件触发的一段连续工作。
- `RunAttempt`：同一 Segment 的一次 Worker 领取、故障恢复或技术重试。
- `RunInputSnapshot`：整个 Run 输入准备的独立回执，记录准备世代与全部文件摘要；不是创建请求或每个 Segment 的新资源选择。当前有 model/migration 与部分接线，完整协议见[资源准备](resource-snapshots.md#输入准备与可信缓存)。
- `AgentSession`：AgentEngine 会话。一个 Run 可以顺序使用多个主会话，并记录 resume/fork/replace 关系；[并行子分析](subagents.md)的只读子会话以 `session_kind = SUBAGENT`、`continuation_mode = BRANCH` 记录。
- `AgentSessionTranscript` / `AgentSessionEntry`：SDK opaque transcript 的追加式镜像。
- `RunStep`：由 STEP_* 事件投影的概念，不是独立持久化表；不要求机械复现 Skill 建议顺序。
- `RunEvent`：SSE 重连和审计使用的追加式事件。
- `ToolCall`：一次版本化 Tool capability 调用。
- `Evidence`：Ticket、文件、代码位置、Diff 或快照等证据引用。
- `Artifact`：报告、补丁、导出文件等产物。
- `Result`：Run 终态时冻结的原始结果或通用结果包络。
- `Evaluation`：人工评分、结论和修订建议，不覆盖 Result。

### 4.3 用户交互与外部效果

- `UserInteraction`：Agent 请求的结构化交互，类型包括 `CLARIFICATION`、`CHOICE`、`REVIEW` 和 `EFFECT_APPROVAL`。
- `InteractionResponse`：用户对交互的追加式答复；必须记录 actor、时间和所依据的版本。
- `ChangeProposal`：准备对外部系统实施的结构化变更提案，包含目标、预览、理由、前置版本、风险和幂等键。
- `EffectExecution`：经批准或命中项目预授权策略后的一次实际写入与回读验证记录。

Agent 可以建议写入、生成补丁和解释风险，但不能把建议直接变成权限。平台只有在注册 write Tool、Project 授权、Integration scope、批准策略和运行时参数校验全部通过后才允许 EffectExecution。

### 4.4 核心关系

- SkillSource 可产生多次 SkillInterpretation；重新解释不覆盖旧记录。
- SkillInterpretation 产生一个 CapabilityBlueprint candidate；发布时冻结进 SkillVersion/RuntimeManifest。
- SkillVersion 可以投影多个 ExecutableTask，同一版本可被多个 SkillComposition 复用。
- 同一 SkillVersion 可被多个 Project 通过 ProjectSkillVersion 启用；就绪度按各 Project 的资源独立计算。
- 停用 ProjectSkillVersion 只影响新 Run 的发现与创建；已存在的 Run 与其 snapshot、Result 不受影响。
- ResourceRequirement 的 Project/Task 选择在创建时冻结为 Run 资源；Integration binding 与内置文档快照的载体不同，见[资源快照](resource-snapshots.md)。
- Run 包含一个或多个顺序 RunSegment；首个 Segment 由用户启动创建。
- RunSegment 包含一个或多个追加式 RunAttempt；技术重试不创建新的业务 Segment。
- Run 可以包含多个 AgentSession；同一 Run 同时最多一个活动的主（PRIMARY）Session，只读 SUBAGENT 子会话可在[子分析边界](subagents.md#能力与故障边界)内并行。
- 用户答复或批准使等待中的 Run 创建新 Segment，并根据兼容性 resume、fork 或启动新 Session。
- Run 包含多个 RunStep、RunEvent、ToolCall、Evidence、Artifact、UserInteraction 和 ChangeProposal。
- 一个 Run 最多有一个终态 Result；终态后的新目标创建新 Run。child/fork 关系是后续产品关联设计，当前创建请求不承诺 parent Run 字段。

把执行关系放在一起看：

```text
Run：固定目标、权限与资源选择
├── Segment 1：初次启动
│   ├── Attempt 1：一次 Worker 领取 → 主 Session / 只读子 Session
│   └── Attempt 2：同段技术恢复 → 审计原 Session 的延续/替换
├── Segment 2：有效答复、批准等业务续行 → 新的 Attempt
├── 输入回执：按 Run 固定完成世代，不随 Segment / Attempt 重选
└── Result / Evidence / Interaction / Proposal 等业务与审计事实
```

这是关系摘要，不是数据库列或完整状态图。Brief 按 Segment 冻结，输入回执按 Run 复用，lease 按 Attempt 校验；它们不能因为都带 checksum 就合并成一种快照。

## 5. 核心字段与实际载体

以下给出定位入口，不维护第二份完整字段字典。数据库列以 [db/models.py](../../PJM/backend/src/projectmind/db/models.py) 为准；公开字段以 [OpenAPI](../../PJM/contracts/openapi/projectmind-api.v1.json) 和相应 Schema 为准。概念对象不代表同名数据库表。

### 5.1 Skill 与能力

| 概念 | 当前持久化载体 | 契约 |
| --- | --- | --- |
| SkillSource | `skill_sources` 的 source metadata、content_hash、file index | 组织资产，不含 Project 选择 |
| SkillInterpretation | `skill_interpretations` 的 report_json、manifest_draft_json、execution_json、lineage | [Interpreter 协议目录](../../PJM/contracts/skills/interpreter/v1/) |
| CapabilityBlueprint | 解释结果与 `manifest_json.capability_blueprint` 内嵌数据 | [Blueprint v1](../../PJM/contracts/capability-blueprint/v1.schema.json) |
| Skill / SkillVersion | `skills` / `skill_versions` | 版本身份与发布状态 |
| RuntimeManifest | `runtime_manifests.manifest_json` 和 checksum | [Manifest v1alpha1](../../PJM/contracts/runtime-manifest/v1alpha1.schema.json) |
| ProjectSkillVersion | `project_skill_versions` 的启停记录 | 精确 PUBLISHED 版本可见性；现有唯一关系不能表达多次启停历史 |

### 5.2 项目任务与资源

| 概念 | 当前载体 | 关键边界 |
| --- | --- | --- |
| ExecutableTask | TaskCatalog descriptor，由版本投影 | 无独立 task 业务表；task_id 由服务端生成 |
| SecretReference | `secret_references`；MANAGED 密文另存 `managed_secret_material` | locator/明文不公开 |
| Integration / ResourceBinding | `integrations` / `resource_bindings` | Project/Task 配置和 Run 冻结分层 |
| ProjectDocument | `project_documents` 与 object-storage blob | 文档身份与内容冻结的设计及联调差距见[资源快照](resource-snapshots.md) |
| AgentTaskBriefSnapshot | `agent_task_brief_snapshots.brief_json` / checksum | 每个 Segment 唯一、不可变 |
| TaskSchedule | `task_schedules` | [调度](task-scheduling.md)复用普通 Run 创建 |

### 5.3 Run、交互与效果

| 概念 | 当前表/契约 | 关键边界 |
| --- | --- | --- |
| Run / RunSkillSnapshot | `runs` / `run_skill_snapshots` | 初始目标、权限和版本不可漂移 |
| RunInputSnapshot | 工作副本 model 与 migration 0029 的 `run_input_snapshots` | Run 唯一；全部输入的准备回执，不是公开资源清单；[消费者与真实恢复仍待验收](resource-snapshots.md#已知差距与后续设计) |
| Segment / Attempt | `run_segments` / `run_attempts` | 业务续行与技术重试分别追加 |
| Session / Transcript | `agent_sessions` / `agent_session_transcripts` / `agent_session_entries` | 主 Session 顺序，子 Session 只读并行 |
| UserInteraction / Response | `user_interactions` / `interaction_responses` | 版本、身份、期限、幂等 |
| Proposal / Approval / EffectExecution | `change_proposals` / `change_approvals` / `effect_executions` | 精确变更审批与可验证执行 |
| Result / Evaluation | `run_results` / `evaluations` | 原始结果不可变，人工修订追加 |
| Evidence / Artifact | `evidence`、结果中的 Artifact refs、blob | Artifact 是产物概念，不假设存在独立 artifact 表 |
| RunEvent / Outbox | `run_events` / `outbox_messages` | 状态与 Outbox 同事务，消费幂等 |

`TEXT_DELTA` 可以消耗 sequence 但不持久化；sequence 欠号不等于事件丢失。完整协议见 [RunEvent](../../PJM/contracts/events/run-event/v1.schema.json)。

创建请求的身份不等于运行时快照，幂等作用域也不等于权限作用域。工作副本在 `task_snapshot_json.creation_request` 保存版本化意图；新旧 hash 路径与事务边界见[Run 创建与幂等](run-creation.md)，不通过重写旧快照补造历史。该内部载体不是新增公开响应字段。

## 6. 权限模型

| 操作 | ADMIN | USER |
| --- | --- | --- |
| 管理用户、项目成员、Integration 和 SecretReference | 允许 | 禁止 |
| 导入、解释、发布或废弃 SkillVersion（Organization 级） | 允许 | USER 不开放 |
| 为 Project 启用或停用 SkillVersion | 允许 | USER 不开放 |
| 查看和执行项目任务 | 允许 | 项目成员且任务已启用可用时允许 |
| 回答澄清、选择和 Review | 允许 | 当前为具有 ProjectWriteActor 的有效项目成员；仍校验交互版本/期限 |
| 批准外部效果 | 允许 | 初版仅 Run 发起人；指定审批人模型上线前其他成员禁止 |
| 配置低风险效果预授权 | 允许 | 禁止 |
| 查看 Run、Evidence、Artifact 和 Result | 允许 | 仅所属项目成员 |
| 追加 Evaluation | 允许 | 所属项目成员 |
| 删除审计记录 | 禁止 | 禁止；只按保留策略清理 |

权限判断顺序：

1. 平台硬拒绝规则。
2. SystemRole 与 ProjectMember。
3. Project 策略与 Integration scope。
4. SkillVersion 的 Tool requirement 和效果意图。
5. Task/Run 的权限快照与 ResourceBinding。
6. ToolCall 参数级策略。
7. 对外部效果的有效批准或显式预授权。

后面的层不能覆盖前面的拒绝。Skill 文本、Agent 决策和用户自由文本都不能直接扩大权限。

## 7. 状态与不变量

### 7.1 状态流转

- `SkillInterpretation`：`ANALYZING → PREVIEW_READY → SUPERSEDED`，失败进入 `FAILED`。
- `SkillVersion`：当前枚举为 `DRAFT / PUBLISHED / DEPRECATED`（见 [skills/domain.py](../../PJM/backend/src/projectmind/skills/domain.py)）；不引入不存在的 TESTING/ARCHIVED。重复发布 PUBLISHED 返回原记录，DEPRECATED 不能重新发布；废弃与删除的作用域见[生命周期规则](skill-contract.md#111-版本内容与可见性)。
- 任务可运行性：`GUIDANCE_ONLY | CONFIGURATION_REQUIRED | RUNNABLE | ACTIONABLE`；它是投影状态，不替代 SkillVersion 状态。
- `ChangeProposal`：`DRAFT → PENDING_APPROVAL → APPROVED → APPLYING → APPLIED`，可进入 `REJECTED`、`STALE` 或 `FAILED`。
- `EffectExecution`：`REQUESTED → LEASED → APPLYING → APPLIED`，也可回到同一 `REQUESTED` 技术重试，或进入 `STALE`、`FAILED`、`VERIFICATION_FAILED`。

### Run 状态速查

下表是常见路径，不是另一份完整状态机。允许转换由 [ALLOWED_RUN_TRANSITIONS / plan_run_transition](../../PJM/backend/src/projectmind/runs/domain.py)决定；不能仅凭 API 操作名自行设置状态。

| 发生的事情 | Run 的路径 | Segment / Attempt 怎么变化 |
| --- | --- | --- |
| 首次启动 | `QUEUED → PREPARING → RUNNING` | 创建 Segment 1；Worker claim 创建本段 Attempt |
| 请求用户输入/批准 | `RUNNING → WAITING_FOR_INPUT / WAITING_FOR_APPROVAL` | Segment 进入 WAITING，Attempt 记 DEFERRED 并释放 lease |
| 有效答复或独立期限处理 | 等待态 → `QUEUED → PREPARING → RUNNING` | 追加新 Segment；领取时创建新段的 Attempt，不覆盖旧答复或批准 |
| 活动 lease 失效并允许恢复 | `PREPARING / RUNNING → RETRY_PENDING → PREPARING` | 原 Attempt 记 LEASE_EXPIRED，同一 Segment 追加 Attempt |
| 完成、不可恢复失败或取消 | `SUCCEEDED / FAILED / CANCELLED` | 对应对象收尾，保存 terminal RUN_SNAPSHOT；终态不能重新打开 |

`RunSegment` 使用 `CREATED / RUNNING / WAITING / COMPLETED / FAILED / CANCELLED`；`RunAttempt` 使用 `CREATED / LEASED / RUNNING / DEFERRED / SUCCEEDED / FAILED / LEASE_EXPIRED / CANCELLED`。枚举存在不代表每次都经历全部状态，例如 claim 直接创建 LEASED Attempt。

`WAITING_PERMISSION` 是旧协议的历史兼容值，不作为新交互的入口。重新投递 Queue job 不等于 Run 必须回到 QUEUED；重试可直接从 RETRY_PENDING 被领取为 PREPARING。取消请求已接受也不等于执行已停止，以终态快照为准。

`Run.RUNNING` 在资源准备前写入；输入回执自己的 `PREPARING / READY` 描述文件准备，属于另一个对象。二者都不能替代 Session 已启动或最终执行权校验。[Runtime 启动顺序](agent-runtime.md#74-从领取到模型启动的边界)区分这些事实；Web 不自行合成未公开的准备状态，也不把回执状态混入 RunEvent enum。

### 7.2 执行不变量

- 等待用户时不占用 Worker lease，不继续计算 wall timeout；交互自身使用独立期限。普通交互过期时不推断推荐项为默认回答，而以 `INTERACTION_TIMEOUT` 新 Segment 显式携带缺失事实；批准过期时 Proposal 变为 STALE，Provider 不执行。
- 用户答复/批准创建新 RunSegment；允许恢复的 Worker lease 丢失才在同 Segment 追加 Attempt。不能把所有 timeout 都当作可重试：当前 engine wall timeout 按 FAILED 收尾，具体策略见 [Runtime](agent-runtime.md#11-事件状态与可靠性)。
- 执行与恢复按 aggregate 固定锁顺序：Run → Segment → Attempt，或 Run → Segment → Proposal/Interaction/Effect；不得先锁子对象再锁 Run。
- 终态 `RUN_SNAPSHOT` 必须是 Run 的最后一个持久化事件，SSE 据此结束。
- 改变目标、SkillVersion、Project、权限上限或已使用的数据源必须创建新 Run；同一 Run 的用户补充只能在冻结边界内收敛目标。
- 多 Session 只改变 Agent 执行载体，不改变 Run 的权限和资源快照。
- 预算上限与实际余额不是同一快照；主/子执行与跨段恢复共用预算的目标要求见[预算设计](run-budgets.md)，当前局部上限不等于共享账本已实现。
- 外部效果执行前必须重新验证提案版本、冻结 Binding checksum、目标对象前置版本和批准有效性；Provider 必须证明原子 optimistic concurrency 与幂等协议，执行后必须回读并产生 before/after Evidence。
- Result 保存 AI 原始输出；人工修订只能追加为 Evaluation。
- PostgreSQL 是 Run、交互、批准、效果和审计事实来源；Redis 只承担 Queue、短期锁和通知。

## 8. 实施兼容说明

0019–0021 已增加 OutcomeEnvelope、RunSegment/UserInteraction/AgentTaskBrief snapshot、顺序 Session、
Integration/ResourceBinding 与 controlled effect 表。新 Run 必须显式创建 Segment 1 和 immutable Run
binding；旧 Run 仍按“一个隐式 Segment”只读投影，不回填不存在的历史业务事实。标准 Redmine REST 接口不被假定具备原子条件更新；`issue.update/v1` 仅连接声明 CAS 与幂等协议的 adapter。Git/SVN 已支持审批后的 `repository.write/v1`，当前 direct/branch 规则见[受控写入](repository-effects.md)。

JAF、repository-review 的旧业务 Schema、seed 和专用 renderer 不再是活动规则来源。历史 SkillVersion 与 Run snapshot 仅用于审计读取，不恢复为平台预定义业务模型。
