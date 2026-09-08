# 01 ProjectMind 统一规划

> **历史快照（2026-08-04）**：以下保留再编前的设计和状态，包含已被现行规格修正的表述，不再是开发依据。当前范围见[实施计划](../planning/roadmap.md)，规则见[文档目录](../README.md)。下文“正本”“当前”等均指当时。

> 本文件是 ProjectMind 产品方向、架构原则、工作包规划和实施顺序的唯一规划正本。
> 已完成工作包的实施记录追加到 `delivery-history.md`。本文是计划正本，不复制历史正文。
>
> 2026-08-04 状态摘要：基础执行闭环、通用 Skill 解释与能力蓝图、资源绑定、交互式 Run、受控外部效果、
> Skill 库作用域、界面三语化和托管凭据已经交付。资源物化、代码仓库回写、解释器过程重表达、任务调度、
> 并行 multi-agent 与前端信息架构已完成实现并部署；剩余主要是外部系统、浏览器和模型专项验收。
>
> 当前产品实现焦点由能力名称直接表达：任务流程视图已完成规格规划，下一步是只读流程投影；generated
> FrontendModule 已完成方向决策和部分安全前置，剩余构建沙箱与首次执行威胁模型尚未开始。
>
## 1. 产品定位

ProjectMind 是一个 Skill 驱动的项目管理 AI Agent 平台。

平台不预设品质、进度、风险等固定业务模块，而是通过 Skill 动态形成能力、可执行任务和工作空间。JAF 品质分析是首个验证场景，不是平台的固定模块。

核心目标：

- 优先兼容 Codex、Claude Code 等编码 Agent 可使用的目录式 Skill。
- 将通用 Skill 转换为可验证、可执行、可审计的平台能力。
- 通过可配置的 SkillComposition 组合多个 Skill。
- 根据 Project 启用的 Skill 动态生成 Workspace。
- 统一管理数据源、权限、Secret 和 Tool。
- 确保 AI 结论具备证据、置信度以及人工评价和修订记录。

## 2. 核心原则

### 2.1 开放 Skill 输入，契约化执行

外部 Skill 不需要预先符合 ProjectMind 专有格式。

平台使用系统级 Skill Interpreter 理解通用 Skill，先生成 CapabilityBlueprint，再投影为内部 RuntimeManifest。不同 Skill 的信息完整度和可包装效果允许不同；缺少业务 Schema 不等于无法执行，不能完整自动化时可降级为指导型或需要配置的能力。

Interpreter 不要求用户在首次生成前逐项确认。系统直接生成可运行的 Draft/Preview，让用户先看到模块和任务效果，再通过对话、表单或选择项提出修改并重新生成。每次生成保留版本和差异，可回滚到历史结果。

SkillSource、Interpreter 或模型版本发生变化时，平台只提示存在可重新解释的版本，不自动改写当前 SkillVersion。重新解释必须由用户主动发起。

### 2.2 Skill 决定能力和数据需求

Skill 经过解释后定义：

- 能做什么、何时触发以及希望达到什么目标。
- 前提需要哪些 Ticket、代码、文档、文件或其他资源。
- 分析过程中必须遵守的规则、推荐步骤、质量标准和禁止事项。
- 交付报告、结构化结论、补丁或外部变更提案等什么结果。
- 何时需要用户澄清、选择、Review 或批准。
- 需要哪些 Tool capability，以及只读、提案或实际写入的效果意图。
- 可选的动态参数/结果 Schema 和标准结果视图。

Skill 定义可用数据及选择原则，AI 在当前 Project 已配置和授权的数据源中选择实际来源。用户认为选择不合适时，可以通过对话或选择框覆盖本次选择，并可将结果保存为项目默认偏好。

### 2.3 Project 配置可用资源

Project 保存：

- 已启用的 SkillComposition 和 Skill 版本。
- 可供 AI 选择的 Integration 及项目默认偏好。
- 项目知识、字段映射和覆盖配置。
- 项目成员和授权范围。

Skill 不保存连接地址、账号、密码和 API Key。

### 2.4 Tool 负责安全访问

Tool 和 Integration 负责 Provider 适配、认证、权限校验、Secret 使用、数据标准化、证据引用和调用审计。

### 2.5 脚本权限和关键结果

MVP 不对正常只读 Tool 调用逐次询问。平台内置只读 Tool，以及已发布 SkillVersion 中登记、checksum 匹配且参数通过校验的受控能力，在当前 Run 固定的项目、Integration、工作目录和资源限制内默认自动执行。

自动执行不等于取消安全边界：未登记能力、宿主机任意 Shell、越出受控 workspace、读取 Secret、切换未绑定 Integration 和扩大网络范围由平台硬拒绝。隔离 workspace read/search 已按 ExecutionProfile 开放；edit 和受控 sandbox command 仍须独立工作包，`cwd` 本身不作为安全边界。

外部效果采用 `observe → propose → apply`：只读并先生成报告/变更提案；实际更新默认进入 `ask → wait → resume`，也可由 ADMIN 对明确 capability、Integration 和 scope 配置无需询问的预授权，但**仅 LOW 风险效果可预授权**（其余风险等级始终经人工批准）。任何写入仍须使用注册 Provider、幂等键、原子前置版本校验和 read-back Evidence。可 apply 的写入是注册済み两条：通过 CAS discovery 的 Redmine `issue.update/v1`，以及 git 的 `repository.write/v1`（§20，落地形态限于 platform 预约 namespace 的新分支 commit，**不可预授权**，始终经人工批准）。svn 写入与其他未注册写入继续禁止。

### 2.6 部署和数据默认策略

- 首期部署在 Linux，使用 Docker Compose。
- 部署环境已存在共享 Traefik。ProjectMind Compose 不创建 Traefik 服务，不配置专用 entryPoint、证书解析器、Dashboard 或 Docker Socket。
- ProjectMind 的 `web` 和 `api` 仅加入运维提供的 external edge network，并按环境提供的域名声明路由；所有 ProjectMind 容器均不发布宿主机端口。
- 对外只经现有 Traefik 的 HTTPS 入口访问：同一域名下 `${PROJECTMIND_CONTEXT_PATH}/api/*`（包含 SSE）转发到 `api`，`${PROJECTMIND_CONTEXT_PATH}/*` 转发到 `web`。Traefik 转发前去除 context path，FastAPI 通过 `root_path` 恢复外部 URL；HTTP 到 HTTPS、TLS 和可选 HTTP/3 由现有 Traefik 统一负责。
- Worker、Sandbox、PostgreSQL、Redis、Object Storage、SessionStore 和 Secret Storage 只位于 ProjectMind 内部网络。基础执行闭环产生的 Artifact 通过 API 下载，不直接暴露 MinIO。
- 建立独立用户体系，不依赖外部统一认证。
- 项目数据默认允许发送给已配置模型。
- 模型输入、输出、证据和 Run 的保存期限由 Project 配置。

### 2.7 Agent 能力最大化，平台中介权威

前述原则可归结为一条主线：**最大化 Agent 的认知与发现能力，同时由平台中介其对外部副作用与凭据的权威。**

- **认知与发现层——尽量放开**。Agent 应获得通用、强大的工具面（隔离 workspace 的 read/search、物化资源树上的自主发现、报告/补丁/变更提案的产出），自行决定读什么、如何分析、提出何种变更，而非被拆成大量按动作切分的窄能力。§19 的物化正是为此：给 Agent 更强的实际能力，同时守住边界。
- **权威层——中介，不移除**。凭据只在 Provider 边界内解析（§2.4）；外部效果一律经 `observe → propose → apply`（§2.5）。Agent 从不直接持有凭据、不直接写生产系统、不获得任意 Shell 或网络出口。
- **边界约束的是「权威如何行使」，不是「能达成什么结果」**。回写 Redmine 可以做到全自动（LOW 风险 + 显式预授权），但 Agent 始终只提案、由平台的 EffectExecution 落地。因此有界与强大在此互补：**边界正是让这份能力可上生产（可审计、可复现、最小权限、可回滚）的前提，而非对 Agent 的削弱。**

这条原则统摄 §2.2（Skill 声明需求）、§2.4（Tool 中介访问）、§2.5（脚本权限与外部效果），并作为 capability 词表、资源物化（§19）与受控回写（§20）设计的共同判据。

## 3. 核心模型

```text
SkillSource
  = 导入的原始通用 Skill、附件、来源和版本摘要

SkillInterpretation
  = Interpreter 对 Skill 的理解、假设、风险和置信度

CapabilityBlueprint
  = 能力、目标、资源前提、指导规则、交付物、交互点和效果意图

RuntimeManifest
  = 已发布能力蓝图的运行投影、Tool requirement、执行策略与可选 Schema

Skill
  = 原始 Skill 与已发布 Runtime Manifest 的版本化能力

SystemRole
  = 真实用户的系统权限；MVP 只有 ADMIN 和 USER

SkillComposition
  = 多个 Skill + 组合配置 + 默认流程 + 展示形态

VirtualRole / Module
  = SkillComposition 的不同展示形态，不是独立权限实体

Project
  = 项目信息 + SkillComposition + Integration + 选择偏好 + 知识 + 配置

Integration
  = 具体数据源实例及其非敏感配置和 Secret 引用

Tool
  = 对外部能力的安全、统一封装

Workspace
  = 根据项目启用的 Skill 动态生成的入口和标准视图

Run
  = 一个持续目标的执行线程，可包含多个交互 Segment、Session、Tool、证据和结果

RunSegment
  = 初次启动、用户答复、Review 或批准触发的一段连续工作

RunAttempt
  = 同一 Segment 的 Worker 技术执行、重试或故障接管记录

AgentSession
  = Run 中具体 AgentEngine 会话；同一 Run 可顺序 resume、fork 或替换多个 Session
```

Skill 与 SkillComposition 解耦。同一个 Skill 可以被多个组合复用，同一个组合也可切换为“虚拟角色”或“功能模块”的展示方式。

MVP 的组织结构为：

```text
单一 Organization
├── 多个 User
│   └── SystemRole: ADMIN | USER
└── 多个 Project
    ├── 多个 SkillComposition
    │   └── presentation: role | module | task_group
    └── 多个 ExecutableTask
```

系统权限和 Skill 组合必须分开：

- `ADMIN` 可管理用户、项目、Integration、Skill、发布和系统配置。
- `USER` 可使用已授权的项目、SkillComposition 和任务，不拥有系统管理权。
- 软件工程师、测试工程师、架构师、品质分析等不是系统权限角色，而是可编辑的 SkillComposition 模板。
- SkillComposition 可配置名称、说明、图标、人格/行为提示、Skill 列表、默认任务、执行流程和展示形态。
- SkillComposition 本身不授予系统权限；实际可执行范围由 SystemRole、Project 授权、Skill/Tool 策略共同决定。

### 3.1 业务结构图

[查看独立业务结构图](../overview/business-structure.html)

### 3.2 领域模型、字段与权限

[查看领域模型与权限设计](../design/domain-model.md)

## 4. 通用 Skill 兼容机制

[查看通用 Skill 兼容与发布规范](../design/skill-contract.md)

### 4.1 导入和解释流程

```text
导入通用 Skill
  ↓
保存不可变 SkillSource
  ↓
Skill Interpreter 分析
  ↓
生成 SkillInterpretation + CapabilityBlueprint
  ↓
投影 TaskBlueprint / ResourceRequirement / RuntimeManifest / 可选 ViewSpec
  ↓
自动静态校验、安全诊断和 Workspace 预览
  ↓
用户查看效果并通过对话或配置调整
  ↓
重新生成或发布为 SkillVersion
```

Interpreter 应提取或生成：

- 领域能力、触发意图、目标和成功条件。
- Ticket、repository、document 等抽象资源要求与选择原则。
- required rules、recommended steps、quality criteria 和禁止事项。
- 报告、Artifact、结构化结果或 ChangeProposal 等交付物。
- 澄清、选择、Review 与效果批准等用户交互点。
- Tool requirement 和 observe/propose/apply 效果意图。
- 可选动态参数/结果契约与 ViewSpec；不能因缺少业务 Schema/View/fixture 直接拒绝。
- 假设、风险、缺失信息和置信度。

### 4.2 兼容级别

- `Native`：Skill 的通用结构可由确定性 Adapter 直接映射为能力蓝图，无需模型补充核心语义。
- `Adapted`：Interpreter 从自然语言规则生成可验证 CapabilityBlueprint 和运行投影。
- `Assisted`：需要较多假设、配置或人工步骤，但仍可发布为指导型能力。

兼容级别与运行就绪度分离。Task 可按当前 Project 的资源绑定投影为
`GUIDANCE_ONLY / CONFIGURATION_REQUIRED / RUNNABLE / ACTIONABLE`；领域 capability 不需要预先存在于 Tool catalog。

### 4.3 安全边界

Interpreter 不能自动创建 Secret、扩大权限或访问未授权的数据源。

导入或生成的 Tool 和脚本在执行前应用权限策略：

