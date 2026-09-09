# Workspace 与 Web 交互

任务中心说明“能执行什么”，Workspace 观察“这一次发生什么”。本页负责布局、导航和交互交接；协议分别由[创建](run-creation.md)、[普通答复](user-interactions.md)、[受控写入](repository-effects.md)、[结果评价](results-evaluation.md)定义。

## 设计原则

等待/批准在 Workspace、概览与导航都有持久入口，不以弹窗为唯一通知；服务端筛选待办，不只查首页。等待释放 Worker lease，但有独立期限。

技术终态、业务完整性、引用与人工判断分开；子分析部分失败显式展示未覆盖范围。前端不合成没有服务器依据的准备进度、百分比或外部成功。

## 信息架构

### 导航层级

平台含概览、组织技能库、项目管理、账户与安全；当前项目含任务中心/调度、Workspace、历史、文档、资源管理。Composition 的 module/role/task_group 是展示，不是权限。

登录属于 App 认证状态，不是 Project/hash 页面。[账户入口](user-lifecycle.md)属于平台，不依赖项目选择；本人安全设置与 ADMIN 的组织用户管理分区显示。

### 路由

现有 [routing.ts](../../PJM/web/src/lib/routing.ts)：

| hash | 职责 |
| --- | --- |
| `#/` | 概览与待办 |
| `#/skills`、`#/projects` | 组织技能、项目管理 |
| `#/accounts` | 本人账户与安全、ADMIN 用户管理；不携带 Project/Run/Task 或账户搜索参数 |
| `#/tasks?project=<id>` | 任务、Preflight、调度 |
| `#/workspace?project=<id>&run=<id>` | 单 Run；task 参数可定位新任务 |
| `#/history?project=<id>` | 项目历史 |
| `#/documents?project=<id>`、`#/resources?project=<id>` | 文档、Integration/Secret/Binding/预授权 |

