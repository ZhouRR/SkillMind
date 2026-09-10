# Task Flow 与 Run Flow

本页定义只读任务预览及后续 Run Flow，不是 Workflow Controller。只读预览与下文尚未冻结的 TaskFlowProjection 是两个协议，不由预览补造持久流程或运行画布；交付状态见[计划 R03](../planning/roadmap.md#r03-task-flow-完整链路)。前置：[Skill 契约](skill-contract.md)、[Workspace](workspace.md)。

## 目标

用户一眼理解目标、资源准备、自动工作/人工确认和交付物。区分三层：

| 视图 | 依据 |
| --- | --- |
| Capability Map：Skill 能做什么 | 精确 SkillVersion 的 Blueprint |
| Task Flow：一个任务建议怎样完成 | 一个 Task 的计划，当前 Project readiness 单独显示 |
| Run Flow：本次实际发生什么 | 本 Run 冻结计划 + 审计事实，不读最新 Task 替换 |

## 一个例子：计划不等于执行事实

任务建议“读文档 → 分析 → 报告”，外部变更须批准。资源候选存在不代表 Run 已读取；无节点关联的子分析属于动态活动；分析完成不等于质量门禁通过；Proposal APPROVED 不等于 apply/read-back 成功。

未提出变更不伪造批准，缺事件也不自动“已跳过”。不按完成节点数虚构 80% 进度，推荐数量、实际活动和质量不是同一计量。

## 已定决策

- Blueprint 是语义正本。节点只能引用既有 task/resource/guidance/interaction/effect/deliverable；真实能力改变走追加解释与新 DRAFT，不拖拽扩权。
- 每次展示一个 Task，不拼全 Skill 大图。首版只投影，不做 DAG/循环/自动分支/节点调度。
- 计划 strength 只有 required/recommended；required 须引用原规则/来源，不因模型建议升级。dynamic 是未关联实际活动，不写进 frozen plan。
- required 是约束，不证明已有逐节点执行/完成检测。Agent 可在授权与预算内调整推荐步骤、追加只读验证。
- 编辑走 Interpretation adjustment/diff/发布；未来 Project overlay 只能添说明、检查点和推荐，不能删 required、绕批准或添能力。
- Run 引入 Flow 后创建时冻结语义及 checksum；后续 Segment/Attempt、版本/配置/布局变化不替换旧计划。
- SDK step_id 不等于节点 ID；未来可选 flow_node_ref 须在本 Run frozen flow 校验，无关联保持动态。
- 等待使用节点 + 持久待办双入口；关闭 Modal/离页不丢服务端 PendingActions，回答/批准仍走原 API 的权限/版本/CSRF/期限。

## 只读任务预览

任务中心读取精确版本的单个 Task，将“任务本身的声明”“Skill 共享要求”“当前准备情况”分开展示。项目读取授权、同组织、活动启用关系与 PUBLISHED 同时成立；不存在、未启用与越权统一拒绝。归档项目可以授权读取，但预览不授予执行权。

[projectmind.task-flow-preview/v1](../../PJM/contracts/tasks/flow-preview/v1.schema.json) 将原蓝图按所选 Task 分区，不生成工作流、节点状态或执行顺序：

| 内容 | 展示规则 |
| --- | --- |
| Task 目标、成功标准、交付物 | 原 Task 声明及原 Blueprint 指针；可选字段未声明与合法空值分别保留 |
| Task 资源 | 仅取该 Task 的 resource_keys；其余资源单列为 Skill 共享声明 |
| 规则、推荐、确认、效果与执行建议 | 标明 Skill 共享范围；现 Blueprint 无 Task 关联，不能推定为本任务必经步骤 |
| 来源 | 原 target/path/line/reason；只验证指针、原文件索引和可用文本快照，不宣称自然语言含义已验证 |
| 当前 readiness | 独立 `scope=SKILL_BLUEPRINT` 的 assessment；null 为未评估，不显示“无需资源”，候选不等于已选择或已读取 |

读取入口：

```text
GET /api/v1/projects/{project_id}/skill-versions/{skill_version_id}/tasks/{task_key}/flow-preview
```

### 身份、来源与失败

服务端与 DRAFT/发布复用原始设计校验，核对 Manifest checksum、版本/解释/来源身份、原 Blueprint 结构与全部 Task 对应关系，再投影所选 Task。读取不规范化回写历史；Manifest 的既有 `skill_key.task_key` 默认 capability 别名与蓝图原 capability 分开保留，不据别名生成新能力。

每个 source trace 的 target 必须在原 Blueprint 中解析，path 必须属于保存的安全相对文件索引；非 binary 文件须有完整文本快照。`TEXT_SNAPSHOT` 表示索引及保存的 UTF-8 文本 size/hash、行范围已核对；`SOURCE_INDEX` 仅用于 line=null 的 binary 文件级引用，只证明索引记录存在，未读取 blob。两者都不证明原规则完整、模型理解正确或外部资源可达。

`AVAILABLE` 带原声明投影；确实未声明或为 null 的 Blueprint 返回 `NOT_DECLARED`，不从 Manifest workflow 反推。已声明却损坏、身份/checksum/来源不一致返回静态 `409 task_flow_preview_invalid`，不降级为空流程。缺失目标为 `404 task_flow_preview_not_found`，读取依赖故障为 `503 task_flow_preview_unavailable`；错误不回显源正文或内部路径。成功和已处理拒绝均禁止缓存。

`blueprint_checksum` 对原 Blueprint 使用共享 canonical hash；`preview_checksum` 覆盖 preview_version、identity、status、blueprint_checksum、plan、source_traces，不含当前 readiness 或自身。浏览器校验形状及精确请求身份，显示服务端 checksum，不用 JavaScript 数字序列化重算 Python 历史格式。它不是 Run 冻结身份或签名。

### 数值读取与校验

参数/结果契约经共享 HTTP 入口的显式 decoder 读取严格 UTF-8 原文；安全整数可用普通数值，其余数值保留原 token，展示不暴露内部包装。不能先转成 JavaScript Number 再把舍入值标为原声明；checksum 字符串未变也不证明原数值保真。协议、快照及 checksum 不因此改写，不添加重复的 raw 字段。

enum 按原声明的 scalar type 校验；integer 不接收 `1.0` 等浮点 token。判重与上下限顺序沿用后端整数/双精度浮点语义：`1` 与 `1.0` 相等，整数 `10**100` 与浮点 `1e100` 不等。大整数不先转浮点数；嵌套参数和结果契约遵守同一规则。

decoder 拒绝非法 JSON、解码后的重复 key 和超过 64 层的容器；原 TaskContract 深度仍以契约为准。来源上传限额不代表生成 Manifest/enum 的字节上限，不据它截断或拒绝合法原声明。响应总量及并发内存预算仍需独立的生产端/消费端协议；本入口不宣称其他 API 已全局无损。

### 页面边界

一次打开一个 Task，以标题、列表和可展开来源呈现；不要求拖拽或横向画布。各项来源只匹配原指针本身及其子项，不继承上级或其他 Task 的来源；全部来源仍可独立查看，未匹配不补造依据。刷新、关闭、Task/版本/项目/会话切换即时隔离旧读取，读取有独立期限；旧 200/401 不污染新目标。页面平台文案覆盖三语，来源标题和正文保留原语言。

预览不调用 Interpreter、不建 DRAFT/Run、不查询运行事件、不访问来源脚本或 blob；认证原有会话续期仍可能写入。缺失预览保留既有任务入口，损坏预览显示不可用而不改写原 SkillVersion；执行仍由原创建与运行门禁判断。

## TaskFlowProjection 目标契约

尚未冻结的目标结构：

```text
TaskFlowProjection
  flow_version / task_key
  nodes: id / kind / title / strength / blueprint_refs / source_traces
  edges: from / to / condition_label
  checksum
  layout
```

| 节点 kind | 引用依据 |
| --- | --- |
| resource | ResourceRequirement、binding/readiness |
| activity | Task、已声明只读 capability、guidance |
| gate | success/quality criteria |
| interaction | InteractionPoint/UserInteraction |
| effect | EffectIntent/Proposal/批准策略 |
| deliverable | Task deliverable、Result/Artifact |

规则详情不全拆成节点。edges 是建议关系，condition_label 仅文字，不执行表达式/JS，不推导前置已完成。

### 计划身份与显示布局

语义 checksum 覆盖协议版本、Task、节点/边/引用/约束；变更走 DRAFT/发布。布局位置、缩放、折叠不参与语义身份，但若保存仍要结构和文件完整性校验。

节点 ID 在计划内唯一，事件按 frozen flow 定位，不用标题、数组位置、SDK ID 或跨版同名猜测。实际状态不进入计划 checksum。规范化使用[共享 hash](../../PJM/backend/src/projectmind/core/hashing.py)，Schema/example、Backend/Web 同步冻结。

### 从发布到历史重放

```text
精确 Task/Blueprint → 来源验证/发布 Flow
  → Run 创建冻结内容/checksum
  → 持久事件与交互/效果可选关联
  → 可重建的 Run Flow
```

只读阶段不提前造 frozen 字段。旧版没声明 Flow 时回退能力摘要/资源/动态步骤；新版声明却损坏应在发布/创建拒绝，历史显示不可用并保留事件，不用最新蓝图修饰旧事实。

同步点包括 Blueprint/projector、Brief/checksum、Run detail/SSE、Web validator/view、Schema/example 与历史兼容。

## 目标用户体验

Skill 页面默认流程预览，report/Blueprint/diff/source trace 作详情；Task Center 查看单任务 Flow 并叠加当前资源 readiness，只有合法 Provider/binding 才显示可执行。

Workspace 展示计划、动态活动、等待、证据与下一步；Conversation/Result/Technical Events 保留。窄屏提供抽屉和键盘列表 fallback，不依赖拖拽/颜色，三语、长标题和未知原因可读。

## 实施工作包

| 阶段 | 必须交付 |
| --- | --- |
| 只读投影 | 复用现有 Blueprint/Task/readiness，能懂资源/建议/确认/产物，不改变执行 |
| 契约与 DRAFT | 可选 Schema、解释/校验、diff/编辑、发布身份和不扩权 |
| Run 观察 | frozen flow、动态活动、可选关联、SSE 重放/重连与子分析 |
| 交互/证据联动 | 原 API、持久待办、详情跳转、权限与状态一致 |

只读预览不是完整链路完成；不依赖 generated FrontendModule、不增业务 Provider 分支、不形成第二个 Run controller。

## 节点状态的事实来源

| 信息 | 依据与不可推导项 |
| --- | --- |
| resource | Task 看 readiness，Run 看冻结/真实准备；候选不等于读成功 |
| activity | 关联 STEP 开始/完成/失败；活动完成不证明业务规则全过 |
| gate | 明确检查结果、Evidence/人工确认；模型口头声明不足 |
| interaction | 持久状态、Response/期限；关窗/发送中不等于受理 |
| effect | Proposal/Approval/EffectExecution 分别显示；批准不等于外部成功 |
| deliverable | 实际 Result/Artifact 及可访问性；SUCCEEDED 不补造产物 |
| dynamic | 无关联实际事件/子分析；不倒写计划或升级 required |

没有事件写“尚无执行证据”；未知/失败/无关联以文字区分。旧 Run 无 flow 时仅显示当时活动；若旁列最新 Task，明确它不是原计划，不与旧完成状态拼接。

## 完成标准

- 无 Flow 的 Skill 正常运行；拖布局不改语义，新发布不覆盖旧 Run。
- 删 required/添能力拒绝；损坏新契约与未声明旧契约分开处理。
- SSE 重复/断线可重建，无关联不猜节点，不重复计数。
- 关闭弹窗仍有待办；回答、批准、apply/read-back 是不同事实。
- Schema/consumer/三语与历史兼容同时回归；实际浏览器验证键盘/窄屏/来源与失败可读，图能显示不算全部验收。