- `auto`：MVP 默认；只允许平台注册 Tool 和已发布、checksum 匹配的脚本在 Run 快照范围内自动执行。
- `ask`：保留给外部写操作或 ADMIN 明确配置的高风险操作；当前基础分析链路不对普通只读 Tool 逐次询问。
- `deny`：未登记能力、越权参数、任意 Shell、Secret 访问和未授权网络等硬拒绝项。

当前标准版本仅使用标准 ViewSpec，不生成或运行 FrontendModule。生成前端模块保留为后续增强能力，必须在独立威胁模型、构建沙箱和 Host API 契约通过专项验证后再启用。

### 4.4 “创建模块”的含义

MVP 中根据 Skill 创建模块，包括：

- Workspace 导航入口。
- 可执行任务和快捷操作。
- Schema 驱动输入表单。
- 标准结果 ViewSpec；受控前端模块代码延后实现。
- 数据源配置要求。
- Tool 和权限申请清单。

不生成后端服务和数据库表。后端能力必须由现有 Runtime、Tool、通用存储和配置模型提供。

## 5. 数据源和 Tool

### 5.1 职责模型

```text
Skill DataSourceRequirement + SelectionPolicy
  → AI 从 Project 可用 Integration 中选择
  → 用户通过对话或选择框覆盖
  → Integration Provider
  → Tool Capability
```

例如：

```yaml
# 資源要求は CapabilityBlueprint が唯一の宣言元(manifest に data_sources は無い)。
resource_requirements:
  - key: issue_source
    kind: issue
    required: true
    access: read
    capabilities: [issue.read/v1]
    accepted_providers: [redmine, csv]

  - key: source_repository
    kind: repository
    required: false
    access: read
    capabilities: [repository.read/v1]
    accepted_providers: [svn, git]
```

同一个 Skill 可以在不同项目中配置不同资源，运行时由 AI 按 Skill 规则和任务上下文选择：

```text
Project A
├── issue_source → JAF Redmine
└── source_repository → JAF SVN

Project B
├── issue_source → issues.csv
└── source_repository → Git Repository
```

Tool capability 必须有版本化的最小请求、响应、分页、错误和 Evidence 契约，例如 `issue.read/v1`、`repository.read/v1`。Provider Adapter 必须通过该 capability 的 conformance test，保证 Redmine/CSV 或 SVN/Git 的核心语义可以互换；Provider 特有字段放入受控 `extensions`。Skill 仍负责业务字段含义和选择标准，但不能依赖未声明的 Provider 私有结构。

### 5.2 Tool Capability

已注册并可执行（Provider 已配线）：

```text
issue.read/v1          issue.update/v1        repository.read/v1
document.read/v1       workspace.read/v1      workspace.search/v1
workspace.write/v1     interaction.request/v1 change.propose/v1
```

**本节早期列举的逐动作能力**（`issue.search`、`repository.search`、`repository.history`、
`document.search`）**不再作为实现方向**：§19.2 已决定按「资源边界」而非「动作」切分——资源在 Run 准备
资源物化进 `input/`，检索与历史由既有 `workspace.search/read` 加物化时生成的索引/历史文件承载，逐动作
切分会让每种访问模式都要一个完整工作包。`tabular.import`、`knowledge.search`、`report.export` 仍是愿望项，
未列入任何已批准工作包；真实需求出现时按同一原则先判断能否落在资源边界上（例如查找表已由「物化文档 +
`workspace.search`」覆盖）。

执行前必须验证：

- 必需的数据源已经配置。
- Provider 属于 Skill 可接受范围。
- Integration 提供所需 capability。
- SystemRole、项目成员关系和 Skill/Tool 策略均允许调用。
- 数据满足 Skill 自己定义的输入契约。
- 脚本和写操作符合当前权限策略。

## 6. Agent Runtime 与默认执行引擎

[查看 Agent Runtime 与证据执行链路规范](../design/agent-runtime.md)

### 6.1 运行流程

```text
接收用户请求
  ↓
读取 Project、SkillComposition、权限和上下文
  ↓
从已启用 Skill 中检索候选任务
  ↓
根据触发条件、输入兼容性和权限过滤
  ↓
加载已发布 Runtime Manifest
  ↓
AI 选择数据源，允许用户覆盖，并创建 Run
  ↓
执行 Workflow 和 Tool
  ↓
收集 Evidence 并校验结构化输出
  ↓
显示标准结果视图
  ↓
用户查看、评价、修订或继续对话
  ↓
保存完整审计记录
```

模型负责候选任务、数据源和执行路径的选择，但不能绕过权限和兼容性校验。用户可以通过对话或选择框修改选择；修改可仅作用于本次 Run，也可保存为 Project 默认值。

### 6.2 AgentEngine 与 Claude Agent SDK

MVP 默认使用 Python 版 Claude Agent SDK 作为 AI 任务执行引擎。ProjectMind 不直接将业务层绑定到 SDK，而是定义可替换的 AgentEngine 接口：

```text
ProjectMind Run Orchestrator
  → AgentEngine
      → ClaudeAgentSdkEngine
          → Claude Agent SDK
              → claude CLI subprocess
```

AgentEngine 最小能力：

```text
execute(run_context)
resume(agent_session_id, user_input)
fork(agent_session_id, user_input)
interrupt(agent_session_id)
stream_events(run_id, after_sequence)
```

MVP 正常只读 Tool 和登记脚本由 PermissionService 根据 Run 快照自动批准，并继续记录决定。越出快照的请求直接拒绝；未来 `ask` 流程再通过追加决定和 resume 恢复 Session，不直接修改 AgentEngine。

职责边界：

| 组件 | 主要职责 |
|---|---|
| ProjectMind | Task 调度、Project 权限、Run 状态、数据源选择、证据、审计、评价和长期存储 |
| AgentEngine | 隔离具体 Agent SDK，统一执行、恢复、中断、工具审批和事件流 |
| Claude Agent SDK | Agent Loop、上下文管理、工具选择、Skills、MCP、Hooks、Session 恢复和流式消息 |
| Tool Gateway | 将 Redmine、SVN、Git、CSV 等能力以受控 MCP Tool 暴露给 SDK |

映射关系：

| ProjectMind | Claude Agent SDK |
|---|---|
| SkillComposition | system prompt + Skills + MCP tools + 执行配置 |
| ExecutableTask | query prompt + ClaudeAgentOptions |
| Run | SDK Session 与 ProjectMind 审计快照 |
| RunStep / ToolCall | SDK streaming message 和 tool use 事件 |
| `ask` 权限 | `permission_mode=default` + `can_use_tool` + hooks |
| `auto` 权限 | 精确 `allowed_tools` + 参数级 hooks，不使用全局 `bypassPermissions` |
| 对话继续 | `resume=session_id` |
| 备选方案 | fork session |

### 6.3 Session 与工作目录策略

- 一个活动 Run 对应一个 Claude SDK Session 和一个 `claude` 子进程。
- 每个 Run 使用独立 `cwd`，禁止不同项目或 Run 共享可写工作目录。
- 同一 Run 的继续对话使用 `resume`；探索替代方案使用 `fork`。
- 后续引入周期任务时，每次创建新 Run 和新 SDK Session，按 Skill 规则注入上次运行摘要，避免会话无限膨胀。
- Docker Compose 部署采用 Hybrid Session：运行时启动子进程，空闲或等待权限时释放，继续对话时从 PostgreSQL SessionStore 恢复。
- 每个 Worker 默认只运行一个活动 Session，通过增加 Worker 副本扩容。
- ProjectMind 数据库是 Run 的权威记录；SDK transcript 仅作为 Agent 会话恢复数据。
- 每个 Run 保存 `agent_engine`、`sdk_session_id`、`cwd`、SDK/CLI 版本、模型、SkillVersion、权限快照、成本和用量。

### 6.4 最小运行对象

```text
Run
├── AgentSession
├── RunAttempt
├── RunStep
├── ToolCall
├── PermissionDecision
├── Evidence
├── Artifact
├── Result
└── Confirmation
```

平台必须能够回答：

> 谁在什么项目中，使用哪个 Skill 和 Interpreter 版本，访问了什么数据，经过哪些步骤，产生了什么结论，最后由谁确认或修改。

## 7. 动态 Workspace

[查看动态 Workspace 与前端模块规范](../design/workspace.md)

当前 Workspace 使用标准组件；Interpreter 生成的受控前端模块属于后续增强：

- 对话工作台和 Schema 输入表单。
- 表格、列表、指标和趋势图。
- 风险、课题和审查意见。
- 报告文档。
- 证据与引用。
- 人工评价和修订表单。
- Run 步骤与审计记录。

每个任务模块应展示 AI 计划执行的步骤、当前步骤、使用的数据源、自动执行范围、硬拒绝结果、运行结果和历史记录。无法匹配专用标准组件时，降级为通用 JSON、Markdown 或表格视图。

### 7.1 任务中心交互

ProjectMind 建立独立任务中心。初始版本先实现立即执行和 Run 历史；指定时间与周期执行已由 §22 交付，条件监控仍不规划。

目标能力包括：

- 立即执行、指定时间执行、周期执行和条件监控。
- 查看任务定义、AI 执行步骤、下一次运行和历史 Run。
- 编辑、暂停、恢复、重新执行和删除。
- 通过对话调整任务，也可使用结构化选择框覆盖数据源和执行选项。
- 记住前次运行状态；监控任务满足结束条件后自动停止。

## 8. 初始产品范围（MVP）

这里的 MVP 表示首个可用产品范围。

### 8.1 基础执行闭环（已完成）

工程骨架首先只打通一条可部署、可审计的纵向链路：

- 单一任务 `jaf.ticket.analyze`，只支持立即执行。
- 使用手工冻结并通过 Schema 校验的 Native RuntimeManifest；Interpreter 在基础闭环之后接入。
- 使用 CSV/Git 固定 fixture 验证 `issue.read/v1` 和 `repository.read/v1`，随后再接 Redmine/SVN。
- 只使用 standard ViewSpec，不生成 FrontendModule。
- 只读 Tool 和已发布、checksum 匹配的脚本在 Run 快照范围内自动执行，不逐次询问。
- 跑通 Run、RunAttempt、AgentSession、ToolCall、Evidence、Result 和 Evaluation。
- 跑通 SSE 重连、幂等创建、取消、失败终态和 Worker 重试。
- 使用锁定的 Claude Agent SDK/CLI 完成 defer/resume、SessionStore、interrupt 和崩溃恢复兼容性探针；探针不要求正常业务流程进入 ask。
- 落地 RuntimeManifest、ViewSpec、Tool capability、JAF input/output、RunEvent 和 API error 的真实版本化 Schema，以及 OpenAPI 基线。

基础执行闭环完成后才扩展通用导入、Interpreter 和真实外部 Provider，避免同时验证过多不确定边界。

### 8.2 MVP 后续增量

- 单组织运行，核心数据保留 `organization_id`。
- Linux + Docker Compose 部署。
- 复用部署环境已有 Traefik；ProjectMind Compose 不包含专用 Traefik，且不发布任何宿主机端口。
- 独立用户体系。
- 单组织 → 多用户 → 多项目 → 多 SkillComposition → 多任务。
- 系统角色只有 `ADMIN` 和 `USER`。
- 可配置 SkillComposition，并可以虚拟角色、功能模块或任务组的形式展示。
- 通用 Skill 导入、原文保存和版本管理。
- 兼容 Codex、Claude Code 等目录式 Skill。
- Skill Interpreter 和 Runtime Manifest。
- Native、Adapted、Assisted 兼容状态。
- 自动生成 Draft/Preview，并支持对话调整、重新生成、版本比较和回滚。
- Project 创建、Integration 配置和 AI 数据源选择。
- Secret 引用和日志脱敏。
- 项目启用 Skill 和受控任务路由。
- 单 Skill 执行和有限串行任务接口。
- AgentEngine 抽象和默认 `ClaudeAgentSdkEngine`。
- Python 版 Claude Agent SDK、Session 恢复和流式事件处理。
- 任务步骤展示和任务中心。
- Tool 和登记脚本默认自动执行，并通过 Run 快照、参数 Schema、Hook、沙箱和审计收口。
- Redmine、CSV、SVN、Git 只读适配。
- Run、Evidence、Result 和 Confirmation 审计链路。
- 动态任务入口和标准结果视图。
- JAF 单 Ticket 品质分析闭环。
- 项目级数据发送和保存期限配置。

### 8.3 暂不实现

- 开放式 Skill 市场。
- 完整可视化工作流设计器。
- Agent 自由生成循环和并行流程。
- 多 Agent 长时间自主协商。
- JAF 功能、应用、担当者和两类汇总任务；保留为单 Ticket 闭环后的扩展。
- FrontendModule 代码生成、构建沙箱和 generated 渲染模式。
- 条件监控任务；一次性和周期调度已由 `docs/01` §22 独立交付。
- 后端服务和数据库表生成。
- 默认回写 Redmine。
- **未经审批**的自动修改或提交源代码（经 ChangeProposal 审批后的分支 commit 已由 §20 落地）。
- 完整多租户计费体系。
- JAF 之外的第二个 Skill 验收场景。

