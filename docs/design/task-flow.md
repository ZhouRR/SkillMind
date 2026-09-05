# Task Flow 与 Run Flow 设计

> 后续设计，尚未实现。当前没有 TaskFlowProjection Schema、持久化字段或运行画布。第一阶段只读展示；后续契约冻结与 Run snapshot 在各自工作包引入。

### 26.1 目标

把 Skill 解析结果从面向实现的字段列表，转换为普通用户可以理解和持续观察的任务流程视图。用户至少应能在一次查看内回答：

1. 这个任务要达成什么目标？
2. 运行前需要准备哪些资源，哪些资源当前不可用？
3. 系统会自动完成哪些工作，哪些节点需要我确认或批准？
4. 最终会产出报告、文档、代码变更、计划更新或其他什么结果？

当前产品对象关系固定为：

```text
Skill
  └─ Capability Map：说明能做什么
       └─ Task Flow：说明一个任务如何完成
            └─ Run Flow：说明本次实际执行发生了什么
```

Task Flow 是对 CapabilityBlueprint 和已发布 Task 的可视化投影，不是新的权限来源，也不是首版工作流执行引擎。现有 Skill 没有明确流程依赖时，平台必须显示“建议/自适应流程”，不能把模型或平台推测伪装成 Skill 的强制顺序。

### 26.2 已定决策

**D1｜CapabilityBlueprint 仍是语义正本。**

流程视图不得独立声明或授予新的业务能力、Tool capability、ResourceBinding、Effect 或系统权限。流程节点只能引用 Blueprint 中已经存在的 `task`、`resource_requirement`、`guidance`、`interaction_point`、`effect_intent` 和 `deliverable` key。新增真实能力或外部写入需求必须重新解释并生成新的 SkillVersion Draft，不能通过拖拽节点扩权。

**D2｜流程图按 Task 展示，不把一个 Skill 的所有能力拼成一张大图。**

Skill 页面展示能力概览和多个 Task 入口；Task Center/Task detail 展示一个 Task 的 Flow；Workspace 展示该 Task 当前 Run 的 Flow。这样“能做什么”“如何完成”和“本次做了什么”不会混在同一层。

**D3｜首版是 Projection，不是严格 Workflow Controller。**

Agent 默认使用 `SUPERVISED` profile，可以重排 recommended steps、追加只读验证和使用 Subagent。流程图必须同时表达“计划”和“实际”：

- `required` 节点表示必须遵守的目标、规则、质量门禁或安全节点；
- `recommended` 节点表示 Skill 建议的过程，允许 Agent 在目标不变时调整；
- `dynamic` 节点表示运行时新增的只读活动或 Subagent 分支；
- 未匹配到计划节点的 Agent 活动进入“动态步骤”区域，不强行归类。

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

### 26.3 TaskFlowProjection 目标契约

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

`nodes` 和 `edges` 保存语义引用；`layout` 只保存画布位置、分组和折叠状态，不能承载权限或执行规则。首版条件只用于人类可读的 `condition_label`，不执行任意表达式或 JavaScript。

现有没有流程契约的 PUBLISHED Skill 必须继续可以运行，并回退到“能力摘要 + 资源清单 + Agent 动态步骤”的标准视图。新增契约时必须同步 Schema、example、Backend projector、AgentTaskBrief frozen checksum、Run detail、SSE/Web view 和测试。

### 26.4 目标用户体验

**Skill 管理/解析页面：** 默认显示“流程预览”而不是原始 JSON/字段清单；report、Blueprint、contract、diff 和 source trace 作为高级详情。每个节点显示来源、置信度、必选/推荐、资源前提、确认条件和预计产出。

**Task Center：** 每个 Task 增加“查看流程”，并在流程节点上显示当前 Project 的 readiness。Redmine、Git、SVN、Figma、文档等只在存在合法 Provider 和 binding 时显示为可执行；没有 Provider 时显示“未支持/需配置”，不能仅凭 Skill 文本显示为可运行。