Interaction/Result 在 Workspace 内，无独立 path。显式无效 Project 保留目标、统一提示，不自动换项目；无参数初次选择、详情验证与失效恢复按[项目选择](project-lifecycle.md#项目选择与失效链接)处理。读取未确认时不挂载项目业务页面，账户等平台入口独立可用。路由不授予权限。

窄屏主导航通过菜单按钮展开完整入口，包括项目选择、语言与退出；收起内容不参与 Tab，Escape/选择后关闭并恢复焦点。项目列表失败与当前详情可读分别显示，归档目标有明确标记。切换项目清除旧 Run/Task 参数，返回和刷新仍按原 URL 目标验证。

项目管理的 ADMIN 成员页签遵循[成员页面规则](project-lifecycle.md#成员管理页面)：成员关系与账户状态分开、候选服务端分页、先确认原目标、未知后只读核对。账户与成员共用请求的防重、期限和旧响应隔离实现，各自保留领域拒绝规则。

项目 CRUD 也共用请求边界，活动与归档列表由同一完整读取投影、共用一个写入门禁；[原版本冲突与未知核对](project-lifecycle.md#并发修改不能只看有无行锁)保留原值/草稿/当前值，人工采用新版不自动提交。平台创建不依赖选中项目，首次默认选择到达不清空原草稿；成员和 module 仍使用真实项目授权边界。

### 工作空间布局

```text
项目/模块上下文
├── 辅助区：新建执行、历史
└── 当前 Run：状态、取消、等待
    ├── Conversation / Result / Events
    └── Evidence / Interaction / Proposal
```

窄屏收起辅助区仍保留状态/待办/结果，源码见 [WorkspacePage](../../PJM/web/src/pages/WorkspacePage.tsx)。

## 渲染模式

当前平台 standard 组件读取 Blueprint、可选输入 Schema 与 Outcome；assisted 表示信息不足，不保证独立 renderer。缺 ViewSpec 不阻止任务，generated 尚无执行链路。

## ViewSpec v1alpha1

ViewSpec 是展示契约，不定义能力或权限。必填字段见 [Schema](../../PJM/contracts/view-spec/v1alpha1.schema.json)；新增组件/actions 同步 validator/renderer，合法声明不证明所有行为已实现。

### 合法结构示例

以下通过现有结构约束；schema_ref 仍需在真实发布上下文解析，不是完整可发布 package。

```json
{
  "view_version": "projectmind.view/v1alpha1",
  "key": "standard-report",
  "title": "分析结果",
  "mode": "report",
  "input": {
    "schema_ref": "schemas/input.json",
    "layout": [{"component": "schema-form", "width": 12}]
  },
  "preflight": {
    "sections": ["selected_sources", "automatic_tools", "hard_denies", "limits"]
  },
  "result": {
    "sections": [
      {"component": "summary", "pointer": "/summary"},
      {"component": "evidence-panel", "source": "evidence_refs"}
    ]
  },
  "actions": ["run.execute", "run.cancel"]
}
```

JSON Pointer 仅读当前授权输入/结果；缺字段空状态，无任意表达式。未知 component/action 拒绝；未声明可回退，声明无效按发布 gate 处理。

## 现行组件与声明组件

[TaskLaunchFields](../../PJM/web/src/components/TaskLaunchFields.tsx)/taskDraft 为立即执行与调度共用，有 Schema 使用 SchemaTaskInput。能力/资源/权限 UI 不是可任意声明的组件协议。

Conversation、事件/Session、Interaction、Proposal 由平台组件负责；Task Center 不重复启动 SSE/取消状态机。RUNNING 不等于输入已物化，取消受理/终态不等于进程退出或用量结清，按[执行监督](run-supervision.md)显示现有事实。

[RunResultPanel](../../PJM/web/src/components/RunResultPanel.tsx)呈现 Outcome/Evidence/Evaluation；新增 run-flow 等 ViewSpec 名须先扩契约。[Flow](task-flow.md)只观察，不按建议节点猜进度。

## 任务启动与执行

Preflight 显示精确 SkillVersion/task、输入、资源与 readiness；服务端创建重验。文档单份/集合/全集均显式确认，可选未选省略槽位；复用 DocumentSourceField，候选变化不悄悄覆盖草稿。

创建已分开可编辑草稿与已发送 payload/幂等键；未知先确认原请求，明确确认后才另开 Run。关闭弹窗保留待确认请求，刷新/离页不承诺恢复，见[创建未知](run-creation.md#提交结果未知时的界面责任)。

创建后按 Run ID 读 detail/SSE，持久 sequence 恢复，TEXT_DELTA 不推进 replay cursor。冻结资源不能中途换绑，变化须新 Run；多个文档槽位物化成 Run 级并集，不是私有隔离目录，见[资源快照](resource-snapshots.md)。

## 结果与人工评价

摘要、交付、Findings、Evidence、限制、Proposal/Effect、Evaluation 分区。confidence 不是正确率，Artifact ref 不是已验证下载对象。

RunDocumentSnapshots 独立展示冻结槽位/模式/成员，即使尚无 Result；FROZEN 不代表已物化或仍可下载。缺失/历史不可用/校验失败与合法空数组区分，不查今天目录补旧事实，长 ID/hash 局部换行或滚动。

### Evaluation

AI 原值、修订建议、理由与历史并列；多建议不自动合并，不续行。精确指针/包络规则见[结果设计](results-evaluation.md)。

当前表单一次一条修订、历史无分页、POST 无原请求幂等，state/abort 未闭合全部竞态；未知不套 Run 创建的重放协议，按[评价界面责任](results-evaluation.md#提交未知与界面责任)接续。

## Task Center

列表使用服务端 task_id/latest_run 与精确版本，不由 UUID 或最近 N 条历史推导。已有选择任务、立即执行、创建/暂停/恢复/归档 Schedule；没有任意任务编辑/复制、对话建 Task 或结果比较。

ScheduleDialog 共用实际输入/文档字段；关闭销毁未保存草稿，换 actor/Project 不接收旧结果。修改 Schedule API/client 已有而页面入口缺失，需原配置/expected_row_version 与人工冲突比较。

当前只把前 100 条 Schedule 挂到可见 Task 卡片，失效任务规则可能无入口；目标独立管理列表、服务端分页/筛选。预览不创建 Run、不补造 end_at/max_runs 外次数；浏览器时区与规则 timezone 不能混同。

ONCE/CRON、重叠/错过/失效唯一规则见[调度](task-scheduling.md)。暂停不撤销已认领触发，run_count 不是成功数，last_run_at/last_run_id 未必同次，UI 不承诺全局串行或 exactly-once。

## 对话与调整

普通 CLARIFICATION/CHOICE/REVIEW、外部批准、Skill 追加调整各走独立协议。终态新目标创建新 Run，不自动建 child 关系或自然语言任意管理入口。

### 普通答复与续行状态

InteractionCard 是文本/选项，不换绑资源；有 Project 写权成员可答，外部批准身份不套所有问题。草稿与已发送原请求分开，复用共享防重、期限和旧响应隔离；待确认意图不因切标签、刷新详情或 OPEN 转为历史而丢失。

区分发送/确认/冲突/过期/未知；410 可能已提交过期并追加 Segment，原答复重放返回原续行与当前状态。读取详情不证明原 POST 成功；人工“确认原答复”沿原键与内容发送，可能完成此前未落库的首次提交。推荐不自动提交，required=false 不提供不存在的跳过，历史悬空批准只读。完整规则见[用户交互](user-interactions.md)。

### 审批请求与执行结果

批准卡片在 RunResultPanel，PendingActionsPanel 仅发现待办。提交精确 Proposal version/checksum/decision/reason，服务端限制 Run 发起人或 ADMIN。

当前每次点击换 key，丢响应后再点可能冲突。目标保留原 payload/key，未知先确认，不让新 reason/相反决定偷用原键；发出/abort 不等于批准撤销，APPROVED 不显示外部成功。

切换 actor/Project/Proposal、刷新和未知明确处理，不把正文/token 存浏览器持久层，不据错误码猜“commit 已完成/PR 未创建”。新增回执须先有[效果契约](repository-effects.md)。

## 项目文档管理的职责

文档页管理当前资产，Task 管草稿选择，Run 显示冻结事实。目录上传逐文件非整批事务；HTML 禁脚本不等于断网，列表消失不等于 blob/副本/备份清除。现有未知/同步防重/context 检查缺口见[文档设计](document-lifecycle.md)，不复刻协议。

## 生成展示与流程的交接

[生成模块](generated-modules.md)负责 builder、CSP、Host、版本/回退：现有业务 module API 和文档 iframe 不是生成入口，失败不得切换业务 SkillVersion。Flow 由[流程设计](task-flow.md)负责只读投影、冻结和事件关联，两者均不作为当前已完成能力。

## 可访问性、国际化与隐私

zh/ja/en catalog 与服务端用户偏好已存在；report_language 独立于 UI 语言。状态不用单一颜色，键盘、焦点、窄屏/长内容可读；不将敏感正文写入遥测，不承诺所有任意输出已有统一清洗。

## 回归验收

- 精确任务/版本、有无 Schema、文档显式范围/历史损坏分别验证；创建未知保留原身份。
- 资产部分成功/未知/换 context，调度分页/失效任务/版本冲突/时区用真实组件验。
- SSE 重连不重复、terminal 后结束；普通答复/批准/Evaluation 不互借幂等保证，原结果不变。
- 关闭弹窗仍有待办；410/未知/晚到与跨 actor 响应可解释，子分析失败范围不隐瞒。
- 三语、键盘、窄屏/长内容与焦点浏览器验收；Flow/generated 另走专项门禁。

源码与同步入口见[代码 README](../../PJM/README.md#web)、[变更指南](../development/change-guide.md)，完成状态只维护[计划 R10](../planning/roadmap.md#r10-全部-web-页面)。