## 9. JAF 首个验证场景

[查看 JAF Skill 迁移与端到端验收规范](../acceptance/jaf-quality.md)

### 9.1 目标流程

```text
用户输入 Ticket ID 或导入记录标识
  ↓
AI 按 Skill 标准从 Project 可用来源中选择票据数据，用户可覆盖
  ↓
按需从 SVN 或 Git 读取代码和历史资料
  ↓
分析原因工程、原因内容和根本原因
  ↓
显示字段级证据、置信度、假设和待确认项
  ↓
用户修改并确认
  ↓
保存原始建议、修改内容和确认记录
```

### 9.2 Skill 迁移要求

- 项目知识和详细规则放入 Skill references。
- CSV 解析、字段转换和确定性校验放入 scripts。
- 数据访问统一使用 Tool。
- Skill 定义票据源、代码源、选择标准和需要读取的历史范围。
- AI 在 Redmine/CSV、SVN/Git 等已配置来源中选择，用户可覆盖。
- 由 Interpreter 生成输入输出 TaskContractDraft，并以通用验收测试评价语义质量。
- 原始 AI 输出与人工修改分别保存。

### 9.3 建议输出

JAF 输出字段不在平台规划中固定。Interpreter 根据当前 JAF Skill 生成 output TaskContractDraft，
平台只要求结果符合冻结契约、关键结论关联 Evidence，并保留人工评价和原始结果。

## 10. 早期建设路径（归档说明）

本节记录项目早期从基础闭环到首个 JAF 验收的建设路径。它用于解释为什么形成现有架构，不是当前进度表；当前状态和下一步只看 §13。

### 基础执行契约、SDK 探针和工程基线

- 冻结单 Ticket Walking Skeleton 和暂缓事项。
- 建立版本化 JSON Schema、Tool capability contract、OpenAPI 与数据库迁移基线。
- 明确 Run/RunAttempt 状态机、幂等键、租约和终态不可变规则。
- 锁定 Claude Agent SDK/CLI，验证 SessionStore、defer/resume、interrupt 和恢复。
- 验证每 Run 文件系统边界；在证明隔离前不向 Agent 暴露可越界的内置文件工具。
- 确定队列实现、Secret Storage、认证会话和保留策略。
- 定义既有 Traefik 接入契约：公开域名、external network、HTTPS entryPoint、统一 context path、`{contextPath}/api` 路由和 SSE 超时/心跳；不创建 Traefik 服务。

完成标准：Schema 示例可自动校验，状态转换测试和 SDK 兼容性探针通过，安全边界有可重复测试。

### MVP 范围和验收样例

- 冻结 MVP 范围和不做事项。
- 以 JAF 品质分析作为唯一通用 Skill 验收样例。
- 由 JAF Skill 定义历史 Ticket 的数量、选取规则和分析标准。
- 以报告形式输出准确度结果，由人工评价。
- 确认 Docker Compose、独立用户体系和项目数据保留配置。
- 确认既有 Traefik 提供的 external network、公开域名和 HTTPS entryPoint；证书与入口策略不进入 ProjectMind 配置。

完成标准：不存在会改变核心数据模型或安全边界的未决问题。

### 领域与安全模型

- 定义核心实体和数据库初稿。
- 定义 ADMIN/USER 权限矩阵、项目成员关系和 Skill/Tool 策略。
- 定义 SkillComposition 及其 role/module/task_group 展示形态。
- 定义 Secret、脱敏和审计规则。
- 定义 Run 状态机。

完成标准：可以完整追踪一次 Skill 导入、解释、发布和执行。

### 通用 Skill 兼容层

- 实现 SkillSource 和 Interpreter。
- 定义 SkillInterpretation 和 Runtime Manifest Schema。
- 定义 Capability 和 ExecutableTask。
- 定义兼容级别、校验器和契约测试。

完成标准：无需修改平台源码即可导入 JAF Ticket Skill，生成通过 Schema 校验的可运行任务和标准 ViewSpec 预览，并能通过对话调整后重新生成。

### Runtime 和 Tool

- 实现 AgentEngine 接口和 Python `ClaudeAgentSdkEngine`。
- 实现 Runtime 执行链路、AI 数据源选择和 SDK 事件到 Run 的映射。
- 实现 SessionStore、独立 Run 工作目录、resume/fork/interrupt。
- 实现 `auto/deny` 到 Claude Agent SDK `tools`、`allowed_tools`、hooks 和 Tool Gateway 的映射；保留 `ask` 兼容性探针和后续扩展接口。
- 实现 Tool SDK。
- 实现 Redmine、CSV、SVN、Git 只读适配器。
- 实现 Run、ToolCall 和 Evidence 审计。

完成标准：成功或失败的执行均可重建步骤和数据来源。

### 动态 Workspace

- 实现 Skill 任务入口和 Schema 输入表单。
- 实现标准结果、证据、人工评价和修订视图。
- 实现立即执行任务和 Run 历史视图。

完成标准：JAF 单 Ticket Skill 仅依赖 standard ViewSpec 即可完成输入、执行、结果、证据和评价闭环。

### JAF 端到端验证

- 迁移 JAF Skill。
- 按 JAF Skill 定义的数量和规则建立历史 Ticket 评价集。
- 实现 Ticket 分析端到端自动化测试。
- 生成分析准确度报告并提供人工评价入口。

完成标准：

- 输出全部通过 Schema 校验。
- 关键结论有证据或明确标记证据不足。
- 人工修改不覆盖原始结果。
- 执行记录可审计和导出。

### 工程化和试运行

- 创建前后端和共享 Schema 工程。
- 建立数据库迁移和自动化测试。
- 建立日志、监控、取消和失败重试。
- 建立备份、恢复和版本回滚方案。
- Git 初始化或远程仓库不是当前开工门禁；依赖锁文件、Schema checksum、迁移和构建版本仍必须保证可复现。

完成标准：安全和稳定性检查通过，可进行内部试用。

## 11. 技术架构图与部署结构

[查看独立技术架构图](../overview/technical-architecture.html)

## 12. 已确认决策和剩余设计项

已确认：

- MVP 使用 Python 版 Claude Agent SDK 作为默认执行引擎，但通过 AgentEngine 保留替换能力。
- ProjectMind 负责 Task/Run/权限/审计，Claude Agent SDK 负责 Agent Loop、Session、Skills、MCP 和工具选择。
- SDK 使用持久化 SessionStore 和每 Run 独立工作目录；同一非终态 Run 可以包含多个顺序 Session。
- 默认 ExecutionProfile 为 SUPERVISED：Agent 在冻结目标和资源内自主规划，只读/工作区能力按 capability 开放，不使用全局 `bypassPermissions`。
- 兼容目标是 Codex、Claude Code 等可使用的通用 Skill。
- Interpreter 首次生成无需人工确认，采用先预览、再对话调整和重新生成。
- 正常只读 Tool 默认自动执行，越权请求硬拒绝；外部效果默认 ask，项目可对低风险、明确 scope 的注册能力配置预授权。
- 不生成后端服务和数据库表；当前标准版本不生成前端模块代码。
- 项目数据默认可发送给模型，保存期限由 Project 配置。
- Linux + Docker Compose 部署，使用独立用户体系。
- 复用环境已有共享 Traefik；ProjectMind 只声明 `web`/`api` 路由和 external network 接入，不维护 Traefik 静态配置、证书、Dashboard 或 Docker API 权限。
- MVP 为单组织、多用户、多项目、多 SkillComposition、多任务。
- 系统角色只有 `ADMIN` 和 `USER`。
- 虚拟角色、模块和任务组统一为可配置 SkillComposition，只在展示形态上区分。
- Skill 提供能力、目标、资源前提、指导规则、交付物和效果意图；Interpreter 生成能力蓝图，AI 推荐实际数据源，用户可以覆盖。
- Interpreter 只在用户主动操作时重新解释，不自动更新已发布版本。
- MVP 只使用 JAF 品质分析 Skill；历史 Ticket 数量和规则由 Skill 定义，准确度通过报告供人工评价。
- 初始 JAF 闭环只实现单 Ticket 立即执行；其余 5 类任务和 generated FrontendModule 后续再做，调度现已由 §22 交付。
- Run 保存不可变目标和权限上限；用户答复/批准追加 RunSegment，技术恢复/重试/接管追加 RunAttempt，终态 Run 不重新打开。
- Tool capability 使用版本化规范结构，Provider Adapter 必须通过 conformance test。
- Git 初始化和远程上传不作为当前工程骨架门禁。
- Web 界面在认证落地前保持中文单语，不预先引入文案 key 基建；中/日/英三语资源化与语言切换作为 MVP 验收前的专门增量，在用户体系可保存语言偏好后实施。`report_language` 与界面语言解耦。

剩余设计项将在后续工作重点中确定：

- 项目数据默认保存期限。
- 认证会话、首个 ADMIN 初始化和 Secret Storage 已按 [认证、会话与 Secret Storage 决策](../design/authentication.md) 冻结；认证实现按该决策执行。

## 13. 当前执行状态

总体目标：以完成的 task execution spine 为稳定底座，完成通用 Skill 从导入、解释、验证、发布到执行的闭环。

| 能力主线 | 状态 | 已交付内容 / 下一步 |
|---|---|---|
| Task Execution | 已交付 | Run/Attempt、Outbox、Worker lease/recovery、AgentEngine、受控 Tool、Evidence、Result、Evaluation、SSE、取消、认证授权和运维链路已打通。 |
| Deterministic Skill Import | 已交付 | Directory/Generic adapter、NormalizedSkillPackage、Assisted draft、SkillSource/Interpretation 持久化已实现。 |
| SkillVersion 与发布基础 | 已交付 | DRAFT、Manifest hard gate、warning acceptance、精确 PUBLISHED 版本和 RunSkillSnapshot 已实现。 |
| Model Skill Interpreter | 已交付 | system Skill、静态分析、结构化 interpretation、修订/diff 和质量验证已接通。 |
| Generic Skill Runtime Bridge | 已交付 | published task、通用 Run、Runtime 和 Workspace 路径已接通。 |
| Dynamic Task Contract | 已交付 | 已去除 JAF/repository-review 活动业务 Schema；Interpreter 生成并冻结可选任务契约；真实部署 smoke 已通过。 |
| Capability Blueprint | 已交付 | CapabilityBlueprint 是 Interpreter 的唯一来源；资源要求就绪度已接入 TaskCatalog/API/Web；历史兼容投影不再作为新来源。 |
| Interactive Agent Runtime | 已交付 | Integration/三级 ResourceBinding、AgentTaskBrief、OutcomeEnvelope、隔离 workspace read/search、RunSegment、顺序 Session、ChangeProposal、默认批准/LOW-only 预授权、Redmine CAS apply 与 read-back Evidence 已闭环。 |
| Skill 库作用域（§16） | 已交付 | Skill 资产归 Organization；Project 通过精确 PUBLISHED SkillVersion 显式启用；TaskCatalog、Run 和 Web 管理已同步。 |
| 界面三语化（§17） | 已交付 | 用户语言偏好、zh/ja/en catalog、切换器、页面文案资源化和基础浏览器验收已完成。 |
| 托管凭据（§18） | 已交付 | `MANAGED` resolver、KEK 信封加密、密文独立存储和录入→提议→批准→apply 链路已完成。 |
| 资源快照物化（§19） | 已部署，待专项验收 | 文档与 repository 物化、workspace 写入、索引/历史/二进制文本化和 Brief 落点说明已完成；服务器基线已通过；剩余真实 HTTPS remote、凭据和模型行为验收。 |
| 代码仓库回写（§20） | 已部署，待专项验收 | git/svn 受控写入、Proposal/Approval、分支或 direct 模式、read-back Evidence 和 Web 审批展示已完成；服务器基线已通过；剩余真实 forge 与端到端落地验收。 |
| 解释器过程重表达（§21） | 已部署，待专项验收 | 声明与授权分离、步骤按能力重表达、未注册能力降档守卫和已实现资源能力映射已完成；服务器基线已通过；剩余 jaf 实机越门与模型行为验收。 |
| 任务调度（§22） | 已部署，待专项验收 | 一次性/周期规则、时区与 DST、CAS 发火、API、Worker 和 Web 面板已完成；migration 已应用，Worker tick 运行正常；剩余实际 schedule 触发观测。 |
| 并行 multi-agent（§23） | 已部署，待专项验收 | 只读子 Agent、能力收窄、预算切分、失败隔离、结果呈现和子 Session 持久化已完成；migration 已应用；剩余模型使用效果观测。 |
| 前端信息架构（§25） | 已部署，待专项验收 | Task Center、服务端待办筛选、导航徽标、概览优先位、决定性 task 关联和三语/窄屏整理已完成；Web 服务已部署；剩余真实浏览器验收。 |
| 任务流程视图（§26） | 规格完成，未开始实现 | 已冻结对象层次、流程节点边界、流程快照和五个实现工作包；下一步是只读流程投影，不改变 Agent 执行语义。 |
| generated FrontendModule（§24） | 规划已定，部分实现 | 方向、安全边界、版本冻结和静态拒绝路径已完成；构建沙箱、首次执行威胁模型和运行时回退尚未开始。 |

