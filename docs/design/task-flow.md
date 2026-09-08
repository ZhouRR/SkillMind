# Task Flow 与 Run Flow 设计

> 定位：后续展示设计，不是 Workflow Controller。前置阅读：[Workspace](workspace.md)、[Skill 契约](skill-contract.md)。当前没有 TaskFlowProjection Schema、持久化字段或运行画布，进度见[计划 R03](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)。本页使用独立章节编号，旧 PLAN §26 引用仍由计划索引承接。

理解体验先读[目标](#1-目标)和[事实示例](#一个例子计划不等于执行事实)；设计数据结构时读[契约](#3-taskflowprojection-目标契约)；准备开发时对照[工作包](#5-实施工作包)与[阶段边界](#8-阶段验收与事实边界)。本页所有新增 Flow 字段均为目标，不是可直接调用的现行接口。

## 1. 目标

把 Skill 解析结果从面向实现的字段列表，转换为普通用户可以理解和持续观察的任务流程视图。用户至少应能在一次查看内回答：

1. 这个任务要达成什么目标？
2. 运行前需要准备哪些资源，哪些资源当前不可用？
3. 系统会自动完成哪些工作，哪些节点需要我确认或批准？
4. 最终会产出报告、文档、代码变更、计划更新或其他什么结果？

先区分三个阅读层次，下面不是数据库所有权树：

| 视图 | 读者的问题 | 依据 |
| --- | --- | --- |
| Capability Map | 这个 Skill 能做什么？ | 已发布精确 SkillVersion 的 Blueprint |
| Task Flow | 选中的任务建议怎样完成？ | 该版本的一个 Task；当前 readiness 单独显示 |
| Run Flow | 这一次实际发生了什么？ | 本 Run 冻结的计划及实际事件；不能读取最新 Task 替换 |

Task Flow 是对 CapabilityBlueprint 和已发布 Task 的可视化投影，不是新的权限来源，也不是首版工作流执行引擎。现有 Skill 没有明确流程依赖时，平台必须显示“建议/自适应流程”，不能把模型或平台推测伪装成 Skill 的强制顺序。

## 2. 已定决策

**D1｜CapabilityBlueprint 仍是语义正本。**

流程视图不得独立声明或授予新的业务能力、Tool capability、ResourceBinding、Effect 或系统权限。流程节点只能引用 Blueprint 中已经存在的 `task`、`resource_requirement`、`guidance`、`interaction_point`、`effect_intent` 和 `deliverable` key。新增真实能力或外部写入需求必须重新解释并生成新的 SkillVersion Draft，不能通过拖拽节点扩权。

**D2｜流程图按 Task 展示，不把一个 Skill 的所有能力拼成一张大图。**

Skill 页面展示能力概览和多个 Task 入口；Task Center/Task detail 展示一个 Task 的 Flow；Workspace 展示该 Task 当前 Run 的 Flow。这样“能做什么”“如何完成”和“本次做了什么”不会混在同一层。

**D3｜首版是 Projection，不是严格 Workflow Controller。**

Agent 默认使用 `SUPERVISED` profile；在已授权能力和预算内，可以重排 recommended steps、追加只读验证和使用 Subagent。流程图必须把“计划约束”与“实际活动”作为两个维度：

- 冻结计划的 `strength` 只取 `required / recommended`。前者引用必须遵守的目标、规则或门禁，后者是允许调整的建议过程；不能仅凭模型建议把节点提升为 required。
- `dynamic` 描述未关联计划的实际活动，不是第三种 strength，也不追加进 frozen flow。只读验证、子分析等无关联活动进入“动态步骤”，按原事件展示。
- required 说明必须满足的约束，不证明平台已有逐节点强制调度或完成检测。质量通过、交互已受理和外部写入成功仍分别依赖各自事实来源，见[节点状态](#9-节点状态的事实来源)。

严格 DAG、循环、自动分支和节点级调度不属于首版；未来若需要，将作为独立 Execution Profile/Controller 设计。

**D4｜流程节点使用受限的通用类型。**

第一版只定义以下类型：

| 类型 | 普通用户文案 | 允许引用 |
|---|---|---|
| `resource` | 准备资源 | ResourceRequirement、Project binding/readiness |
| `activity` | 系统处理 | Task、已声明的只读 capability、guidance |
| `gate` | 质量检查/判断条件 | success criteria、quality criteria |
| `interaction` | 需要你确认 | UserInteraction/InteractionPoint |
| `effect` | 提交变更/更新外部系统 | EffectIntent、ChangeProposal、批准策略 |
| `deliverable` | 交付结果 | Task deliverable、Result、Artifact |

required rule、prohibited action 和普通说明默认作为节点详情中的规则/提示，不全部拆成独立节点，避免图形变成技术字段列表。

**D5｜流程图编辑不直接改写已发布 Skill。**

Skill 管理者对解析结果的修改通过既有 Interpretation adjustment、Draft、diff 和发布流程产生新版本。Project/Task 层未来可以增加受限 overlay，但只能增加人工检查点、说明、资源选择偏好和推荐步骤，不能删除 required rule、绕过交互/批准节点或增加未授权 capability。

**D6｜Run 必须冻结流程快照。**

Run 流程观察阶段引入持久化契约后，Run 创建时冻结 `flow_checksum` 及对应的流程内容；后续 SkillVersion、Task Draft、Project overlay 或资源配置变化不得改变历史 Run。第一阶段的只读投影不得为旧 Run 伪造这些字段。新的 Segment 可以基于同一 Run 的 frozen flow 和用户响应继续，但不能读取最新版本替换原流程。

**D7｜运行状态由事件投影得到。**

现有 `STEP_STARTED`/`STEP_COMPLETED`/`STEP_FAILED` 的 `step_id` 来自 Agent SDK 动态任务，不保证等于 Blueprint 节点 ID。第一版不得假定二者一一对应。后续可以增加可选的 `flow_node_ref` 关联，但必须校验它指向 frozen flow 中已存在的节点；缺少关联的事件显示为动态活动。Interaction、ChangeProposal、Evidence 和 Artifact 同样只增加可选关联，不改变现有状态机和审计 sequence。

**D8｜用户交互使用“流程节点 + 持久待办”双重出口。**

进入 `WAITING_FOR_INPUT` 或 `WAITING_FOR_APPROVAL` 时，流程图中的节点显示“等待你处理”，并可以打开平台 Modal/Drawer。关闭弹窗、离开页面后，待办仍必须通过概览、导航徽标和服务端筛选的 PendingActions 保留；弹窗不是唯一通知渠道。回答或批准仍沿用现有 Interaction/ChangeProposal API、CSRF、版本、过期和授权校验。

## 3. TaskFlowProjection 目标契约

后续新增 `TaskFlowProjection` contract。下列仅是待验证的目标结构，不是已冻结 Schema。它是可选的展示/计划契约，不替代 CapabilityBlueprint：

```text
TaskFlowProjection
  flow_version
  task_key
  nodes[]
    id
    kind
    title
    strength: required | recommended
    blueprint_refs[]
    source_traces[]
  edges[]
    from
    to
    condition_label
  checksum
  layout
```

`nodes` 和 `edges` 保存语义引用；`layout` 只保存画布位置、分组和折叠状态，不能承载权限或执行规则。边表示推荐关系，不是 Worker 的调度依赖；`condition_label` 只供人阅读，不执行表达式或 JavaScript，也不能从连线自动推导前置节点已完成。

### 计划身份与显示布局

目标契约须把“计划改了”和“看图方式改了”分开，否则拖动画布或切换窄屏布局会让历史事件失去关联。

| 内容 | 变化时的处理 |
| --- | --- |
| 计划语义 | 版本、task_key、nodes、edges 及其引用/约束参与语义 checksum；变更走 Draft、校验和发布，不覆盖已有 Run |
| 显示布局 | 位置、折叠、缩放不参与语义 checksum，不改变节点身份或批准要求。若随版本保存，仍校验结构并保护完整文件，不能把“无需重算计划”当作“不校验内容” |
| 节点身份 | id 在一份计划内唯一；事件通过本 Run 的 frozen flow 校验引用，不靠节点标题、数组位置或 SDK step_id 匹配，也不跨版本套用同名节点状态 |
| 实际状态 | 来自持久事件和关联对象，不写进计划 checksum；没有事件时保留“尚无执行证据” |

上面的 `checksum` 对应后续 Run 的 `flow_checksum`，不是来源 package 的 content_hash 或完整发布文件的 hash。字段集合、规范化方式与版本兼容须在 Schema/example、Backend validator 和 Web consumer 一起冻结；复用[共享 hash 实现](../../PJM/backend/src/projectmind/core/hashing.py)，不让前后端各算一套身份。

### 从发布到历史重放

```text
精确 SkillVersion 的 Task / Blueprint
    ↓ 投影、来源校验、Draft 与发布
Task Flow（版本化计划）
    ↓ 创建 Run 时冻结语义内容和 checksum
本 Run 的 frozen flow
    ↓ 与实际事件、交互、Proposal 等关联
Run Flow（可重建的观察视图）
```

这是引入 Flow 契约后的目标链路。只读预览阶段只使用现有数据，不提前伪造 frozen 字段。后续 Segment/Attempt 沿用同一 Run 计划；最新 Task 的布局、规则或 readiness 不覆盖历史事实。

现有没有流程契约的 PUBLISHED Skill 必须继续可以运行，并回退到“能力摘要 + 资源清单 + Agent 动态步骤”的标准视图。新增契约时必须同步 Schema、example、Backend projector、AgentTaskBrief frozen checksum、Run detail、SSE/Web view 和测试。

兼容必须区分“旧版本没有声明”和“新版本声明却损坏”：前者走标准回退；后者在发布/创建时拒绝，历史读取显示不可用原因并保留原事件，不能用最新 Blueprint 重建成看似合法的旧计划。

## 4. 目标用户体验

**Skill 管理/解析页面：** 默认显示“流程预览”而不是原始 JSON/字段清单；report、Blueprint、contract、diff 和 source trace 作为高级详情。每个节点显示来源、置信度、必选/推荐、资源前提、确认条件和预计产出。

**Task Center：** 每个 Task 增加“查看流程”，并在流程节点上显示当前 Project 的 readiness。Redmine、Git、SVN、Figma、文档等只在存在合法 Provider 和 binding 时显示为可执行；没有 Provider 时显示“未支持/需配置”，不能仅凭 Skill 文本显示为可运行。

**Workspace：** 增加 Flow 观察区，显示当前节点、已完成节点、动态活动、等待节点、证据/产物数量和下一步。Conversation、Result、Technical Events 继续保留，但原始 event timeline 不作为普通用户的主要进度界面。窄屏使用抽屉，必须提供键盘可操作的列表/详情 fallback，不能只依赖画布拖拽和颜色。

## 5. 实施工作包

| 工作包 | 边界 | 进入下一阶段前必须证明 |
| --- | --- | --- |
| 只读流程投影 | Web 复用现有 Blueprint、TaskCatalog、readiness、Interaction、Effect 和 Result 数据 | 能看懂资源、建议步骤、确认点和产出；不改变执行语义 |
| 流程契约与 Draft | 可选 Schema、Interpreter 输出/校验、Draft/diff、受限编辑与发布绑定 | 可版本化且节点不能扩权；历史 Skill 继续可用 |
| Run 流程观察 | frozen flow、动态活动、可选 flow_node_ref、SSE replay/重连、Subagent 分支 | 实际活动只来自可审计事实，缺少关联不伪造完成 |
| 交互与证据联动 | Interaction/Proposal/Evidence/Artifact 详情，Modal/Drawer + PendingActions | 用户沿用原 API 回答/批准；跨入口状态与授权一致 |

此表规定依赖和交付，不重复维护进度。只读预览可以先实施，但不能用它代替后三个工作包的冻结、关联与回退要求。

## 6. 非目标

- 不把流程图直接升级为新的 Run lifecycle 或第二套执行入口。
- 不从 RuntimeManifest 的旧 `workflow` 字段机械反推 Skill 流程。
- 不允许任意节点声明 Tool、Secret、网络、Shell 或外部写入权限。
- 当前不实现严格 DAG 控制器、循环执行、任意表达式条件或跨 Run 编排。
- 不依赖 generated FrontendModule；标准 Web 组件必须可以独立呈现流程和待办。
- 不增加 Figma、Jira 等 Provider 的业务分支；Provider 是否存在仍由通用 Integration/Capability registry 决定。

## 7. 完成标准

这些是完整链路的验收，不把一张可显示的预览图当作全部完成：

| 场景 | 可观察结果 |
| --- | --- |
| 普通 Skill 没有声明 Flow | 能力、资源与动态活动仍可读；不虚构强制顺序或历史计划 |
| 拖动布局、再发布计划、打开旧 Run | 布局不改变语义身份；新版本不覆盖旧 Run，历史节点仍只关联原 frozen flow |
| 手动删 required rule 或增加能力 | 发布/创建校验拒绝，不把图形编辑当授权入口 |
| SSE 断线、重复事件、缺失节点关联 | 重放去重，不重复计数；无关联保留动态活动，不把相似标题合并成已完成 |
| 关闭等待弹窗、批准后回读失败 | 待办不因关窗丢失；批准与 apply/read-back 分开，不显示虚假的成功 |
| 窄屏、键盘与三语查看 | 无需拖拽和颜色也能理解状态、来源与待办；长标题、错误和未知状态可读 |

Schema/example、公开投影、Web validator、三语、Backend/Web 回归与历史兼容需同时具备证据。阶段交付按下一节分别判断。

## 8. 阶段验收与事实边界

| 阶段 | 输入依据 | 不得推导的事实 |
| --- | --- | --- |
| 只读 Task 预览 | 当前已发布 Blueprint/TaskCatalog/readiness | 推荐顺序不等于强制依赖；不要求改变 Backend |
| Flow 契约与 Draft | 新的可选 Schema、版本与校验器 | 未通过发布的编辑不改变已发布 Skill |
| Run 流程观察 | 创建时 frozen flow + RunEvent + detail | 未关联节点的事件不能推断节点完成 |
| 历史 Run | 原始 Run snapshot 与当时事件 | 缺少流程快照时不能用最新 Skill 假造历史计划 |

新增节点不能仅凭模型声称为 required。required 需要既有蓝图规则和来源依据；无法映射的活动显示为动态活动。节点关联、完成状态与 Evidence 是三种不同事实，分别验证。

现行 UI 责任见 [Workspace](workspace.md)，当前推进顺序见[实施计划 §26](../planning/roadmap.md#26-任务流程视图)。

## 9. 节点状态的事实来源

下表是展示语义，不是新增公开状态枚举。冻结契约时再确定字段与 enum，并同步 Backend/Web：

| 节点/信息 | 可用依据 | 不能据此显示的状态 |
| --- | --- | --- |
| resource | 当前 Project readiness；Run 场景使用冻结资源与实际准备结果 | 候选存在不等于读取成功；新配置不覆盖历史 Run |
| activity | 已关联的开始/完成/失败事件 | STEP_COMPLETED 只证明对应活动结束，不证明所有业务规则通过 |
| gate | 对应质量检查结果、来源与 Evidence，必要时人工确认 | 模型一句“已检查”不等于平台已确定性证明质量达标 |
| interaction | 持久 UserInteraction/Response 的状态、版本与期限 | 关闭弹窗、发送中的回答不等于服务端已接受 |
| effect | Proposal/Approval/EffectExecution 的各自状态 | APPROVED 不等于 APPLIED；APPLIED 也不能掩盖回读失败 |
| deliverable | 实际 Result/Artifact 引用及可访问性 | Run SUCCEEDED 不自动补齐不存在的交付物 |
| 动态活动 | 无计划节点关联的实际事件/子分析 | 不倒写 frozen flow，也不自动升级为 required 节点 |

推荐步骤、required 约束、运行时新增活动是不同维度：`dynamic` 不是与 required/recommended 并列的冻结强度值。等待、失败、未观察和无关联都要有文字说明；不按节点数量虚构百分比进度，节点没有事件时默认“尚无执行证据”。

历史 Run 没有 flow snapshot 时只显示当时可还原的活动。若另展示最新 Task 预览，必须清楚标注它不是该历史 Run 的原计划，不能与历史完成状态拼接。

### 一个例子：计划不等于执行事实

假设任务建议“读文档 → 分析 → 生成报告”，并声明“若需要修改外部内容，须提交提案并批准”。下面是目标展示语义，不是新增固定工作流：

| 这次实际发生的事 | 应怎样显示 |
| --- | --- |
| Project 有可选文档，尚无本 Run 准备记录 | 任务资源可能就绪；不能显示该 Run 已读完文档 |
| Agent 追加一次子分析，没有 flow_node_ref | 展示动态只读活动；不往历史计划补节点或改变 required |
| 已有分析完成事件，缺少质量检查依据 | 活动已结束；质量门禁仍“尚无证据”，不一并打勾 |
| 提案获批，apply/read-back 尚未成功 | 显示批准已受理和实际效果状态；不标成外部内容已更新 |
| 没有提出外部变更 | 不伪造一次批准。条件性动作是否适用须有明确依据；不能仅因缺少事件写成“已跳过/已通过” |

这个例子解释为什么不显示“完成了四个节点，所以进度 80%”：建议数量、实际活动、质量结论与外部效果不是同一计量单位。
