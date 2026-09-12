# Workspace 与 Web 交互

任务中心选择“执行什么”，Workspace 观察“一次 Run”。本页负责布局与交互；提交协议见[创建](run-creation.md)、[答复](user-interactions.md)、[批准](repository-effects.md)、[评价](results-evaluation.md)。当前缺口统一见[计划 R10](../planning/roadmap.md#开发任务)。

## 设计原则

等待/批准有持久待办入口，服务端筛选而非只查首页；关窗不丢待办，等待释放 lease 但有独立期限。技术终态、业务完整性、引用与人工判断分开，子分析失败显示未覆盖范围；不虚构进度或外部成功。

## 信息架构

### 视觉规范

本节是全站画面风格的正本；新增页面延续现有工作台，不另起一套设计系统。目标是克制、清晰、适合长时间阅读，精致感来自留白、对齐和层级，不靠增加装饰或堆满信息。

- **外观**：暖灰夜间、暖白日间与少量琥珀色主操作，细边框、轻阴影；不增加大面积渐变或装饰卡片。图标复用[线性 SVG](../../SKM/web/src/components/routeIcons.tsx)，保持尺寸与笔画一致；定期执行使用日历＋执行图形。
- **层级与留白**：名称、状态、当前内容和下一步优先，每个操作区一个主要动作，其余降为次要操作。页头、提示、页签、工具栏、列表与分页明确分组，不贴边；技术字段成组对齐，长内容换行，不靠缩小字号塞入首屏。项目空态、失效或归档提示与正文保留 24px 间距。
- **导航**：跨页走侧栏，同页模式走页签；不在右上角重复增加“导入技能”“返回任务”等已有导航入口。侧栏不留品牌图标/文字占位，项目选择显示名称而非 key；菜单和底部操作共用纵向滚动区，不横向溢出。
- **信息密度**：概览突出待办与最近执行，技能默认列表、导入另开页签，任务突出执行入口。ID/版本/校验值收进技术详情，空闲的上传原请求查询与停止待发布操作收进帮助/高级入口；进行中的审批、失败和结果未知仍直接可见。
- **展开方式**：短说明用折叠，资源配置、证据和评价用抽屉，确认用小弹窗；不嵌套弹窗。Run 摘要完整展示，长报告与文档预览使用宽阅读区。展开只控制显示，不因主题或阅读详情切换丢失草稿/请求状态；创建与关闭草稿的具体规则见对应流程。
- **文案与状态**：标题、控件和内容已能表达的用途不再配常驻说明，内部存储、校验和协议实现留在开发文档。说明只保留会影响选择、操作后果或结果判断的信息，并在相关操作处简短呈现；权限、费用、未保存内容、失败与结果未知不能因精简而隐藏。用户填写的模块说明和业务内容保留。错误说明“哪里失败、原因和下一步”，可定位时指出文件/字段，技术码作为补充且不泄露敏感值；三语统一，不能日语界面直接漏出英文权限错误。状态配文字，只报告实际观测到的事实，例如 API 连接不等于所有服务正常。

登录页和主导航提供日间/夜间切换，首次默认夜间，不跟随系统。手动选择存当前浏览器 localStorage，跨页/刷新保留并同步同源页签；存储受限仍可在当前页切换。初始主题在应用加载前设置，切换不重建业务表单或请求 owner。

颜色与控件复用[共享样式](../../SKM/web/src/styles/base.css)，[阅读布局](../../SKM/web/src/styles/reading.css)和[页面层级](../../SKM/web/src/styles/presentation.css)不另造颜色。正文 14px、报告 15px 无衬线为基础，代码与 ID 才用等宽字体。以 PC 1440×900 为主，兼顾 1366×768、1920×1080 和中/日/英；双主题检查首屏、空态、多记录、长文本、键盘和焦点，手机保留基础回归、不裁掉操作。[全站检查](../../SKM/web/tests/browser/check_visual_style.py)与[阅读检查](../../SKM/web/tests/browser/check_reading.py)是隔离 mock，不能替代 Windows 字体、实际 Chrome/Edge 或真实部署验收。

### 导航层级

平台入口不依赖项目选择；[账户](user-lifecycle.md)分本人安全与 ADMIN 用户管理，登录属于 App 状态而非 hash 页面。项目内按下表导航；Composition 的 module/role/task_group 只影响展示，不授予权限。

### 路由

现有 [routing.ts](../../SKM/web/src/lib/routing.ts)：

| hash | 职责 |
| --- | --- |
| `#/` | 概览与待办 |
| `#/skills`、`#/projects` | 组织技能、项目管理 |
| `#/accounts` | 本人账户与安全、ADMIN 用户管理；不携带 Project/Run/Task 或账户搜索参数 |
| `#/tasks?project=<id>` | 任务、Preflight、创建调度与任务汇总 |
| `#/schedules?project=<id>` | 项目全部调度的分页、筛选、详情与原配置管理，不受 module 筛选影响 |
| `#/workspace?project=<id>&run=<id>` | 单 Run；task 参数可定位新任务 |
| `#/history?project=<id>` | 项目历史 |
| `#/documents?project=<id>`、`#/resources?project=<id>` | 文档、认证信息与访问权限 |

Interaction/Result 无独立 path。按[项目选择](project-lifecycle.md#项目选择与失效链接)验证精确目标：无效链接不换项目，读取未确认不挂载业务页，平台入口仍可用。切换项目清除旧 Run/Task 参数，刷新/返回重验原 URL；列表失败、详情可读和归档分别显示。

资源页将连接地址、认证信息和访问权限合并为一次添加，主页面只显示配置列表与添加入口。单独凭据管理、固定任务连接/收窄范围、自动批准规则放入默认折叠的高级设置，已有设置继续生效。任务沿用资源匹配与启动选择，不要求先手工创建绑定；后台权限、冻结和批准检查仍按既有规则执行。

窄屏菜单保留全部入口、项目选择、语言与退出；收起内容不可 Tab，Escape/选择后关闭并恢复焦点。

[成员页](project-lifecycle.md#成员管理页面)与账户共用防重、期限和旧响应隔离，成员关系不等于账户状态。项目 CRUD 共用写入门禁；[冲突/未知](project-lifecycle.md#并发修改不能只看有无行锁)保留原值、草稿和当前值，人工采用版本不自动提交。平台创建不依赖选中项目，默认选择到达不清空草稿。

[Module 共享配置/解绑](skill-contract.md#共享更新与删除的区别)仅 ADMIN 可写；client 核对原 Project/module 和成功状态。错身份、坏响应、断连或取消均不证明回滚，不自动重试；管理页的完整未知隔离仍待接续。

### 工作空间布局

```text
项目/模块上下文
├── 辅助区：新建执行、历史
└── 当前 Run：状态、取消、等待
    ├── Conversation / Result / Events
    └── Evidence / Interaction / Proposal
```

未选择 Run 时保留辅助区；选择后改为工具栏，完成状态不重复显示连接信息。结果主画面保留摘要、人工复核/格式状态及保存时检查范围的短说明；历史缺失/不合法记录不补造通过。检查全文、ID/版本/置信度、证据与评价使用右侧抽屉，长报告留在主画面，不套小弹窗。发现事项可直接查看对应的原 Run 证据，未解析引用只显示文字。源码见 [WorkspacePage](../../SKM/web/src/pages/WorkspacePage.tsx)。

## 渲染模式

standard 读取 Blueprint、可选输入 Schema 与 Outcome；assisted 不保证独立 renderer。缺 ViewSpec 不阻止任务，generated 尚无执行链路。

## ViewSpec v1alpha1

ViewSpec 不授予能力或权限；字段见 [Schema](../../SKM/contracts/view-spec/v1alpha1.schema.json)。新增组件/actions 须同步 validator/renderer，合法声明不等于行为已实现。

### 合法结构示例

以下仅示范合法结构；schema_ref 还须在发布上下文解析。

```json
{
  "view_version": "skillmind.view/v1alpha1",
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

[TaskLaunchFields](../../SKM/web/src/components/TaskLaunchFields.tsx)/taskDraft 共用于立即执行和调度，有 Schema 使用 SchemaTaskInput；能力/资源/权限 UI 不是任意声明协议。

Conversation、Session、Interaction、Proposal 由平台负责，Task Center 不另建 SSE/取消状态机。按[执行监督](run-supervision.md)区分 RUNNING/已物化、取消受理/进程退出/用量结清。

[RunResultPanel](../../SKM/web/src/components/RunResultPanel.tsx)呈现结果/证据/评价；[Flow](task-flow.md)只观察，新增 ViewSpec 组件先扩契约。

## 任务启动与执行

Preflight 显示精确版本/Task、输入、资源与 readiness，创建时服务端重验。DocumentSourceField 共用显式单份/集合/全集选择，可选未选省略；候选变化不覆盖草稿。

草稿与已发送 payload/key 分开；[创建未知](run-creation.md#提交结果未知时的界面责任)先确认原请求，关窗保留待确认项，刷新/离页不承诺恢复。创建后按 Run ID 读 detail/SSE，以持久 sequence 重放，TEXT_DELTA 不推进 cursor。

[冻结资源](resource-snapshots.md)不能中途换绑，变化须新 Run；文档槽位物化为 Run 级并集，不是槽位私有目录。

## 结果与人工评价

摘要、交付、Findings、Evidence、限制、Proposal/Effect、Evaluation 分区；confidence 不是正确率。附件按同 Project/Run 发布索引区分采用/未采用，无保存记录不可下载；保持保存时 v1/v2 原义，字节与请求校验见[可信附件](results-evaluation.md#可信附件的发布与读取)。

RunDocumentSnapshots 无 Result 也展示冻结槽位/模式/成员；FROZEN 不代表已物化或可下载。缺失/损坏与合法空数组分开，不拿当前目录补历史，长 ID/hash 局部换行或滚动。

### Evaluation

结果顶部进入评价抽屉，原值、修订、理由与分页历史保持区分；多修订不自动合并或续行，null 与不存在分开。抽屉开关只控制显示，不卸载草稿/请求 owner；关窗后原提交阶段及恢复入口留在结果页。按[评价界面责任](results-evaluation.md#提交未知与界面责任)确认或人工原样重发，相似历史不证明原提交。归档只读，换身份/结果不迁移动作。

## Task Center

列表采用服务端 task_id/latest_run 和精确版本，不从 UUID/最近历史推导；支持执行与 Schedule 管理，不提供任意 Task 编辑/复制、对话建 Task 或结果比较。

[只读预览](task-flow.md#只读任务预览)展示单 Task，分开任务声明、Skill 共享要求、来源与全蓝图 readiness；未评估不显示“无需资源”，不启动 Run 或猜步骤状态。

ScheduleDialog 共用输入/文档字段；关闭创建销毁未保存草稿，切换 actor/Project 隔离旧响应。独立[调度管理](task-scheduling.md#保存后的管理入口)使用服务端分页/筛选，失效精确任务仍可读、不跟随 latest。冲突/未知保留原请求与草稿，人工采用版本后继续，重开不绕门禁，读取拒绝即关闭写入。

[在途核对](task-scheduling.md#在途只读核对)分开读取时刻、配置版本、认领/租约，旧协议/失败/未见 PENDING 不混同；刷新不重发。预览不造 Run 或限额外次数，浏览器时区不等于规则 timezone。暂停不撤回已认领项，lease 到期不证明停止，run_count 非成功数，last_* 未必同次；不承诺全局串行或 exactly-once。

## 对话与调整

普通答复、外部批准、Skill 调整各走独立协议；终态新目标建新 Run，不自动建 child 关系或开放自然语言管理。

### 普通答复与续行状态

InteractionCard 由有 Project 写权成员回答文本/选项，不换绑资源或套用批准身份。共享防重/期限/旧响应隔离，保留草稿与原请求；切标签、刷新或转历史不丢待确认意图。

按[用户交互](user-interactions.md)区分发送/确认/冲突/过期/未知：410 可能已追加 Segment，GET 不证明原 POST 成功；人工原键/内容确认也可能完成首次提交。推荐不自动提交，required=false 不虚构跳过，悬空批准只读。

### 审批请求与执行结果

批准卡片在 RunResultPanel，PendingActionsPanel 只发现待办；提交精确 version/checksum/decision/reason，仅 Run 发起人或 ADMIN 可批。

当前点击会换 key；目标按[效果契约](repository-effects.md)保留原 payload/key、先核对未知，新理由/相反决定不占原键。发送/abort 不等于撤销，APPROVED 不等于外部成功。换 actor/Project/Proposal 和刷新须隔离旧动作，不持久保存正文/token，也不按错误码猜远端阶段。

## 项目文档管理的职责

文档页管当前资产，Task 管草稿选择，Run 管冻结事实。目录上传非整批事务，HTML 禁脚本不等于断网，列表消失不等于字节/备份清除；协议见[文档设计](document-lifecycle.md)。

## 生成展示与流程的交接

[生成模块](generated-modules.md)负责 builder/CSP/Host/展示回退，不复用业务 module API 或文档 iframe，失败不切业务版本。[Flow](task-flow.md)负责计划/事实投影；未完成链路不由本页补造。

## 可访问性、国际化与隐私

zh/ja/en 与用户偏好共用 catalog，report_language 独立。弹窗/抽屉共用焦点循环、Escape 与关窗后焦点返回，不叠加弹窗；窄屏抽屉占满宽度，关闭不等于取消请求。状态不能只靠颜色，键盘/窄屏/长内容可用；正文不入遥测，不宣称任意输出已统一清洗。

## 回归验收

- 精确任务/版本、有无 Schema、文档显式范围/历史损坏分别验证；创建未知保留原身份。
- 资产部分成功/未知/换 context，调度分页/失效任务/版本冲突/时区用真实组件验。
- SSE 重连不重复、terminal 后结束；普通答复/批准/Evaluation 不互借幂等保证，原结果不变。
- 关闭弹窗仍有待办；410/未知/晚到与跨 actor 响应可解释，子分析失败范围不隐瞒。
- 三语、键盘、窄屏/长内容与焦点浏览器验收；Flow/generated 另走专项门禁。

实现与同步见[代码 README](../../SKM/README.md#web)、[变更指南](../development/change-guide.md)。