### 13.1 基础执行闭环完成定义

基础执行闭环以 `jaf.ticket.analyze` 作为 acceptance profile，已经证明：

- authenticated Project-scoped Run 可以幂等创建、执行、取消、失败恢复并进入可审计终态；
- published SkillVersion、Manifest checksum、权限、输入和数据源选择被固定在 Run snapshot；
- 注册只读 Tool 产生 ToolCall 与 Evidence，validated Result 不可变，人工修改以 Evaluation 追加；
- PostgreSQL 是正本，Redis 只承载 queue、短期通知和 lock；SSE 可持久化 replay；
- Docker Compose、共享 Traefik、migration、backup/restore 和 smoke 路径已经建立。

后续变更不得把基础执行闭环从“已交付”改回功能开发状态。未完成的 Compose 环境复验属于发布操作检查，不改变功能完成判断；实际执行记录继续维护在 [运维 Runbook](../operations/runbook.md) 和实施计划中。

### 13.2 下一步与当前决策

- §21 解释器过程重表达已完成部署基线验收（脚本编排 Skill 不再被静态门挡、步骤按三档重表达、守卫降档不拒发）；待 jaf 实机越门与模型行为验收。
- §19 资源快照物化已完成部署基线验收：document/repository 两路物化、真实 git/svn 客户端、文件索引、提交历史和 xlsx 文本化均就位；待真实 HTTPS remote、凭据与模型行为验收。
- §20 代码仓库回写已完成部署基线验收：git/svn 双侧均可在审批后落地，默认 `write_mode=direct` 直接进既定分支，设 `branch` 才走 `projectmind/` 预约分支与 PR。PR 自动合并、多仓库原子变更仍是长期非目标；待真实 forge 与端到端落地验收。
- §22 任务调度已完成部署基线验收：migration `0025` 已随当前链应用，Worker tick 已运行且最近检查无失败；待可控 schedule 的实际触发、重叠跳过与错过处理观测。
- **剩余四条线**：
  1. **专项验收（需业务数据或外部系统）**：执行 §19.5 的基础资源验收与仓库资源验收，同批完成解释器实机越门、模型行为观测和 §20 的端到端「提案 → 批准 → 落地」。服务器部署基线已经完成；§22 的 migration 与 tick 基线也已完成，仍需实际 schedule 触发观测。
  2. **§26 任务流程视图（产品实现侧）**：规格规划已完成，下一步进入现有数据的只读流程投影，再实现可版本化的流程 Draft 和 Run 流程观察。该建设不改变 Agent 的自主执行语义，不引入严格 DAG 执行控制器，也不依赖 generated FrontendModule。
  3. **§23 并行 multi-agent（专项验收）**：只读子 Agent、预算切分、失败隔离、结果呈现和子 Session 持久化已完成，migration `0027` 已随当前链应用；待模型扇出与汇总效果观测。
  4. **§24 generated FrontendModule**：方向和安全边界已定，版本冻结与静态拒绝路径已本地完成；构建沙箱需要内部 npm 镜像，首次执行前必须完成威胁模型，剩余实现暂未开始。
- **残余的分类**：见 `docs/12` §35.2。服务器镜像、Compose 服务、migration head、PostgreSQL/Redis preflight 和 Worker tick 基线已经通过；当前残余集中在真实资源/外部系统、模型行为、认证 smoke 与浏览器专项验收。
- Dynamic Task Contract 降为可选参数/结果结构，不再作为 Skill 语义中心；缺少业务 Schema 不影响能力发布。
- Agent 默认使用 SUPERVISED profile，在平台硬边界内自主调整分析步骤；固定 workflow 只保留 required rule 和质量门禁。
- 用户澄清、观点 Review 和外部效果批准在同一非终态 Run 中追加 RunSegment；Worker 故障才追加 RunAttempt。
- 外部写入按 observe/propose/apply 分层，默认批准后执行；受控预授权可配置，但任意 Shell、未知 Tool 和 scope 外写入继续硬拒绝。
- 详细实现边界以
  [Skills 能力蓝图与交互式 Runtime 实施规范](../design/skill-interpretation.md)
  为准；唯一实施顺序维护在本文件 §15。

## 14. 动态 Task Contract 与业务 Schema 去预定义化（已交付）

已完成。Interpreter 生成并冻结可选任务契约，JAF/repository-review 的活动业务 Schema 已从新执行路径移除。

计划正文与实施记录见 [`delivery-history.md`](delivery-history.md) §10、§29.1。

## 15. 能力蓝图与交互式 Agent Runtime

### 15.1 目标

ProjectMind 的核心不再是“把 Skill 包装成一份固定业务表单”，而是把 Skill 中的能力完整传递给
Agent，并由平台提供资源、权限、交互和审计边界：

```text
通用 SkillSource
  → Interpreter 识别能力、目标、资源、指导、交付物与效果意图
  → CapabilityBlueprint Preview / 调整 / 发布
  → Project 绑定 Redmine、Git/SVN、文档或文件资源
  → Run 冻结 AgentTaskBrief 与 ExecutionProfile
  → Agent 在硬边界内自主规划和调用 Tool
  → 必要时询问用户、请求 Review 或提出 ChangeProposal
  → 新 RunSegment + resume/fork/new AgentSession
  → 结果、证据、外部效果验证和人工评价
```

本主线明确三个区别：

- 领域 capability 不等于 Tool capability。新业务能力无需先登记到 Tool catalog，实际访问才要求
  `issue.read/v1`、`repository.read/v1` 等注册能力。
- RunSegment 不等于 RunAttempt。用户答复/批准产生 Segment；Worker 重试/接管产生 Attempt。
- effect intent 不等于 permission。Skill 和 Agent 可以提出写入，但只有平台策略和有效批准可以执行。

### 15.2 目标体验

以 Ticket 分析为例，Skill 可以声明“读取障害票与相关代码，按规则分析字段，生成报告，并在用户
同意后更新 Redmine”。平台据此：

1. 显示需要 issue source 和可选 repository，推荐当前项目可用的 Redmine/Git/SVN Integration。
2. 将 Skill 原始指导、必需规则、目标和资源绑定冻结给 Worker。
3. Agent 自主决定读取顺序、交叉验证和报告组织，不被固定步骤过度限制。
4. 需要用户观点或资源选择时暂停 Run，在页面通过对话/选择框继续。
5. 对 Redmine 更新先展示结构化 Proposal；默认批准后执行，也可由 ADMIN 为明确低风险范围配置预授权。
6. 执行后回读目标对象并保存 before/after Evidence。

代码 Review 同理：资源可以是 Git、SVN 或项目文件；交付物可以是报告和 patch candidate。真实提交只在
相应 write Provider、并发校验和回滚策略已由 §20 就位（git），Agent 仍不能直接运行 `git commit`/`svn commit`——落地只在 Provider 边界内。

### 15.3 实施工作包

#### 规格与领域边界

- 更新 docs/01/04/05/06/07/08/10/11、02/03 HTML 图与 AGENTS。
- 将交付历史收敛为纯记录（现 `docs/12`），不保留 active state 文件。
- 冻结 CapabilityBlueprint、ResourceRequirement、AgentTaskBrief、RunSegment、UserInteraction 和
  ChangeProposal 的职责边界。

完成门禁：正式文档中不再把固定业务 Schema、单会话和“全部 Agent 文件能力永久禁用”作为长期方向。

#### CapabilityBlueprint Interpreter

- 新增通用 Blueprint contract、DTO、validator、source trace 和 checksum。
- 升级 `projectmind-skill-interpreter`，输出能力、目标、资源、指导、交付物、交互点和效果意图。
- 把旧 RuntimeManifest 通过兼容 projector 映射为 Blueprint；历史 PUBLISHED 版本不原地修改。
- Preview/diff 页面按蓝图维度展示，不再只显示 Schema 和 gate finding。

完成门禁：JAF、repository-review、development-readiness 三类普通 Skill 不添加平台业务 Schema 即可
生成可发布能力蓝图。

#### ResourceBinding 与 readiness（已交付）

- 实现 ResourceRequirement 到 Integration/ProjectKnowledge/用户输入的候选匹配和选择。
- 支持 Project 默认、Task 配置和 Run preflight 覆盖，并冻结 provider、revision 和 scope。
- TaskCatalog 与 Workspace 显示 `GUIDANCE_ONLY / CONFIGURATION_REQUIRED / RUNNABLE / ACTIONABLE`。

完成门禁：同一 SkillVersion 在不同项目根据资源配置得到正确就绪度，缺少资源提供明确选择入口而不是
模糊 hard gate。

交付结果：0021 增加 SecretReference、Integration 与 Project default / Task / immutable Run
ResourceBinding。Provider catalog 固定 capability、是否可写、Secret 要求和 scope 规则；ADMIN API/Web
只公开 locator/config 的 metadata，不返回正文。Run preflight 按 Task → Project default → Run override
解析后冻结 provider、Integration revision、scope 与 canonical checksum，Agent Tool 必须使用精确
`binding_id`。TaskCatalog 将 Integration 候选与 Project 文档候选组合计算 readiness；未知、禁用、越界
或无明确 write scope 的绑定均 fail closed。

#### AgentTaskBrief 与受控自主（已交付）

- 冻结完整 Skill guidance、目标、成功条件、资源、Tool、效果策略和 checkpoint 给 Worker。
- 引入 `GUIDED / SUPERVISED / DELEGATED` ExecutionProfile，默认 SUPERVISED。
- ResultValidator 以通用 OutcomeEnvelope + Skill quality criteria 为基础，task-specific Schema 可选。
- 按能力逐步开放隔离 workspace 的 read/search，再独立评估 edit 和 sandbox command。

完成门禁：Agent 可自主组织 Ticket 分析和代码 Review，同时 required rule、Evidence 和权限边界不退化。

交付结果：新 Run 固定平台 `OutcomeEnvelope`，task-specific output Schema 仅在稳定机器消费字段存在时
作为 `structured_data` 的附加约束；无业务 Schema 不再导致 `structured_output_missing`。Result 持久化
区分历史 `STRUCTURED_OUTPUT` 与新 `OUTCOME_ENVELOPE`，冻结 Evidence/Artifact 引用及可选 Schema
identity。`workspace.read/v1`、`workspace.search/v1` 通过注册 MCP Provider 开放，只能读取当前 Run 的
`input/` 与 `workspace/`，拒绝 symlink/路径逃逸并限制 file、byte、result 数；SDK 内置
Read/Glob/Grep/Bash/Write/Edit/Web 继续 hard deny。ExecutionProfile 同时固定进 permission snapshot，
`SUPERVISED` 以下不能取得 workspace search。edit 与 sandbox command 仍需独立威胁模型和实施工作包。

#### RunSegment、用户交互和顺序多 Session（已交付）

- 增加 RunSegment、AgentTaskBrief snapshot、UserInteraction/Response 的 migration、repository 和 API。
- Run 状态增加 WAITING_FOR_INPUT/WAITING_FOR_APPROVAL，等待时释放 Worker lease。
- 支持同一 Run 的 resume/fork/replace Session，并记录 parent/checkpoint/engine identity。
- Web 增加 Segment/Session/Tool/Interaction 的详情抽屉和结构化响应组件。

完成门禁：一个 Run 可完成“Session A 分析 → 用户提供观点 → Session B 继续 → Result”，故障接管只
新增 Attempt，审计时间线和 SSE 可恢复。

交付结果：0020 以不回填业务猜测的方式增加 RunSegment、AgentTaskBrief snapshot、UserInteraction/
Response 与 Session lineage；新 Run 显式创建 Segment 1，旧 Run 只在 read model 投影隐式 Segment 1。
`interaction.request/v1` 经 SDK `defer` 停止计费执行，在同一 transaction 保存公开问题、期限与
checkpoint，Run 进入 WAITING_FOR_INPUT/WAITING_FOR_APPROVAL，Attempt 变为 DEFERRED 并释放 lease。
经 Project/actor/CSRF、version、expiry 和 idempotency 校验的回答完成旧 Segment、创建下一 Segment 并
重新 dispatch；Worker 依 continuation mode 选择 resume/fork/replace，且 partial unique constraint 禁止
同 Run 并行 ACTIVE Session。Run detail/API/Web 已显示 Segment、Attempt、Session、Interaction 时间线，
CLARIFICATION/CHOICE/REVIEW 可在详情中结构化回答。完成门禁由“Session A → Review → fork Session B →
Result”执行器测试、仓储 sequence/lease 测试与 Web contract test 固定。

#### ChangeProposal 与受控外部效果（已部署，待专项验收）

- 实现 ChangeProposal、批准/拒绝、有效期、预授权匹配和 EffectExecution。
- 首选实现 Redmine `issue.update/v1`；repository 第一版只生成 patch/commit proposal。
- 所有写入使用幂等键、optimistic concurrency 和 read-back Evidence。
- 默认 ask；ADMIN 可以为明确 capability + Integration + operation + risk + scope 配置直接执行。

