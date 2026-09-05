# Workspace 与 Web 交互设计

> 现行页面规范（旧 docs/07）。后续 Task Flow 与 generated FrontendModule 已拆到独立设计，当前未实现的操作在本页明确标注。

## 1. 设计原则

任务中心说明“能执行什么”，工作空间观察“这一次发生什么”。结果、证据、人工评价和外部批准分别呈现。

等待输入/批准必须在工作空间、概览与导航保留入口；等待不占用 Worker lease，但有独立交互/批准期限，不能写成“无限期等待”。列表在服务端筛选，避免只检查首页而漏掉旧待办。

并行子分析失败时，完成与未覆盖范围都要以文字和视觉状态区分。部分成功不能显示成全面核查完成。

## 2. 信息架构

### 2.1 导航层级

```text
平台
├── 概览
├── 技能库（组织资产）
└── 项目管理
当前项目
├── 工作空间（单个 Run）
├── 历史记录
├── 任务中心（任务、资源就绪度、调度）
├── 项目文档
└── 资源管理（Integration、Secret、Binding、预授权）
```

SkillComposition 的 module/role/task_group 是展示方式，不是系统角色或权限来源。Project 选择、模块过滤与 Run 选择分别保存上下文。

### 2.2 路由

当前使用 hash 路由，定义在 [routing.ts](../../PJM/web/src/lib/routing.ts)。

| 路由 | 职责 |
| --- | --- |
| `#/` | 概览：待办、最近运行、状态 |
| `#/tasks?project=<id>` | 任务中心：选择任务、配置启动与调度 |
| `#/workspace?project=<id>&run=<id>` | 观察指定 Run；新任务可用 task 参数定位 |
| `#/history?project=<id>` | 项目运行历史 |
| `#/documents?project=<id>` | 项目文档 |
| `#/resources?project=<id>` | 资源管理 |
| `#/skills` | 组织技能库 |
| `#/projects` | 项目管理 |

Run、Interaction、Result 在工作空间内展示，没有独立 path 路由。URL 明确指定了无效/无权 Project 时显示问题，不悄悄换项目。API 每次重新鉴权，前端路由不构成授权。

### 2.3 工作空间布局

```text
项目与模块上下文
┌──────────────────┬─────────────────────────────────┐
│ 新建执行          │ 当前 Run：状态、取消、等待入口      │
│ 历史列表          │ Conversation / Result / Events    │
│                  │ Evidence / Interaction / Proposal │
└──────────────────┴─────────────────────────────────┘
```

窄屏收起辅助区域，仍保留当前状态、待办和结果入口。详细实现见 [WorkspacePage](../../PJM/web/src/pages/WorkspacePage.tsx)。

## 3. 渲染模式

当前使用平台 standard 组件；assisted 表达信息不足/指导型内容，不保证存在独立 assisted renderer。generated 模式尚无实际运行链路。

Blueprint、可选输入 Schema 与 Outcome 驱动现有界面。缺少 ViewSpec 不阻塞普通任务；不能从“兼容一个声明”推导出“已有任意组件解释器”。

## 4. ViewSpec v1alpha1

### 4.1 作用

ViewSpec 是可选的展示契约，不定义业务能力或执行权限。新增字段/组件必须同步 Schema、validator 和 renderer，不能只修改文档示例。

### 4.2 字段

当前 [ViewSpec Schema](../../PJM/contracts/view-spec/v1alpha1.schema.json) 必填 view_version/key/title/mode/input/preflight/result；input 还必填 schema_ref/layout。preflight sections 和 actions 都是枚举，不是任意字符串。

### 4.3 合法结构示例

