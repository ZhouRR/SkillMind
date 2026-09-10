# Task Flow 与 Run Flow

本页定义只读预览与后续 Run Flow，不新增执行器。现行预览不等于尚未冻结的 TaskFlowProjection，也不补造持久计划；状态见[计划 R03](../planning/roadmap.md#r03-task-flow-完整链路)，前置见[Skill](skill-contract.md)、[Workspace](workspace.md)。

## 目标

让用户看懂目标、资源、自动工作/人工确认及交付物，区分三层：

| 视图 | 依据 |
| --- | --- |
| Capability Map：Skill 能做什么 | 精确 SkillVersion 的 Blueprint |
| Task Flow：一个任务建议怎样完成 | 一个 Task 的计划，当前 Project readiness 单独显示 |
| Run Flow：本次实际发生什么 | 本 Run 冻结计划 + 审计事实，不读最新 Task 替换 |

## 一个例子：计划不等于执行事实

“读文档 → 分析 → 报告”只是计划：有候选不等于已读取，分析完成不等于质量通过，APPROVED 不等于 apply/read-back 成功。无关联的子分析显示为动态活动；缺事件不猜“已跳过”，不按节点数虚构百分比。

## 已定决策

- Blueprint 是语义正本，节点只引用既有 task/resource/guidance/interaction/effect/deliverable；能力改变走追加解释与 DRAFT/diff/发布，不拖拽扩权。
- 每次投影一个 Task，不做 DAG、循环、自动分支或节点调度。
- strength 仅 required/recommended；required 须有原规则/来源，不等于已有完成检测。Agent 可在授权/预算内调整推荐、追加只读验证；Project overlay 不得删 required、绕批准或添能力。
- Run 创建冻结语义/checksum，后续 Segment/Attempt、版本、配置或布局不替换。flow_node_ref 只关联本 Run frozen flow，不用 SDK step_id 猜节点；无关联动态活动不倒写计划。
- 节点与持久待办双入口；回答/批准沿原 API 的身份/版本/CSRF/期限，关窗不丢待办。

## 只读任务预览

读取须同时满足 Project 授权、同组织、活动启用关系及 PUBLISHED；不存在/未启用/越权统一拒绝。归档可授权读取，但预览不授予执行权。

[预览 v1](../../SKM/contracts/tasks/flow-preview/v1.schema.json)按单 Task 分区，不生成顺序或节点状态：

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

与 DRAFT/发布共用校验：Manifest checksum、版本/解释/来源身份、原 Blueprint 及全部 Task 对应关系通过后才投影。不回写历史，Manifest 默认 `skill_key.task_key` 别名与原 capability 分开，不据此造能力。

source trace 的 target 须解析到原 Blueprint，path 须在安全文件索引中。非 binary 必须核对完整 UTF-8 快照的 size/hash/行范围，标为 `TEXT_SNAPSHOT`；binary 仅允许 line=null 的 `SOURCE_INDEX`，不表示已读 blob。两者均不证明规则完整、模型理解或外部可达。

`AVAILABLE` 返回原声明；仅未声明/null 返回 `NOT_DECLARED`，不从 Manifest 反推。损坏/身份/来源不符为静态 `409 task_flow_preview_invalid`，目标缺失为 `404 task_flow_preview_not_found`，读取故障为 `503 task_flow_preview_unavailable`。不回显正文/内部路径，成功及已处理拒绝均 no-store。

`blueprint_checksum` 使用原 Blueprint 的共享 canonical hash；`preview_checksum` 覆盖 preview_version、identity、status、blueprint_checksum、plan、source_traces，不含 readiness/自身。浏览器核对形状/精确身份并展示服务端 hash，不以 JS 重算 Python 历史格式；它不是签名或 Run 冻结身份。

### 数值读取与校验

共享 HTTP 显式 decoder 读取严格 UTF-8；安全整数外的数值保留原 token，不先转 JS Number，也不暴露内部包装。协议、快照/hash 不改写，不另添 raw 字段；hash 未变不证明数值保真。

enum 按声明 scalar type 校验，integer 不接收 `1.0`。判重/上下限沿后端整数与双精度语义：`1` 等于 `1.0`，整数 `10**100` 不等于浮点 `1e100`；大整数不先转浮点，嵌套同规则。

拒绝非法 JSON、解码后重复 key 和超过 64 层的容器；TaskContract 深度另按契约。上传限额不限制生成 Manifest/enum，不截断合法声明；响应总量/并发内存需另定完整协议，不宣称其他 API 已无损。

### 页面边界

单 Task 用标题/列表/展开来源，无需拖拽。来源只匹配原指针及子项，不继承上级或其他 Task；全部来源可另读，无匹配不造依据。读取有期限，刷新/关闭/切 Task/版本/项目/会话隔离旧 200/401；平台三语，来源保留原语言。

预览不调用 Interpreter、建 DRAFT/Run、查事件或读脚本/blob；认证续期仍可能写入。未声明保留任务入口，损坏显示不可用而不改版本；执行另过原门禁。

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

不把全部规则拆成节点；edges 只表示建议，condition_label 不执行表达式/JS，不推导前置完成。

### 计划身份与显示布局

语义 checksum 覆盖版本、Task、节点/边/引用/约束，不含实际状态或布局；语义变更走 DRAFT/发布，保存布局仍校验结构/文件完整性。节点 ID 计划内唯一，事件按 frozen flow 定位，不按标题/位置/SDK ID 猜测。采用[共享 hash](../../SKM/backend/src/skillmind/core/hashing.py)，同步 Schema/example 与消费者。

### 从发布到历史重放

```text
精确 Task/Blueprint → 来源验证/发布 Flow
  → Run 创建冻结内容/checksum
  → 持久事件与交互/效果可选关联
  → 可重建的 Run Flow
```

旧版未声明则回退摘要/资源/动态活动；声明损坏须在发布/创建拒绝，历史保留事件并显示不可用，不借最新蓝图修饰。接线须同步 projector、Brief/checksum、Run detail/SSE、Web、契约与历史兼容；只读阶段不提前造 frozen 字段。

## 目标用户体验

Skill 默认预览、详情展开 report/Blueprint/diff/来源；Task Center 叠加 readiness，合法 Provider/binding 才显示可执行。Workspace 保留计划、动态活动、待办、证据及 Conversation/Result/Events；三语/长标题/未知可读，窄屏与键盘列表不依赖拖拽或颜色。

## 实施工作包

| 阶段 | 必须交付 |
| --- | --- |
| 只读投影 | 复用现有 Blueprint/Task/readiness，能懂资源/建议/确认/产物，不改变执行 |
| 契约与 DRAFT | 可选 Schema、解释/校验、diff/编辑、发布身份和不扩权 |
| Run 观察 | frozen flow、动态活动、可选关联、SSE 重放/重连与子分析 |
| 交互/证据联动 | 原 API、持久待办、详情跳转、权限与状态一致 |

不依赖 generated FrontendModule，不增业务 Provider 分支或第二个 Run controller。

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

无事件显示“尚无执行证据”，未知/失败/无关联文字区分。旧 Run 无 flow 只显示当时活动；旁列最新 Task 必须标为非原计划，不拼旧完成状态。

## 完成标准

- 无 Flow 的 Skill 正常运行；拖布局不改语义，新发布不覆盖旧 Run。
- 删 required/添能力拒绝；损坏新契约与未声明旧契约分开处理。
- SSE 重复/断线可重建，无关联不猜节点，不重复计数。
- 关闭弹窗仍有待办；回答、批准、apply/read-back 是不同事实。
- Schema/consumer/三语与历史兼容同时回归；实际浏览器验证键盘/窄屏/来源与失败可读，图能显示不算全部验收。