完成门禁：未批准、过期、stale、越界和重放均在 Provider 前安全处理；成功效果有 before/after 证据。

交付结果：0021 增加 ChangeProposal、ChangeApproval、LOW-only EffectPreauthorization、EffectExecution，
并让 Result 固定 `change_proposal_refs`。Agent 仅通过 `change.propose/v1` 产生无权限 candidate；平台重新
核对 Blueprint effect intent、冻结 Binding checksum、Integration capability/scope、Evidence 所有权、
Proposal version/checksum、期限和 actor。用户批准限 Run 发起人或 system ADMIN；显式预授权必须精确
匹配 Redmine `issue.update/v1` + Integration + `update_fields` + LOW + scope，其他情况保持 ask。

Redmine apply 使用版本化 `projectmind.redmine-effect/v1` CAS adapter：先验证 discovery 声明的原子
`revision` 前置条件与 idempotency 支持，再 pre-read、以同一 idempotency key 原子写入、最后 read-back；
标准 Redmine issue REST endpoint 不满足该平台写入门禁时在写前拒绝。成功保存 before/after Evidence；
stale、transport retry、verification failure 和 lease recovery 保持同一 EffectExecution/幂等键并有独立
状态。普通 Interaction 与 effect approval 的期限由 recovery cron 分别闭合，均创建审计 Segment，且不
推断默认回答或批准。Git/SVN 仍只输出 Outcome 中的 patch/commit proposal，不启用提交 Provider。

API/Web 已提供 SecretReference、Integration、ResourceBinding、预授权管理，以及 Proposal exact
version/checksum 批准、diff、Approval/Effect/read-back 展示。Contract、OpenAPI、Backend/Web 类型和
安全测试同步；服务器 migration 与基础运行检查已通过，CAS adapter 联调与网络级 effect smoke 仍归入端到端验收工作包。

#### 端到端与质量评估（部署验收与模型质量）

- 在真实 PostgreSQL 和部署 Compose 中验证 import→interpret→bind→run→interaction→result/effect。
- 固定样例各重复至少三次，评价能力识别、规则保真、证据充分、交付质量和人工调整量。
- 比较固定 workflow 与 SUPERVISED profile，确认放宽执行策略没有降低安全和可审计性。
- 回写 `docs/history/delivery-history.md`，不创建新的 active state 文档。

完成门禁：新增通用 Skill 无需平台业务代码/Schema 即可形成能力，并能安全完成资源绑定、交互式多会话
执行和受控效果。

当前状态与剩余执行顺序（事实记录见交付履历 §24/§25）：

1. **模型反复补测（本机可执行）**：`.env` 已配置模型 credential，2026-07-18 首轮 300 秒实测
   5/6 候选 `model_timeout`、跑完的 2 件解释结构均有效——瓶颈是 endpoint 时延而非输出质量。
   用 `--timeout-seconds 900` 完整重跑 2 个固定样例 × 3 次（预计 30–90 分钟），核对
   `structural_valid_rate=1.0` 且 `semantically_stable=true`。
2. **人工质量评审**：门禁通过后，对 field path、review item 与调整量做人工语义评审，并完成
   固定 workflow 与 SUPERVISED profile 的对比结论。
3. **实机 PostgreSQL（部分完成）**：服务器已完成 migration head `0028_skill_source_file_index`，
   PostgreSQL/Redis preflight 返回 `ready`；9 项真实 DB invariant 测试尚未在部署镜像执行，仍需带测试集的
   验收环境补跑。
4. **Compose 部署验收（基础完成）**：镜像 archive、镜像 ID、Compose 服务健康、migration 和 preflight
   已通过；`import→interpret→bind→run→interaction→result` 的 authenticated smoke 仍需应用 ADMIN 账号、
   Project UUID 和可用 published task 才能执行。
5. **Redmine CAS adapter 联调（需部署环境）**：discovery → pre-read → 原子 CAS write →
   read-back 的 effect smoke。
6. 全部完成后回写交付履历、更新本节与 §13 状态，并将文档「現在の段階」推进到下一主线决策。

### 15.4 发布与兼容记录

1. **Blueprint 双读兼容**（已完成，后续收回）：新增蓝图和兼容 projector，旧 Runtime 不变。
2. **ResourceBinding 与 AgentTaskBrief**（已完成）：新任务使用资源绑定和 AgentTaskBrief；旧任务继续单 Segment。
3. **Interactive Run**（已完成）：启用 waiting state、RunSegment、用户交互和顺序多 Session。
4. **Controlled Effects**（已部署，待专项验收）：只为通过专项门禁的 Redmine CAS Provider 启用
   `issue.update/v1` apply；Git/SVN 和其他能力保持 propose。
5. **蓝图来源收敛**（已完成）：移除仅为旧解释投影存在的临时双读代码，保留历史 snapshot reader。

正式发布前不存在需要兼容的历史解释，因此蓝图双读已提前收回：蓝图以 Interpreter 为
唯一来源，manifest 未声明蓝图即无法发行。RunSegment 双读已启用：新 Run 必须有显式 row，历史 Run
继续由只读 implicit Segment projector 保持可读，不反向捏造 migration 数据。

每次发布都必须保持基础执行闭环产生的历史 Run 可读、终态不可变和当前生产路径 fail closed。

### 15.5 非目标

- 定时、周期、监控和预约任务。
- generated FrontendModule 或任意前端代码执行。
- 并行 multi-agent/sub-agent 编排。
- 任意宿主 Shell、开放网络或来源脚本直接执行。
- 未经 Proposal 与批准/预授权的外部系统写入。
- JAF 其余 5 类任务、Skill marketplace 和自动版本升级。

## 16. Skill 库作用域：Organization 资产与 Project 启用（已交付）

Skill 库作用域已完成。Skill 资产 lifecycle 归 Organization；TaskCatalog 与新 Run 的可见性以 Project 显式启用的精确 PUBLISHED SkillVersion 为准（migration 0017/0018）。

计划正文与实施记录见 [`delivery-history.md`](delivery-history.md) §18–§20、§29.2。

## 17. 界面中/日/英三语资源化与语言切换（已交付）

界面三语化已完成。`users.ui_language` 偏好（0022）、`/users/me/ui-language`、`web/src/lib/i18n/` 三语目录与 sidebar 切换器已交付，全部画面经 catalog 取文案。

残余：母语者人工校对与浏览器级三语/键盘验收（随部署验收执行）。计划正文与实施记录见 [`delivery-history.md`](delivery-history.md) §26–§28、§29.3。

## 18. 托管凭据自助与应用层信封加密（已交付）

托管凭据能力已完成。新增 `MANAGED` resolver：明文经创建端点一次即由部署级 KEK 做 AES-256-GCM 信封加密并只存密文，`ENVIRONMENT`/`FILE` 零改动。决策与威胁模型见 `docs/09` §7.2。

服务器 migration 已通过；残余为三语目视验收与应用级浏览器验证。计划正文与实施记录见 [`delivery-history.md`](delivery-history.md) §29.4。

## 19. 资源快照物化与 Agent 工作区能力扩充

### 19.1 目标

让 Agent 能在绑定 scope 内**自主发现并读取**资源文件（例如从故障票的线索反查源码，或跨用户上传的设计书检索关键词），而不必由平台为每种访问动作（检索 / 日志 / 差分）各定义一个 Tool capability。做法是把边界守在**物化那一刻**：Run 准备流程由平台将冻结的 ResourceBinding 物化成只读文件树放入 `input/<requirement_key>/`——`repository` 是按 scope 裁剪、按 revision 冻结的目录树，`document` 是按 `content_hash` 取正文的单文件——之后 Agent 使用既有的 `workspace.search/v1` 与 `workspace.read/v1` 自行工作。

这不是放宽既有边界，而是兑现 `docs/06` §6.2 中已经写明、但从未落地的一句话——「Read/Glob/Grep 只能作用于平台显式挂载的只读资源快照或 Run workspace」。`input/` 目录当前已被创建且读取映射已就位，唯独没有任何代码向其写入内容。

同时补齐 Agent 侧长期缺失的**工作区写入**能力，并修复一处就绪度谎报。

### 19.2 已定决策

- **按「资源边界」切，而非按「动作」切**。不新增 `repository.search/v1`、`repository.log/v1` 等逐动作 capability；文件发现由 `workspace.search/v1` 在已物化的树上完成。逐动作切分会让每种访问模式都需要一个完整工作包，是当前 Skill 无法运行的直接原因。
- **不向 Agent 开放对外部系统的直接调用**。物化保住四条边界：凭据只在物化流程的 Provider 边界内解析、scope 裁剪在物化侧强制、读取经既有 Provider 产出 Evidence 与 `content_hash`、内容按冻结 revision 可复现。若允许 Agent 直接执行外部命令，这四条同时失效。
- **物化范围含 `repository` 与 `document`，不含 `issue`**。二者都落入 `input/<requirement_key>/`，让 Agent 用同一组 `workspace.search/read` 自主发现：`repository` 是按 scope 裁剪、按 revision 冻结的文件树；`document` 是用户执行前经文档 API 上传到 Project 的 blob，按 `content_hash` 取 UTF-8 正文落为单文件。纳入 `document` 的收益是让上传文档也获得「跨文件检索」而非仅「按已知路径点读」，与 `repository` 同理；代价极小，因为读取侧 `ProjectDocumentSource` 已存在，无需新客户端。`issue` 是记录而非文件树，继续由 `issue.read/v1` live tool 逐条读，不物化。既有的 `document.read/v1` 与真实 `repository.read/v1` live tool 与物化并存，供已知精确路径的单点精读。
- **查找表按「物化文档 + `workspace.search`」消费，不引入表格能力**。`JAF規模一覧.csv`（按键查担当者）、字段映射（ID→label）这类查找表，物化为文档后由 Agent 用 `workspace.search` 定位行、`workspace.read` 取值。单条键查找该路径足够；大规模精确键查找的可靠性边际，留待真实需求出现时再评估是否做 §5.2 愿望项 `tabular` 能力，当前不做。
- **超出体积上限一律 fail closed，不截断**。截断会让 Agent 面对一棵「不完整却看起来完整」的树，据此得出「文件不存在」这类错误但自信的结论。上限与 `workspace.search/v1` 的扫描预算对齐（10 MB / 500 文件），避免「物化了却搜不全」的静默盲区。
- **物化清单必须记录 `skipped`**（二进制、超限文件）。缺少它，Agent 会把「平台读不了」误判为「不存在」。
- **仓库 scope 不支持 wildcard**（`normalize_provider_scope` 已拒绝 `*`），因此仓库物化天然有界，不存在「不限」导致整仓落盘。
- **同时注册真实 `repository.read/v1` Provider**。物化器本就需要真实 git/svn 客户端，顺带暴露为 Provider 的增量成本极小，且使既有契约首次真正可用。该 capability 的 blueprint / readiness / permission 语义完全不变，仅运行时多一条实现路径。
- **`workspace.write/v1` 的写入面限 `workspace/` 与 `output/`，明确排除 `input/`**。物化快照是冻结证据，可写即破坏「内容对应 binding revision、可复现」这一不变式。
- **`document` 的「写」拆成两义，只有产出交付物是自由的**。Agent 产出新文档（报告 / 改写副本）是向 `output/` 写，属 workspace 写入能力，用户从 `output/` 下载；平台不提供名为 `document.write` 的自由资源能力。反之，原位覆盖已物化的 `input/` 文档或 Project 文档库均不做（破坏冻结证据不变式；需要修订版就产出新的 `output/` 交付物，由用户决定是否替换），写回外部系统按 §19.4 经 ChangeProposal 与审批。
- **凭据本文按 `username:secret` 解释，不给 Integration config 加用户名字段**。无冒号时按 token 处理并补 `x-access-token`（token 认证忽略用户名），password 侧可含冒号（只按首个冒号分割）。凭据不进 argv：git 经环境变量注入 Authorization header，svn 经 `--password-from-stdin`；子进程只拿最小环境，不继承 Worker 的 DB URL 与 KEK。**§20 复用同一客户端时沿用这套规则**。
- **repository URI 的 scheme 在登记时就限制**：git 收 `http`/`https`/`file`，svn 另加 `svn`；ssh 系因平台不持有密钥而拒绝，URI 内嵌 `user:password` 一并拒绝。这是“就绪度必须反映实际可执行”在 scheme 粒度上的延续。
- **冻结 revision 由单点规则定出**：binding 的 `revisions` 恰好 1 条即用之；为空视为「不限 revision」（UI 上是可选项，硬边界是 `paths`），取 Integration 的 `default_revision`；多条时仅当含默认值才取默认，否则 fail closed（不擅自挑第一条）。物化与 live 读取共用该规则，live 额外接受「该式解析出的具体 SHA/号」。
- **索引、历史与二进制文本化都在物化时完成，不新增 capability**。生成物落 `.projectmind/`，计入体积/件数预算并参与再访 hash 校验，但不进入标识资源内容的 `content_digest`。转换失败与超限仍记入 `skipped`，维持「读不了 ≠ 不存在」。
- **不开放原始网络访问**。当前用例（Redmine 读取）已由 `issue.read/v1` 覆盖且更优——凭据留在 Provider 边界、scope 由 binding 冻结、响应自动成为 Evidence。原始网络反而引入凭据外泄面、绕过 scope、且响应不可作为 Evidence。若将来出现真实需求，只接受 `docs/06` §6.2 已描述的形态：注册的 fetch Provider + 按 Project 的 origin allowlist + 不附带凭据 + 响应记为 Evidence，并须先更新本计划。