下例仅展示当前 Schema 的合法结构。input schema_ref 的真实目标仍需在发布上下文解析；不是可以直接发布的完整 Skill package。

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
      {"component": "finding-list", "pointer": "/findings"},
      {"component": "evidence-panel", "source": "evidence_refs"}
    ]
  },
  "actions": ["run.execute", "run.cancel"]
}
```

### 4.4 绑定规则

JSON Pointer 只读取当前任务或已授权结果。缺字段显示空状态，不执行任意表达式。未声明/未知 action 或 component 不能绕过 Schema；能通过 Schema 也不等于 renderer 已支持所有行为。缺少 ViewSpec 可回退，已声明的无效内容按发布 gate 处理。

## 5. 现行组件与声明组件

### 5.1 启动与准备

[TaskLaunchFields](../../PJM/web/src/components/TaskLaunchFields.tsx) 与 [taskDraft](../../PJM/web/src/lib/taskDraft.ts) 为立即执行和调度共用；有 Schema 时使用 [SchemaTaskInput](../../PJM/web/src/components/SchemaTaskInput.tsx)。能力摘要、资源选择和权限说明是平台 UI，不等于 ViewSpec 可任意声明这些组件名。

### 5.2 执行过程

Conversation、Run history、事件/会话时间线、Interaction 和 Proposal 详情由平台组件负责。Task Center 不启动第二套 SSE、取消和终态处理。

### 5.3 结果

[RunResultPanel](../../PJM/web/src/components/RunResultPanel.tsx) 呈现 Outcome、结构化数据、Evidence 和 Evaluation。ViewSpec 当前枚举包含 schema-form、text-field、enum-select、source-picker、summary、metric-grid、finding-list、data-table、evidence-panel、artifact-list、evaluation-form、raw-result。新增 capability-summary/run-flow 等声明名必须先扩契约。

## 6. 任务启动与执行

### 6.1 Preflight

显示精确 SkillVersion/task、输入、已选资源和就绪度。用户从合法候选选择，服务端创建时再次验证，不把 readiness 当作永久授权。

### 6.2 实时执行

创建后以 Run ID 获取 detail 并订阅 SSE。持久事件使用 sequence 恢复；TEXT_DELTA 不推进持久 replay cursor。刷新从服务端重建状态，不把浏览器存储当审计正本。

### 6.3 资源调整

Run 已冻结的 Integration/revision/权限不能在中途替换。需要变更时创建新 Run。document 当前实际全集物化的限制见[资源快照](resource-snapshots.md)，界面不应承诺尚未实现的单文档隔离。

## 7. 结果与人工评价

### 7.1 固定区域

摘要、交付物、Findings、Evidence、限制/待确认、Proposal/Effect 与 Evaluation 分开显示。Outcome PARTIAL/BLOCKED 与 Run 的技术终态不是同一个枚举。

### 7.2 Finding

依据 [OutcomeEnvelope](../../PJM/contracts/outcomes/envelope/v1.schema.json) 呈现。业务字段来自精确版本的可选契约，不添加 JAF 专用 renderer。

### 7.3 Evaluation

人工评分、comment 和 JSON Pointer revision 追加保存；original_value 由服务端从不可变 Result 取得，不能覆盖 AI 原值。

## 8. Task Center

### 8.1 列表信息

任务目标、来源 SkillVersion、就绪度、资源需求、关联调度与上次 Run。task_id/latest_run 由服务端返回，不从 UUID 规则或最近 N 条历史在前端推导。

### 8.2 操作

当前支持选择任务进入工作空间、配置立即执行、创建/修改 Schedule、暂停/恢复/归档 Schedule、查看关联历史。暂停的是调度，不是 Agent；任务复制、任意任务编辑、对话创建 Task Draft、流程编辑与结果比较不是已实现操作。

### 8.3 调度规则

ONCE/CRON、timezone、触发预览、错过/重叠/失效行为以 [TaskSchedule](task-scheduling.md) 为准。创建前的重叠查询不能保证并发创建绝不重叠，不在 UI 承诺严格串行或精确一次。

## 9. 对话与调整

当前支持运行中的 CLARIFICATION/CHOICE/REVIEW 响应、Proposal 审查与批准，以及 Skill 解释的显式追加调整。三者使用各自版本/幂等/权限协议。

终态后的继续分析创建新 Run。对话任意创建/编辑任务、自动建立 child Run 关联与自然语言任意执行管理操作属于后续设计，不作为现有功能说明。

## 10. FrontendModule 生命周期

后续目标与当前前置实现见[生成模块设计](generated-modules.md#目标与流水线)。不在本页重复维护第二套设计。

## 11. Build Sandbox

### 11.1 前置

固定依赖、锁文件、资源限制、无凭据、批准的内部 mirror；实际 builder 尚未实现。

### 11.2 静态检查

现有拒绝项不能代替完整网络与运行时隔离。详见[生成模块构建网络](generated-modules.md#构建网络)。

## 12. Runtime Sandbox 与 Host API

### 12.1 iframe 隔离

采用已决定的同主机专用路径、CSP 响应头与无 allow-same-origin iframe。旧“必须独立 Origin”不再是本项目当前设计；同主机剩余风险和 CSP 可执行性要求见[生成模块](generated-modules.md#origin-与-csp)。

### 12.2 Host 协议

尚未冻结。模块只请求受控展示与准备操作，不能直接调用 API 或批准 Proposal。

### 12.3 消息校验

来源窗口、channel nonce、精确模块版本、Project/Run 与 Schema 共同校验；opaque origin 的 null 不能用作可信身份。

## 13. 生成模块回退

尚未实现。目标是错误时关闭 iframe 回到 standard，业务记录不变；见[版本与回退](generated-modules.md#版本与回退)。

## 14. 可访问性、国际化与隐私

当前界面文案通过 zh/ja/en catalog，用户偏好由服务端保存；不再描述为“认证完成前中文单语”。report_language 是任务输出配置，独立于界面语言。

页面保留键盘操作、焦点和窄屏入口，状态不能仅靠颜色表达。历史测试不代替当前浏览器专项验收。敏感数据查看审计、全部输出格式清洗等如需扩展，须明确代码与验收，不把目标写成统一处理已经存在。

## 15. JAF 验收

只使用平台通用组件，呈现资源、规则、Evidence、Outcome、Review 与必要的 Redmine Proposal。指标与 30 case 的数据隔离见[JAF profile](../acceptance/jaf-quality.md)。

## 16. 回归验收

| 变化 | 验证 |
| --- | --- |
| 输入/任务选择 | 无业务 Schema 可启动；有 Schema 正确校验；精确 task/version |
| SSE/历史 | 断线重连不重复，terminal snapshot 后结束，旧结果可读 |
| 等待/批准 | 关闭弹窗后仍有待办；版本/期限/授权失败清晰 |
| Result/Evaluation | 原始结果不变，修订追加，Evidence 不跨 Run |
| 子分析 | 失败范围可见，不能表现为全部完成 |
| 语言/布局 | 三语、键盘、窄屏与长内容 |
| generated/Flow | 后续专项验收，不算入当前已实现功能 |

## 17. 实施入口

状态与下一步见[计划 §13](../planning/roadmap.md#13-当前执行状态)。代码定位与契约同步见[变更指南](../development/change-guide.md)。

## 18. Task Flow View（后续增强）

唯一详细设计见[Task Flow 与 Run Flow](task-flow.md)。首阶段使用现有数据展示资源、建议步骤、确认点和交付物；后续再引入版本化 Flow、Run 冻结与可选事件关联。不能从 STEP_* 自动猜测全部计划节点完成。