**Workspace：** 增加 Flow 观察区，显示当前节点、已完成节点、动态活动、等待节点、证据/产物数量和下一步。Conversation、Result、Technical Events 继续保留，但原始 event timeline 不作为普通用户的主要进度界面。窄屏使用抽屉，必须提供键盘可操作的列表/详情 fallback，不能只依赖画布拖拽和颜色。

### 26.5 实施工作包

| 工作包 | 边界 | 关键结果 | 状态 |
|---|---|---|---|
| 规格边界 | 更新规划、Workspace 和 Skills 规范，冻结对象层次、节点类型、权限边界和非目标 | 规格可评审，暂无代码/迁移 | 已完成（文档） |
| 只读流程投影 | Web 复用现有 Blueprint、TaskCatalog、readiness、Interaction、Effect 和 Result 数据 | 普通用户能看懂资源、步骤、确认点和产出；不改变执行语义 | 未开始 |
| 流程契约与 Draft | `TaskFlowProjection` contract、Interpreter 输出/校验、Draft/diff、受限手动节点和发布绑定 | 流程可版本化，节点不能扩权；历史 Skill 保持兼容 | 未开始 |
| Run 流程观察 | frozen flow snapshot、动态活动投影、可选 `flow_node_ref`、SSE replay/断线恢复和 Subagent 分支显示 | Workspace 能实时显示“计划 vs 实际” | 未开始 |
| 交互与证据联动 | Flow 节点与 Interaction/ChangeProposal/Evidence/Artifact 详情联动，Modal/Drawer + PendingActions 双出口 | 用户在交互节点完成回答/批准并能继续 Run | 未开始 |

### 26.6 非目标

- 不把流程图直接升级为新的 Run lifecycle 或第二套执行入口。
- 不从 RuntimeManifest 的旧 `workflow` 字段机械反推 Skill 流程。
- 不允许任意节点声明 Tool、Secret、网络、Shell 或外部写入权限。
- 当前不实现严格 DAG 控制器、循环执行、任意表达式条件或跨 Run 编排。
- 不依赖 generated FrontendModule；标准 Web 组件必须可以独立呈现流程和待办。
- 不增加 Figma、Jira 等 Provider 的业务分支；Provider 是否存在仍由通用 Integration/Capability registry 决定。

### 26.7 完成标准

- 新增普通 Skill 没有完整流程声明时，仍能显示能力摘要、资源前提和动态执行过程，不虚构固定顺序。
- 用户可以从流程图判断资源是否就绪、何时需要自己处理、最终会产生什么结果。
- 手动节点经过 Draft/publish 校验，不能删除 required rule、绕过批准或增加权限。
- Run 的流程 checksum、节点引用和实际事件可以重放；SSE 重连不重复或伪造节点完成状态。
- 等待中的 Interaction/Approval 在流程图、Workspace、概览和导航中状态一致。
- 所有流程契约、API、Web 类型、三语文案、Backend/Web 测试和历史版本兼容性检查同步完成。

## 8. 阶段验收与事实边界

| 阶段 | 输入依据 | 不得推导的事实 |
| --- | --- | --- |
| 只读 Task 预览 | 当前已发布 Blueprint/TaskCatalog/readiness | 推荐顺序不等于强制依赖；不要求改变 Backend |
| Flow 契约与 Draft | 新的可选 Schema、版本与校验器 | 未通过发布的编辑不改变已发布 Skill |
| Run 流程观察 | 创建时 frozen flow + RunEvent + detail | 未关联节点的事件不能推断节点完成 |
| 历史 Run | 原始 Run snapshot 与当时事件 | 缺少流程快照时不能用最新 Skill 假造历史计划 |

新增节点不能仅凭模型声称为 required。required 需要既有蓝图规则和来源依据；无法映射的活动显示为动态活动。节点关联、完成状态与 Evidence 是三种不同事实，分别验证。

现行 UI 责任见 [Workspace](workspace.md)，当前推进顺序见[实施计划 §26](../planning/roadmap.md#26-任务流程视图)。