### 19.3 实施工作包

本工作包已在本地完成，**实施记录（含决定与验证）见** [`delivery-history.md`](delivery-history.md) **§31–§33**；本节只保留边界与状态。

| 工作包 | 边界 | 状态 |
|---|---|---|
| 就绪度与 workspace 写入 | Provider 级就绪度判定，以及只写 `workspace/` 与 `output/` 的 `workspace.write/v1` | 本地完成 |
| 文档资源物化 | 物化框架与全 Project 文档进入 `input/documents/` | 本地完成 |
| repository 资源物化 | 真实 git/svn 客户端、绑定再校验、scope/revision 强制和 `repository.read/v1` | 本地完成 |
| 索引、历史与二进制文本化 | 生成 `files.txt`、`history.txt` 和 xlsx 文本，不新增 capability | 本地完成 |
| Agent 工作区说明 | 将资源根、索引、清单、历史和冻结 revision 写进 AgentTaskBrief 与执行 prompt | 本地完成 |

### 19.4 非目标

- 原始网络访问与 SDK 内置 Web（见 19.2 决策）。
- Bash / sandbox 命令执行：须具备独立 capability、mount/sandbox、资源限制与网络策略并配齐测试后，才可作为独立工作包评估。
- 对 Integration 目标的直接写入：外部写入始终经 ChangeProposal 与审批，当前范围不变。
- `workspace.read/v1`、`workspace.search/v1` 的契约变更：现有契约已足够，不做扩展。

### 19.5 验收用例：JAF 单票分析（分层验收）

以 `jaf-quality-ticket`（Redmine 指摘票的个别输入&分析）作为 §19 的真实验收用例，验证「脚本编排 Skill → 能力编排」这条路（过程对齐规则由 `docs/11` §5.4 承载）。该 Skill 只产出报告、不回写，天然可分两部分：

- **基础资源验收（不含 SVN）**：数据源走 `issue.read/v1`（redmine/csv）；配置表（字段映射、`JAF規模一覧`、知识库）上传为 Project 文档并物化进 `input/`；10 项整合性 + 大部分 12 项妥当性校验由 Agent 推理完成；报告作为 Outcome 产出。**不依赖 SVN 与二进制文本化**，是资源物化的最小可跑边界。
- **仓库资源验收（含 SVN）**：加 `repository.read/v1`（svn provider）读源码、`workspace.search` 在物化树上发现文件、xlsx 设计书文本化做 Step 5 设计书验证。基础资源验收通过后逐能力接入。

基础资源验收通过即证明 `docs/11` §5.4 的三档对齐在真实 Skill 上成立：`curl→issue.read`、`parse_issue.py→issue.read`（直译），SVN 步骤降级为 guidance 且不阻塞主流程。

### 19.6 把物化产物告知 Agent（本地完成）

**问题**：前几个物化工作包把资源写进了 `input/`，但 AgentTaskBrief 与执行 prompt **从未提到它们存在**。Brief 的
`resources[]` 只有 `key`/`kind`/`capabilities`/`binding`，没有落点路径；prompt 里也没有任何一句说明
「你的绑定资源已经在 `input/<key>/` 下」。于是 §19 的整条前提——「Agent 用既有 `workspace.search/read`
自走发现」——依赖模型**自己想到**去搜 `input/`。这是 §19.5 两个验收部分最可能的失败方式，且失败时表现为
「Agent 说资源不存在」，与真正的缺资源无法区分。

**已定：写进 Brief**（prompt 是它的渲染）。Brief 是「Agent 被告知了什么」的受审记录，只改 prompt
会让审计记录与实际告知不一致。契约同步面比预估小——`task_brief_checksum` 未被任何测试钉死字面值，Web 只
消费 checksum 字符串不消费 Brief 正文，因此实际只需同步 schema、example、projector 与测试。

**落法**：物化器 `materialize()` 从返回 `None` 改为返回 `MaterializedResource` 记述子元组（初次物化与
reuse 经路都由同一份 manifest 派生，重试与首次的案内不会漂移）；Brief 的资源项按种别/key 对应上落点
（document 按种别——全 Project 文档共用一个根；repository 按 key）；prompt 增一节列出根、索引、
清单、历史与冻结 revision，并写明「先看索引或清单再检索」与「`skipped` 里的是读不了、不是不存在」。
**未物化的要求不写落点**——落点只来自物化器实际写下的结果，不由蓝图推测，否则未配线环境会案内一个不存在
的目录。

**验收**：部署上跑 §19.5 的基础资源验收，观察 Agent 是否在无人提示下读取物化文档并产出报告。

## 20. 代码仓库回写（受控变更提案与 commit）

### 20.1 目标

把 `observe → propose → apply` 从 Redmine 字段更新（§2.5 已实现的 `issue.update/v1`）延伸到**代码仓库**：Agent 在只读分析后产出**代码变更提案**（diff/patch），由平台在审批后经注册的 repository-effect Provider 落地为一次可追溯、可回滚的仓库变更。这补上了 §2.5 曾整体禁止、但自动化最终必需的那块：git 的受控写入现已开放（svn 仍禁止）。

Agent 侧不新增直接写能力：仍只调用既有 `change.propose/v1` 产出提案，apply 由 EffectExecution Worker 执行（与 `issue.update/v1` 同一受控路径）。D1–D4 已于 2026-07-25 批准并落地，见 §20.2。

### 20.2 决策

**立项时已定**

- **Agent 只提案，绝不直接 commit/push**。变更经 `change.propose/v1` → 审批 → EffectExecution Worker，凭据只在 Provider 边界内解析，与 `issue.update/v1` 同构。
- **落地形态是「可评审的变更」，不是对默认分支的直接写**。产出 = 新分支 + commit；PR/MR 的自动开设属于后续仓库协作能力。绝不直接 push 到受保护/默认分支。
- **不可 LOW 预授权**。代码变更后果重，`repository.write/*` 不进入 §2.5 的预授权白名单——始终需人工批准（或以 PR 评审为等价闸门），不存在「静默自动 commit」。这与 issue.update 的低风险字段更新是刻意的差别。
- **base-revision CAS**。提案冻结 base revision；apply 时若分支已移动则拒绝、要求重新提案（与 issue.update 的原子前置版本校验同理）。
- **产出即 Evidence 且可回滚**。落地的 base/commit SHA 与分支名记为 before/after Evidence；回滚经删除分支或 revert。
- **复用 §19 的仓库客户端**。资源物化工作包交付的真实 git 客户端同时服务读物化与写落地，避免两套 subprocess 与凭据实现。

**已定（2026-07-25 批准。实施记录见 [`delivery-history.md`](delivery-history.md) §34）**

- **D1｜单一 `repository.write/v1`**：携带变更集 + `base_revision` + `target_branch`。拆成 commit 与
  pull-request 两个能力会让「提交了但没开 PR」成为可达的中间态——既无人评审，也不在提案的审批范围内。
  合成一个能力，「一次审批 = 一次可评审的变更」才是原子的。
  - **载体在 repository.write 契约工作包落地时定为逐文件全文 `SET` / `REMOVE`**，不是 unified diff。Agent 仍只调用既有
    `change.propose/v1`，其 `changes[]` 形如 `/files/<repo path>`。理由：read-back 可按 path 逐个比对
    内容 hash，replay 判定也确定（内容已一致即 replay）；diff 无法重放，读回也没有可比对象。差分本身
    由 base commit 与新 commit 之间的 git 历史承载。
- **D2｜先 git，svn 作为后续扩展**：git 的分支 + PR 模型自带人工闸门；svn 没有等价物，需另定评审约定。
  因此 `svn` 不能声明 `repository.write/v1`（宣言时即拒绝）。
- **D3｜临时可写工作副本**：EffectExecution 内按 `base_revision` clone/checkout，用完即弃，复用 §19 的
  git client（新增 `open_writable`）。**不**复用 Agent 的 `input/` 物化树：那是 0o400 冻结证据，可写
  即破坏「内容对应 binding revision」这一不变式；且物化按 scope 裁剪，变更可能触及 scope 内但未物化
  （被 skip 或超预算）的文件。
- **D4｜UTF-8 文本、单文件 1 MiB、单次 50 文件 / 4 MiB**：超限一律 fail closed 并要求拆分提案。二进制
  与大文件不做——patch 校验、read-back 与人工评审都建立在文本可读之上。

**实现过程中新增的两条边界**

- **落地形态是 platform 预约 namespace `projectmind/` 下的新分支**。允许前缀由平台固定而非逐 Integration
  配置：「绝不 push 到默认/受保护分支」因此由结构保证，不依赖配置正确。同名分支若内容不同则判
  `target_branch_conflict` 失败，**绝不覆盖**（不使用任何 force 选项）。按 Integration 收窄前缀留待真实需求。
- **PR/MR 的开设已落地**，但**要求配置而非推测**：Integration config 的 `forge_kind` /
  `forge_api_base_url` / `forge_project` 三者要么齐备要么全无，部分设置直接拒绝；从 host 名猜测 forge
  类型对自建实例不可靠，猜错的后果是「往别的地方写」。未配置 forge 的 Integration 只产出「分支 +
  commit + read-back Evidence」，评审由人在 forge 上发起——闸门不受影响，因为该 capability 不可预授权，
  每次 apply 之前都已有人工批准。PR 开设失败不回滚 commit（commit 已通过 read-back 验证）。

**D5｜svn 的分支目标约定（2026-07-25 已定：由平台在代码中固定）**

分支目标 = `<仓库根>/branches/projectmind/<名称>`。**仓库根不是猜的**——`svn info` 的应答里带
`<repository><root>`，因此 `repository_uri` 指向 trunk 还是仓库根都能正确定位；`branches/` 不存在时由
`svn copy --parents` 建立。这样既满足「代码里约定一个就够」，又不需要对客户仓库布局做假设。
实现与 git 对称：承认为闸门（该 capability 不可预授权）、提交后 `svn cat` 逐文件读回校验、同名路径
内容不同则判冲突不覆盖、base revision 不符判 STALE（svn 侧由「按 base revision checkout 后 commit
被 out-of-date 拒绝」天然承担）。

**D6｜承认后直接进主分支（2026-07-25 已定，默认值即 `direct`）**

config 的 `write_mode` 取 **`direct`（默认）** 或 `branch`。`direct` 时，承认済み变更直接 commit 到
Integration 的既定分支（git 为 `default_revision` 指向的分支并要求 fast-forward；svn 为绑定的
`repository_uri` 路径），**不建预约分支、不开 PR**——判断依据是「ChangeProposal 的人工批准即是闸门」，
该 capability 不可预授权，每次落地前都有人批过。想额外挟一层分支/PR 评审的仓库显式设 `branch`。

因此 CAS 是唯一的并发防线，也更关键：git 要求远端分支头仍等于提案冻结的 base，svn 由 out-of-date
拒绝承担；不符则判 STALE 要求重新提案，**绝不使用 force**。

两条随默认值变化而必须成立的约束：

- **git 的 `default_revision` 不能停留在 `HEAD`**。`HEAD` 不是分支名，而 direct 要写「既定分支」，
  所以声明了 `repository.write/v1` 的 git Integration 若未给出具体分支名，**登记时即拒绝**
  （只声明读取的 Integration 不受影响）。svn 的 `HEAD` 是正当 revision 式，不在此列。
- **保护分支的仓库应显式设 `branch`**。否则 direct 下服务端会拒绝 push——这是预期的 fail closed，
  但把它留给运行时不如在配置时想清楚。

**影响面**：既有 repository Integration 的 config 里没有 `write_mode`，因此**继承新默认值 `direct`**。
该能力与本次默认值同日交付，尚无历史配置受影响；将来若要为存量整合保留旧行为，需要一次把
`write_mode: branch` 写入既有 config 的迁移。

### 20.3 实施工作包

| 工作包 | 边界 | 状态 |
|---|---|---|
| repository.write 契约与提案校验 | 契约、catalog 登记、预约 namespace、scope、文本变更格式、规模上限和 apply chain 登记 | 本地完成 |
| git Effect Provider | 可写工作副本、分支 commit/push、remote read-back、replay 与冲突判定 | 本地完成 |
| Effect service 与 Worker | Proposal → Approval → EffectExecution，预授权和 Secret 要求由能力表统一约束 | 本地完成 |
| PR/MR 与 SVN 支持 | GitHub/GitLab PR/MR 配置、SVN 分支目标、读回校验和冲突保护 | 本地完成 |
| `write_mode` 与 Web 审批展示 | `direct`/`branch` 选择、资源配置和逐文件变更渲染 | 本地完成 |
| PR 自动合并与多仓库原子变更 | 明确不做 | 长期非目标（§20.4） |

### 20.4 非目标

- **未经批准的写入、自动合并（merge PR）**：始终经人工闸门。`write_mode=direct`（默认）下只能写既定分支且要求 fast-forward，不使用任何 force；`write_mode=branch` 下分支名限制在 `projectmind/` 预约 namespace、既有分支绝不覆盖。**受保护分支的仓库应显式设 `branch`**——否则 direct 会被服务端拒绝，这是预期的 fail closed。
- **代码变更的 LOW 预授权**：不开放（见 §20.2）。
- **二进制/大文件变更、跨仓库原子变更**：当前不做。
- **Agent 直接执行 git 命令**：写落地只在 Provider 边界内，Agent 无 Shell/网络出口（§2.7）。

## 21. 解释器过程重表达（docs/11 §5.4）

### 21.1 目标

让脚本编排型 Skill（步骤写死 `svn cat` / `curl` / `python3 x.py`，`allowed-tools` 声明 `Bash` / `Write` 等内建工具）能被**导入并解释**，其过程步骤被重表达为平台能力调用，而非在静态门被整体硬挡、或原样带过。把 docs/11 §5.4「解释器是翻译器而非校验器」实装到三处代码落点。

当前 `jaf-quality-ticket` 因 `allowed-tools` 含 `Write` 在解释器执行前即抛 `UnsafeSkillSourceError`（`interpreter.py:456`），蓝图根本不生成——本增量是它首次进到解释器的前提，也是 §19.5 验收用例的前置。

### 21.2 已定决策

- **声明 ≠ 授予**。`SKILL.md:8` 已定义 source 声明为 untrusted evidence，但静态门（`interpreter.py:314-328`）却因**声明** `bash/edit/web/write` 就判 error → `blocked` → 硬停，自相矛盾。本增量把「声明内建工具」从 blocking error 降为**可重表达信号**（non-blocking），解释继续。
- **硬挡只留给真正不安全**。`blocked`（`interpreter.py:351,456`）仅对真实不安全静态发现触发：明文 credential 物料、敏感文件外泄、路径逃逸——**不含**工具声明本身。
- **过程重表达、业务原样**。Layer 2（system Skill）区分：**过程/工具机制**（`svn cat` → 经 `repository.read` 读）可重表达为能力调用；**业务规则/约束**（enum、整合性规则、验收条件）仍 `without invention` 保真，`source_trace` 不变。
- **三档落法**（承 docs/11 §5.4）：① 直译（svn cat→repository.read、curl→issue.read）② 换 idiom（svn list\|grep→物化+workspace.search，依赖 §19，未到位则降级）③ 降级 guidance（svn log、xlsx 无等价能力）。硬失败仅当 required 核心步骤既不可映射又不可降级。
- **安全不变式不动**。松开的只是「因声明而拒绝解释」；Agent 永不被授予 bash/edit/web/write（permission snapshot + tool registry 强制，不变），`external_write_policy=deny`、`write_capabilities=[]` 不变。
- **确定性守卫**。重表达目标必须是 catalog 已注册能力（请求内已嵌，`interpreter.py:466`）且须在蓝图 `resource_requirements` 中声明；过不了则降级，不硬塞。
- **修声明解析漏洞**：硬挡当前是精确匹配，`Bash(python3 *)`（带参）逃过、裸 `Bash` 命中——归一化解析（剥 `(...)`）让带参与裸形一致处理（现统一为「可重表达」而非「挡」）。

### 21.3 实施工作包

解释器过程重表达已在本地完成，**实施记录见** [`delivery-history.md`](delivery-history.md) **§30**；本节只保留工作包边界与状态。

| 工作包 | 边界 | 状态 |
|---|---|---|
| 静态门与声明解析 | 声明内建工具从 blocking error 降为 `declared_builtin_tool` 信号，硬挡只留给真实不安全发现；归一化 `allowed-tools` 解析 | 已部署（待实机越门） |
| 过程重表达规则 | system Skill 的三档映射、过程/业务分界、守卫和 `source_traces` 义务 | 已部署（待模型行为观测） |
| 步骤能力守卫 | publish gate 检查名指能力已注册且被资源要求开示，否则 warning 降档不拒发 | 本地完成（离线已验） |
| 已实现能力对齐 | 将文件索引、提交历史、文本化副本、workspace.write 和仓库变更提案纳入三档映射 | 本地完成 |

### 21.4 非目标

- 给 Agent 授予 bash/edit/web/write，或自动执行脚本（永不）。
- 物化（§19）：idiom 档（svn list→search）依赖它，未到位则降级。
- 明文 credential 硬挡的松动（那是真实不安全，保留）。

### 21.5 验收

- **静态门验收** = `jaf-quality-ticket` 首次越过静态门、进入解释并生成蓝图（不再 UnsafeSkillSourceError）。这是 §19.5 基础资源验收的前置。
- **模型行为验收** = 蓝图 `recommended_steps` 里 `svn cat`→`repository.read`、`curl`→`issue.read` 被重表达，且业务规则（整合性/妥当性 enum）未被发明。
- **发布守卫验收** = 名指未注册/未开示能力的步骤只产生 warning，Skill 仍可发布（离线已验，见 `test_manifest_gate.py`）。
- 上述工作包均已实现并通过 ruff/mypy/pytest；服务器部署基线已通过，剩余为实机越门与模型行为验收。
## 22. 任务调度（TaskSchedule）

### 22.1 目标

把 Task 从「用户点一次跑一次」扩到**按时间自动发起 Run**：预约一次、或按 cron 周期反复执行，
时区、下次触发时刻、重叠处理与失效停机都由平台承担。这是初始产品范围中暂缓的调度能力，现按
`docs/07` §8.1/§8.3 已有的设计落地。

调度不是新的执行路径：它最终仍调用与 `POST /projects/{id}/task-runs` 完全相同的
`RunService.create_task_run`。调度只回答「**什么时候**、**以谁的身份**、**用哪份冻结配置**」发起，
Run 之后的一切（Segment/Attempt、交互、审批、效果）不变。

### 22.2 已定决策

- **D1｜只做「一次性预约」与「cron 周期」，监控条件不做。** `docs/07` §8.1 还列了「监控条件」，
  但条件求值需要在 Run 之外再开一条读外部资源的路径，其权限、Evidence 与审计要另立一套。
  周期地跑一个 Run、由 Skill 在 Run 内自行判定，用已有能力就能表达。停止条件因此收敛为
  `end_at`（到期自动终结）与 `max_runs`（累计触发上限），不引入外部条件求值器。
- **D2｜cron 用标准库自实现 5 段解析，不引入 `croniter`。** 解析与「求下一个触发时刻」是确定性纯函数，
  可完整测试；为此引入依赖反而扩大供应链面（与 `agent/binary_text.py` 同一取舍）。支持
  `*`、`n`、`a-b`、`*/n`、`a-b/n` 与逗号列表，星期用 `0-6`（0=周日）。`L`/`W`/`#`/`@reboot`
  等扩展语法**保存时即拒绝**，不静默解释成别的意思。
- **D3｜时区必填 IANA 名，DST 的两个歧义显式定死。** 触发时刻在该时区求值后转 UTC 存储
  （`zoneinfo` 是标准库）。春季**不存在的本地时刻**跳过该次、取下一个匹配时刻（顺延到邻近小时会让
  「每天 02:30」在那天变成不确定的时间）；秋季**重复的本地时刻**只触发一次，取 `fold=0` 的那次
  （否则每年秋天多跑一次）。
- **D4｜冻结精确 SkillVersion 与资源选择，失效不静默改用别的来源。** 承 `docs/07` §8.3：schedule 保存
  `skill_version_id` + `task_key` + `input` + `sources`。触发时若该版本已不是 Project 的 active
  `ProjectSkillVersion`、或输入/资源选择已不再合法，schedule 转 `ERROR` 并停止触发、记录原因，
  **不**回退到其它版本或其它来源。
- **D5｜重叠默认跳过。** 承 `docs/07` §8.3：同一 schedule 的上一个 Run 仍处非终态时，本次触发跳过并
  记录原因，`next_run_at` 照常前进。不排队，同一 Task 不并行执行。
- **D6｜错过的触发不追赶。** Worker 停机后重启，若已错过 N 次触发，只按「最近一次到期的触发」执行一次
  （仍受 D5 检查），`next_run_at` 直接推进到当前时刻之后的第一个匹配时刻，错过次数记入 schedule。
  追赶 N 次会在恢复瞬间对外部系统产生突发压力。
- **D7｜触发的 idempotency key 由 `(schedule_id, 触发时刻)` 决定性导出。** `create_task_run` 已按
  `(project, task, idempotency_key)` 幂等，因此即使两个 Worker 同时 tick、或 tick 与恢复重叠，同一次
  触发也只会产生一个 Run。认领本身另加一层 `next_run_at` 的 CAS，两道防线互不依赖。
- **D8｜以创建者身份运行，触发时重新校验其权限。** schedule 记录 `created_by`；触发时重新确认该 user
  仍 `ACTIVE` 且仍对该 Project 有访问权，不满足则转 `ERROR` 停止。否则离职者留下的 schedule 会带着
  他的权限一直跑下去。
- **D9｜状态机与不删除。** `ACTIVE ⇄ PAUSED`；`ACTIVE/PAUSED → COMPLETED`（一次性已触发、或达
  `end_at`/`max_runs`）、`→ ERROR`（D4/D8 的失效，修好配置后可手动恢复为 `ACTIVE`）、
  `→ ARCHIVED`（终态）。不物理删除（`docs/07` §8.2）。

### 22.3 实施工作包

| 工作包 | 边界 | 状态 |
|---|---|---|
| 调度领域与触发求值 | 状态机、跳过原因、标准库 cron、时区求值、`task_schedules` 表和 migration `0025` | 本地完成 |
| 调度服务与 Worker | 增改、暂停/恢复/归档、触发预览、到期认领和建 Run | 本地完成 |
| 公开 API | Project 作用域 CRUD、触发预览、状态操作和契约 schema/example | 本地完成 |
| Web 调度面 | 列表、创建/编辑、下三次触发预览、暂停/恢复/归档和三语文案 | 本地完成 |

### 22.4 非目标

- **监控条件触发**：见 D1。
- **同一 Task 的并行执行与排队**：`docs/07` §8.3 明确 MVP 不并行；重叠一律跳过。
- **追赶错过的触发**：见 D6。
- **跨 Project 的全局调度视图**：调度与 Run 一样按 Project 授权，不提供越 Project 的聚合面。
- **调度触发的 Run 绕过任何既有闸门**：审批、预授权、资源绑定再校验完全不变——调度改变的只有
  「谁在什么时候按下开始」。
## 23. 并行 multi-agent（扇出子 Agent）

### 23.1 目标

让**一个 Run 内**可以并发地跑多路互相独立的只读分析，主 Agent 汇总它们的结论。这是
`docs/06` §3 与 `docs/11` §8.2 一直挂着的那项：「多 Session 是顺序延续能力，不等于并行
multi-agent；并行子 Agent 需要单独的资源、冲突和审计设计」——本节把那份设计定下来。

**状态：已部署，待模型使用效果验收。** migration `0027` 已随当前数据库链应用；决策见 §23.2，实施工作包与状态见 §23.3。

### 23.2 已定决策

- **D1｜子 Agent 一律只读，且能力是主 Agent 的真子集。** 永不授予 `change.propose/v1`、
  `interaction.request/v1`、`workspace.write/v1` 或任何注册 write capability。这一条把最难的两类
  冲突（外部效果的归属、工作区写入的相互覆盖）**从存在性上消掉**：所有效果仍然只从主 Session
  发起，人工批准面对的仍是同一个提案链，既有闸门一字不改。
- **D2｜扇出由主 Agent 在运行时发起，不进蓝图契约。** 新增平台控制能力
  `subagent.dispatch/v1`，与 `interaction.request/v1` / `change.propose/v1` 同为 control tool。
  **刻意不动 CapabilityBlueprint 的 `execution_preferences`**：加一个「可并行项」字段会连带
  冻结 checksum、system Skill 版本与解释器 fixture 三处同步，而并行与否是运行时判断，不是
  Skill 的语义。
- **D3｜Worker 仍是唯一的 event 写入者，SSE 契约不变。** 子 Session 的活动不各自写 RunEvent，
  而是收敛成主 Session 上的**一次 ToolCall + Evidence**；每个子 Session 另存 `AgentSession` 行
  供审计。这样 AGENTS 的「Run 内 sequence 严格单调增」不需要改成分层序号，
  `contracts/events/run-event/v1.schema.json` 也不动。
- **D4｜预算是切分，不是相乘。** `max_turns` / `max_output_bytes` 仍是 Run 级上限。dispatch 时
  从 Run 的剩余预算里划出一份分给各子 Agent，主 Agent 的剩余额度同步扣减。否则「并行 4 路」
  就等于把成本上限悄悄乘以 4。
- **D5｜有界扇出、禁止嵌套。** 单次 dispatch 的子任务数与并发数都有硬上限（建议 4），子 Agent
  的能力集里没有 `subagent.dispatch/v1`，因此不可能递归展开。
- **D6｜失败隔离，取消与 wall timeout 作用于整组。** 单个子 Agent 失败只让该路返回错误摘要，
  不拖垮 dispatch；Run 取消与 `wall_timeout_seconds` 一次性终止全组。
- **D7｜实现落点已勘定**（下列均已核对当前代码）：
  - `ClaudeAgentSdkEngine` 的 `self._active` 已是 `dict[AgentSessionRef, _ActiveExecution]` +
    锁，**结构上已支持并发 session**，不需要改引擎的并发模型。
  - `RunContext` 是 frozen dataclass，子上下文用 `dataclasses.replace()` 派生（换 prompt、
    收窄 `tools` 与 `permission_snapshot`、缩小 `limits`）即可，不需要新的 builder。
  - Provider 需要 engine 与 session 持久化句柄，因此 `RunToolContext` 要**增补**主
    `RunContext`（frozen dataclass 加字段是相加式改动，不影响既有 Provider）。
  - 子 Session 的 `continuation_mode` 现有取值（INITIAL/RESUME/FORK/REPLACE）都不贴切，需加一个
    新值——这是本工作包**唯一**的契约同步点（`runs/domain.py` → API response → `web` 的
    `messages.enums.continuationMode` 三语）。

### 23.3 实施工作包

| 工作包 | 边界 | 状态 |
|---|---|---|
| dispatch 契约与能力投影 | `subagent.dispatch/v1` 契约、catalog 登记、checksum 同步和 fail-closed 能力投影 | 本地完成 |
| 子 Agent Provider | 受限 `RunContext`、并发执行、预算切分、失败隔离、超时打断和结果汇总 | 本地完成 |
| 结果呈现 | 未完成的分析面与已完成的分析面视觉区分，并明确结论覆盖范围 | 本地完成 |
| 子 Session 持久化 | migration `0027`、`BRANCH` continuation mode、三语文案和 Run 详情时间线 | 本地完成 |

### 23.4 非目标

- **子 Agent 写外部系统或直接提案**：见 D1，永不开放。
- **嵌套扇出**：见 D5。
- **跨 Run 的并行编排**：Run 仍是审计与目标的单位。
- **同一 Task 的多 Run 并行**：那是 §22 D5 的重叠策略，与本节无关。

### 23.5 验收

- **可离线确定性验证的部分**（本工作包的安全性质，必须有测试）：子能力集永不含
  write/effect/interaction 能力；并发与数量上限生效；预算切分后总额不超过 Run 上限；单路失败
  不影响其余路；取消终止全组。
- **需模型 egress 的部分**：模型是否在合适的场合扇出、汇总质量如何——与 §21 的模型行为验收同属
  `docs/12` §35.2 的 A 类残余。

## 24. generated FrontendModule（方向已定，部分实现）

### 24.1 目标

让 Interpreter 为 Skill 生成**受控的前端模块代码**，经构建沙箱产出 bundle，在隔离 iframe 中渲染该
Skill 特有的结果视图；失败时自动回退标准 ViewSpec。承 `docs/07` §10–13 的目标设计，本节把它从
「后续增强草案」升级为可实施的决策与工作包。

### 24.2 已定决策

**D1｜产物形态：生成 React/TS 源码，经构建沙箱产出 bundle，在 iframe 中运行。**
即 `docs/07` §10 的完整链路（生成源码 → 静态检查 → 依赖锁定 → 构建沙箱 → 单测与 Host API 契约测试
→ iframe 预览 → 调整重生成 → 随 SkillVersion 发布）。已评估过更便宜的「声明式 ViewSpec 扩展」
（模型只产数据、平台用已审组件渲染，可一次消掉独立 Origin/构建沙箱/供应链三个面），**选择不采用**：
表达力受限于平台组件库，每加一种呈现都要先改平台。

**D2｜Origin 形态：同主机 + 专用路径 + `CSP: sandbox` 头，不新增域名。**

- 落点 `{contextPath}/modules/<bundle-hash>/`，由现有 `web` 容器提供。
- 每个响应**必须**带 `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; connect-src 'none'`。
- 依据：iframe 不启用 `allow-same-origin` 时本就是 opaque origin（`docs/07` §12.1 自己也据此
  拒绝把 `event.origin` 当身份）。独立 Origin 真正多买到的是「有人直接导航到 bundle URL 时不在应用
  Origin 上带会话 Cookie 运行」——而响应头 `CSP: sandbox` 对**任何**加载方式都强制 opaque origin，
  等效地消掉这一条，且不需要新域名、新证书、新 Traefik 路由（`AGENTS.md` 的 Ingress 规约因此不动）。
- **这条响应头是安全边界，不是样式**：配错即整层隔离失效。必须固定在 `web` 容器的 nginx 配置里，
  并由测试断言其存在——不能靠记性。
- **已知并接受的代价**：与独立子域名相比少一层纵深（误加 `allow-same-origin` 时，独立域名仍能兜住，
  同主机则不能）。因此 iframe 属性也要有测试钉住「不含 `allow-same-origin`」。

**D3｜威胁模型是「首次执行生成代码」的准入条件，不是本轮决策的前置。**
本轮只定方向与形态。在首次真正执行生成代码（iframe 预览）**之前**必须完成
`docs/07` §11.2 静态拒绝项的依据论证与残余风险记录，**责任人待定**。这一条刻意留在计划里而不是
删掉：D2 把纵深换成了「响应头必须正确」，正是需要威胁模型逐条确认的那类取舍。

**D4｜构建沙箱是独立 Compose service，不接 edge network。**
`module-builder` 固定 Node/pnpm 版本、冻结 lockfile、禁 package lifecycle scripts，构建后断网跑测试。
**内部 npm 镜像地址由部署环境提供；未提供时构建拒绝，不回退公网 registry**——回退正是要消掉的供应链面。

**D5｜依赖白名单，越界即拒绝。**
仅 React、`@projectmind/module-sdk`、`@projectmind/ui` 与批准的纯前端库。禁 lifecycle scripts、原生
扩展、Git dependency、动态依赖地址。静态拒绝项按 `docs/07` §11.2。

**D6｜失败即降级，且不影响数据。**
bundle 加载超时、协议不兼容、运行异常或 Host API 违规 → 立即禁用当前 iframe 回退标准 ViewSpec。
回退不触碰 Task、Run、Result、Evidence。

### 24.3 实施工作包

| 工作包 | 边界 | 准入条件 | 状态 |
|---|---|---|---|
| 版本与 SkillVersion 绑定 | `FrontendModuleVersion` 表、migration `0026`、精确版本绑定、版本状态机和降级/回滚规则（**不执行生成代码**） | — | 本地完成 |
| 静态拒绝与依赖白名单 | 六类静态拒绝分析、依赖白名单和 lifecycle script 强制（**不构建、不投放**） | — | 本地完成 |
| 构建前置检查 | 镜像未配置、指向公开 registry 或 lockfile `resolved` 指向公网时 fail closed | — | 本地完成 |
| 构建服务 | `module-builder` Compose service 与冻结 lockfile 构建 | 内部 npm 镜像地址 | 未开始 |
| 安全投放与 iframe 预览 | `{contextPath}/modules/`、`CSP: sandbox`、iframe 预览和 postMessage Host API v1 | **威胁模型完成** | 未开始 |
| 发布与运行时回退 | 随 SkillVersion 发布、运行时健康检查和失败回退标准视图 | 安全投放与 iframe 预览 | 未开始 |

前置版本、静态拒绝和构建前置检查不执行生成代码，因此不受首次执行威胁模型阻塞，可以先行。

### 24.4 非目标

- **生成后端服务、数据库表、Secret 或新系统权限**：生成模块只负责模块内容与交互（`docs/07` §10）。
- **模块直接发起网络请求**：`connect-src 'none'`，一切经 Host API 受控 action。
- **把生成模块的权限混入普通 Web 应用**：`AGENTS.md` 的 TypeScript 规约不变。
- **独立子域名**：见 D2，已评估并选择同主机方案。

## 25. 前端信息架构重整（任务中心与待办出口）

### 25.1 目标

把「新功能逐个焊在旧画面上」积累的信息架构债还掉，让 Web 达到可交付形态。样式基线本身是成熟的
（token 体系、960/480 断点、`prefers-reduced-motion`、对比度已验），问题不在视觉而在**对象与画面的
对应关系**。

### 25.2 已定决策

- **任务是一等对象，必须有自己的画面。** `docs/07` §8 的 Task Center 早已写在规范里，但实现中
  任务只是工作空间弹窗里一个 `<select>` 的选项——它的就绪度、需要的数据来源、定时安排、上次执行
  分散在四处，用户无法回答「我现在能跑什么、哪些还差配置」。新增 `tasks` 画面承担这个职责。
- **任务中心「选」，工作空间「观」。** 前者横并排比较，后者聚焦一个 Run 的进展/结果/证据/待答项。
  **不在任务中心复制第二套执行 lifecycle**：立即执行跳转到工作空间。
- **定时执行属于任务，不属于「当前草稿」。** §22 的 Web 调度工作包曾把定时面板放在工作空间左轨，是因为当时没有
  任务画面。它绑定的是「此刻的草稿」，读不出是哪个任务的预定；现在迁到任务行内。工作空间左轨
  恢复为「新建执行 + 全高历史」两块。
- **等待必须可见。** Run 在等待回答/批准时不持有 lease 也不计 wall timeout，会无限期停住。
  「待你处理」因此常驻概览与导航徽标。**筛选在服务端完成**——只取历史首页再前端过滤会漏掉更早的
  等待项，而「漏报待办」比「不显示待办」更糟。
- **概览回答「我现在该做什么」，不复制导航。** 原先的功能模块 card 网格是 sidebar 入口的复制品；
  改为「待你处理 → 最近执行 → 服务/项目状态」。
- **任务与执行历史的关联用服务端的决定性 `task_id`。** 前端不自行推导该 UUID——推导方式一旦变化
  就会出现「能执行但历史对不上」的静默不一致。为此把 `derive_task_id` 收敛为单一实现，并在
  task descriptor 上公开。

### 25.3 实施工作包

| 工作包 | 边界 | 状态 |
|---|---|---|
| Task Center | `tasks` 路由与任务中心画面，就绪度/资源/定时/下次执行/上次执行和共用启动表单 | 本地完成 |
| 待办出口 | Run 历史服务端状态过滤、`PendingActionsPanel`、导航徽标和概览待办优先位 | 本地完成 |
| 路由与文案整理 | 死代码清理、三语 key 清理和 `docs/07` 路由/职责同步 | 本地完成 |
| 上次执行关联 | 服务端 `latest_run_by_task` 投影和模块筛选/跳转目标对齐 | 本地完成 |

### 25.4 非目标

- **Run / Interaction / Result 的独立路由**：目前它们在工作空间观测区内按当前 Run 呈现。改为独立路由
  需要先把 hash 路由换成 path 路由（见 `docs/07` §2.2 的取舍）。
- **跨 Project 的全局待办**：待办与 Run 一样按 Project 授权。
- **在任务中心执行第二套 Run lifecycle**：任务中心只负责选择，执行仍进入 Workspace。

## 26. 任务流程视图（Task Flow Projection）

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

Run 创建时冻结 `flow_checksum` 及对应的流程内容；后续 SkillVersion、Task Draft、Project overlay 或资源配置变化不得改变历史 Run。新的 Segment 可以基于同一 Run 的 frozen flow 和用户响应继续，但不能读取最新版本替换原流程。

**D7｜运行状态由事件投影得到。**

现有 `STEP_STARTED`/`STEP_COMPLETED`/`STEP_FAILED` 的 `step_id` 来自 Agent SDK 动态任务，不保证等于 Blueprint 节点 ID。第一版不得假定二者一一对应。后续可以增加可选的 `flow_node_ref` 关联，但必须校验它指向 frozen flow 中已存在的节点；缺少关联的事件显示为动态活动。Interaction、ChangeProposal、Evidence 和 Artifact 同样只增加可选关联，不改变现有状态机和审计 sequence。

**D8｜用户交互使用“流程节点 + 持久待办”双重出口。**

进入 `WAITING_FOR_INPUT` 或 `WAITING_FOR_APPROVAL` 时，流程图中的节点显示“等待你处理”，并可以打开平台 Modal/Drawer。关闭弹窗、离开页面后，待办仍必须通过概览、导航徽标和服务端筛选的 PendingActions 保留；弹窗不是唯一通知渠道。回答或批准仍沿用现有 Interaction/ChangeProposal API、CSRF、版本、过期和授权校验。

### 26.3 TaskFlowProjection 目标契约

后续新增 `TaskFlowProjection` contract。它是可选的展示/计划契约，不替代 CapabilityBlueprint：

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
