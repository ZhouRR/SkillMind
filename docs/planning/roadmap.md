# ProjectMind 实施计划

> 状态正本。最近核对：2026-09-08（工作副本文档/代码的只读核对）。最近部署证据仍为 2026-08-04 的[交付记录 §49](../history/delivery-history.md#49-服务器部署基线验收2026-08-04)；没有重新验收服务器或模型。

先看[当前状态](#13-当前执行状态)、[下一步](#132-下一步与当前决策)和[全项目工作登记](#133-全项目重构与缺失功能实施2026-09-05-启动)。设计正文见[文档导航](../README.md)，本页只管理范围、状态和门禁。

章节 13 保留原编号以兼容已有链接，但已移到最前。其余旧章节收在文末的[引用索引](#旧章节引用索引)，用于定位代码中的 docs/01 略记，不是第二套当前设计或进度表。

## 13. 当前执行状态

| 能力 | 实现证据 | 验收与限制 |
| --- | --- | --- |
| Run/认证/证据/评价/SSE | 现有模块、契约和回归测试；历史已交付 | 新部署仍需 authenticated smoke |
| Skill 导入/解释/发布/组织资产/项目启用 | 现有 skills/compositions 模块与契约 | 完整模型反复及人工质量报告待补 |
| Blueprint/Task Contract/Brief/交互与效果 | 现有通用契约、Segment 与 effect 链路 | 真实模型与 CAS adapter 专项验收待补 |
| 三语界面与托管凭据 | 现有 i18n、用户偏好和 MANAGED resolver | 浏览器三语/键盘与托管凭据端到端需复验 |
| 资源物化与 workspace.write（§19） | repository/document 物化、索引、历史、xlsx/xlsm/docx 文本化 | document 冻结处于工作副本联调阶段；仓库分支名不等于固定 commit；HTTPS remote 和模型使用待验 |
| repository.write（§20） | Git/SVN、direct/branch、Proposal 与 read-back | 真实 forge/并发/失败恢复端到端待验 |
| 过程重表达（§21） | 静态信号、system Skill、能力引用守卫 | 模型语义保真和 JAF 实机执行待验 |
| TaskSchedule（§22） | ONCE/CRON、Worker tick、API/Web、migration 0025 | 实际触发、重叠与漏发观测待验；不承诺精确一次 |
| 并行子分析（§23） | dispatch、能力收窄、单次预算分配、子 Session 记录 | Run 总预算尚无共享扣减；模型汇总效果待验 |
| 前端信息架构（§25） | Task Center、服务端待办、历史与任务关联 | 浏览器专项待验 |
| Task Flow（§26） | 设计完成 | 无 Flow Schema、持久化或页面实现 |
| generated FrontendModule（§24） | 版本模型、静态拒绝和构建前置检查 | 构建 service、投放、Host API 与回退未实现 |

### 13.1 完成状态的判定

- **实现完成**：代码、契约和对应回归存在；不意味着外部系统与模型效果已通过。
- **部署基线通过**：对应镜像、migration、健康检查、preflight 的有日期记录。
- **专项验收通过**：具体场景有环境、输入版本、命令/操作、结果与未覆盖项的可追溯记录。
- **设计完成**：边界与验收条件可评审，不能据此宣称 API 或功能可用。

2026-07-27 历史记录为本地 Backend 900 passed、Web 248 tests；这是当时的结果，本次不重复声称通过。2026-08-04 记录只证明部署基线，明确未覆盖应用认证 smoke、真实资源/效果、模型、调度触发和浏览器。

### 13.2 下一步与当前决策

| 顺序 | 工作 | 交付和门禁 |
| --- | --- | --- |
| 1 | 补齐运行约束差距 | [子 Agent 共享预算](../design/subagents.md#预算现状与修正设计)、[文档资源冻结](../design/resource-snapshots.md#已知差距与后续设计)；代码修改需同步契约与有状态回归 |
| 2 | Task Flow 只读投影 | 先复用现有数据，不增加执行控制器或假造节点完成；见 §26 |
| 并行准备 | 真实资源与模型专项验收 | 有合法测试项目/账号后跑 smoke、HTTPS Git/SVN、CAS/forge、调度与浏览器；测试写入使用专用目标 |
| 后续 | Flow 契约与 Run 快照 | 版本化 Draft、冻结内容、事件关联和旧 Run 回退 |
| 条件满足后 | generated 模块 | 内部依赖镜像、构建网络边界、威胁模型和浏览器隔离验证 |

共享预算差距会影响执行成本，资源范围与内容版本差距会影响数据可见性和重试可复现性，故优先于继续扩大自主能力。文档修订不改变 Runtime，也不替这些缺口作通过结论。

### 13.3 全项目重构与缺失功能实施（2026-09-05 启动）

用户目标：基于文档重构整个项目内的所有代码，并实现文档中已经定义但尚未实现的功能。

范围覆盖 Backend、Web、contracts、Skill 执行资产、脚本、构建/部署配置及对应测试。以下是全量工作入口，
不是把目标缩减为第一批缺口；每一领域仍需逐条核对设计中的字段、行为、不变量、失败路径和验收条件。
现行规范是依据，history 中已被替代的设计不重新引入；文档明确禁止或列为非目标的能力不被解释为待实现功能。

| 工作 | 规范与代码范围 | 完成证据要求 | 当前状态 |
| --- | --- | --- | --- |
| R01 资源冻结 | 资源快照；documents / integrations / runs / agent、Preflight 与 Run 详情 | 显式单份/集合/全集、创建时冻结、不可越权/漂移、重试与历史兼容 | 实施中：后端清单/ID/hash 改动存在；选择 UI、公开快照、幂等与既有回归未贯通 |
| R02 Run 统一预算 | 子分析与 Runtime；agent / runs / worker / DB | 主子共同计费、原子预留、幂等结算、跨 Segment/Attempt 恢复与并发测试 | 待实现 |
| R03 Task Flow 完整链路 | Task Flow、Workspace、Interpreter；contracts / skills / runs / Web | 预览、契约/Draft/diff/发布、不可变 Run 流程、事件关联、待办/证据联动、三语与历史回退 | 待实现全部工作包 |
| R04 生成模块 | generated FrontendModule；modules / API / Web / builder / Compose | 威胁模型、依赖与网络隔离、构建测试、安全投放、Host 协议、发布与回退；禁止仅静态通过即执行 | 前置代码待重新核对，其余待实现 |
| R05 领域与身份安全 | 领域模型、认证；auth / projects / documents / integrations / core / DB | 所有权、CSRF/Origin、角色、Secret、并发与不变数据约束的正负向回归 | 待逐模块核对与重构 |
| R06 Skill 生命周期 | Skill 契约与解释实现；skills / compositions / contracts / Web / system Skill | 导入到解释、版本发布与项目启用、来源/identity、动态契约、兼容与错误恢复 | 待逐条核对与补齐 |
| R07 Run 与审计 | Runtime；runs / worker / agent / events / evidence / evaluations / storage | Segment/Attempt/Session、Outbox/lease、恢复/取消/等待期限、结果与 SSE 重放的有状态验证 | 待逐条核对与重构 |
| R08 外部效果 | 受控写入；effects / integrations / repository / Redmine / Web | 提案、精确批准/预授权、CAS/幂等、read-back、Git/SVN 模式与部分失败恢复 | 待重新验收与补齐 |
| R09 调度 | TaskSchedule；schedules / worker / API / Web | ONCE/CRON/timezone/DST、预览、失效、错过/重叠、同一创建路径及已声明保证 | 待重新验收与补齐 |
| R10 全部 Web 页面 | Workspace 与产品概览；pages / components / API / lib / styles / i18n | 页面职责、服务端筛选、并发请求清理、三语/键盘/窄屏、真实用户流程 | 待逐页核对与重构 |
| R11 运维与工程工具 | 开发/运维规范；ops / migrations / scripts / images / Compose / Dockerfile | 锁定依赖、静态检查、启动/迁移、backup/restore/rollback、KEK 保留、清理策略与 smoke | 待核对；不得操作未知生产数据 |
| R12 业务质量验收 | JAF acceptance；通用运行链与业务 Skill/评价数据 | 文档定义的 case/指标/人工评价、Gold 隔离、规则保真；不把 fixture 结果冒充真实模型质量 | 待构建/执行可获得的验收，其余保留外部条件 |
| R13 全量契约与最终审计 | 全部现行设计、AGENTS、contracts 和工程入口 | 逐需求证据、Schema/example/OpenAPI 同步、Backend/Web/DB/浏览器/部署范围匹配的测试 | 待全量复验；不能用局部绿色结果判定总目标完成 |

本表保留既有全项目实施任务登记；本次继续整理文档并不新增代码实施或部署授权。所有状态与未验收项继续保留在本节/§13；完成的实现细节回写对应设计和有日期的交付记录。
环境、账号、内部依赖镜像或真实业务数据不足时先完成可独立推进的代码与测试，不擅自采用公网依赖或放宽安全门禁。

### 13.4 最近文档核对（2026-09-08）

本轮采用代码/契约只读核对来修正文档，没有修改业务代码，也没有重新执行全量应用或部署验收。文档、契约与文档浏览器的实际检查结果见[交付记录 §51](../history/delivery-history.md#51-文档设计边界与阅读体验续整2026-09-08)。

| 发现 | 已整理的设计依据 | 后续实施/验收责任 |
| --- | --- | --- |
| document 旧全集说明与工作副本冻结改动不同步 | [范围、时序、幂等与历史](../design/resource-snapshots.md) | R01 完成端到端同步后才能标记已交付 |
| 冻结 repository 分支名被误解为固定内容 | [授权与内容版本](../design/resource-snapshots.md#仓库授权与内容版本) | R01 区分首次物化基线、live Evidence 与缓存来源检查 |
| 每个物化根的额度被表述成 Run 总额度 | [产物与访问](../design/resource-snapshots.md#产物与访问) | R01 的跨根总量与 R02 的模型预算分别实现和验证 |
| 资源缺失时澄清被误解为可在原 Run 中新增权限 | [Runtime 暂停边界](../design/agent-runtime.md#52-必须暂停的情况) | R07 保持创建校验与续行边界一致 |
| JAF 建议目录混放导入资产与 Gold | [隔离目录](../acceptance/jaf-quality.md#3-迁移目标目录) | R12 实际验收时检查导入包、Project 资源与模型可见面 |
| Runbook 残留禁止 SVN write、所有 Session 同时唯一等旧规则 | [运行排障](../operations/runbook.md#8-対話型-run-と外部-effect-の運用契約) | R08/R11 按当前 Provider 和 PRIMARY/SUBAGENT 边界验收 |

## 旧章节引用索引

以下保留旧 PLAN 的章节与锚点，方便从代码注释追溯；新开发直接阅读链接到的领域设计，当前状态以 §13 为准。完整旧正文见[再编前快照](../history/unified-plan-2026-08-04.md)。

### 1. 产品定位

见[产品概览](../overview/product.md)：通用 Skill 驱动的项目任务平台，JAF 是验收场景。

### 2. 核心原则

#### 2.1 开放导入与版本发布

导入不执行来源；Interpreter 生成蓝图，管理员审查后发布精确版本。见[Skill 契约](../design/skill-contract.md)。

#### 2.2 能力与资源要求

蓝图描述目标、规则和资源要求，不授予 Tool 权限。

#### 2.3 Project 配置

Project 显式启用 SkillVersion，并独立配置资源与成员。

#### 2.4 Tool 访问

凭据、scope 和审计在平台 Provider 边界内强制。

#### 2.5 自动执行与外部变更

已注册的读取和 workspace 能力按快照执行；来源脚本没有通用执行器。外部变更见[受控写入](../design/repository-effects.md)。

#### 2.6 部署和数据

见[系统结构](../overview/architecture.md#部署边界)与[认证](../design/authentication.md)。数据保留期限、清理任务及恢复演练仍需明确验收，不把产品目标当成已安装服务。

#### 2.7 Agent 自主范围

建议步骤可调整；权限、必需规则、批准、结果不可变性由平台约束。

### 3. 核心模型

见[领域模型](../design/domain-model.md)与[术语](../overview/glossary.md)。

### 4. 通用 Skill 兼容机制

见[Skill 契约](../design/skill-contract.md)和[解释实现](../design/skill-interpretation.md)。

### 5. 数据源和 Tool

#### 5.1 职责模型

资源要求唯一来源为 Blueprint；实际绑定在 Project/Task 配置与 Run 快照中。

#### 5.2 已开放能力

以[已安装能力与边界](../design/agent-runtime.md#61-tool-分类)为准。未注册的 search/export/tabular 能力不能因为旧规划列出名称就成为可用 API。

### 6. Agent Runtime

#### 6.1 运行流程

见[Runtime 生命周期](../design/agent-runtime.md#71-生命周期)。

#### 6.2 AgentEngine

见[实际接口](../design/agent-runtime.md#3-agentengine-抽象)。

#### 6.3 Session 与工作目录

一个 Run 允许顺序主 Session 和有界只读子 Session。workspace 是执行上下文，Provider 校验才是文件访问边界。

#### 6.4 运行对象

Run → Segment → Attempt；Session 是执行载体，Evidence/Result/Evaluation 分别记录事实、原始结论与人工反馈。

### 7. Workspace

见[页面与交互](../design/workspace.md)。任务中心选择任务，工作空间观察单个 Run。

### 8. 当前产品范围

见[产品边界](../overview/product.md#产品边界)。开发完成、部署基线通过、专项验收完成分别记录。

### 9. JAF 验证场景

见[JAF 验收](../acceptance/jaf-quality.md)。30 case benchmark 是验收要求，尚无完成证据。

### 10. 早期建设路径

见[交付历史](../history/delivery-history.md)。不再从早期 seed/专用 Schema 方案启动新开发。

### 11. 技术架构

见[系统结构](../overview/architecture.md)和[技术结构图](../overview/technical-architecture.html)。

### 12. 设计决策与开放问题

| 决策 | 当前约束 |
| --- | --- |
| 蓝图唯一来源 | 只由 Interpreter 生成；Manifest 不反推蓝图 |
| 版本冻结 | SkillVersion、Run 输入/权限/资源、Result 不原地修改 |
| 持续 Run | 业务答复追加 Segment，技术重试追加 Attempt |
| 外部写入 | 精确 Proposal 批准；repository 永不预授权 |
| Task Flow | 展示投影，保持 Agent 自主执行 |
| generated 模块 | 同主机专用路径与 opaque origin；首次执行前完成威胁模型 |
| 开放问题 | Run 共享预算、document 快照边界、数据保留与部署专项验收 |

### 14. 动态 Task Contract（已实现）

业务 Schema 是可选派生契约。见[Skill 契约 §4.4](../design/skill-contract.md#44-taskblueprint-与动态参数)；历史计划见交付历史 §29.1。

### 15. 能力蓝图与交互式 Agent Runtime

#### 15.1 目标

目标与通用协议见[解释实现](../design/skill-interpretation.md)。

核心不变量：effect intent 不等于 permission；guidance 完整传入 Agent，但写入授权由平台注册能力与批准链路独立决定。

#### 15.2 使用体验

见[产品概览](../overview/product.md#一次使用过程)。

#### 15.3 工作与验收

实现已进入回归基线；专项验收按 §13.2 与[运维 Runbook §4](../operations/runbook.md#4-generic-task-acceptance-と障害接管)。

#### 15.4 兼容

旧 Run 的 implicit Segment 与旧结果只读保留；蓝图反向兼容投影不恢复。

#### 15.5 非目标

本工作包当时不含调度与并行子分析；两者后由 §22/§23 实现，不能继续解释为当前全局禁用项。

### 16. Skill 库作用域（已实现）

见[领域模型 §2](../design/domain-model.md#2-组织项目与资源)。

### 17. 界面三语（已实现）

见[Workspace §14](../design/workspace.md#14-可访问性国际化与隐私)。

### 18. 托管凭据（已实现）

见[认证 §7](../design/authentication.md#7-secret-storage)与[Runbook](../operations/runbook.md)。

### 19. 资源快照物化与工作区

#### 19.1 目标

见[资源快照](../design/resource-snapshots.md)。

#### 19.2 决策

见资源快照中的资源范围、产物、限制和失败策略；不新增按动作划分的 repository search/log Tool。

#### 19.3 工作包

当前实现与差距统一见 §13；完整旧工作包表保留在归档。

#### 19.4 非目标

任意 Shell、开放网络和任意源脚本执行。

#### 19.5 验收

见[资源快照验收](../design/resource-snapshots.md#验收条件)：先项目文档，再仓库快照，最后真实凭据与模型使用。

#### 19.6 Brief 告知

只把物化器实际返回的路径与 revision 写入 Brief，不推测不存在的文件。

### 20. 代码仓库回写

#### 20.1 目标

审批后落地可追溯变更。

#### 20.2 决策

见[受控写入](../design/repository-effects.md)。direct 默认、Git/SVN 均支持；旧“仅分支/禁止 SVN”决策已被后续交付取代。

#### 20.3 工作包

实现已存在；真实外部系统专项验收见 §13。

#### 20.4 非目标

自动批准、自动合并、force push、多仓库原子提交。

### 21. 解释器过程重表达

见[解释实现 §5.4](../design/skill-interpretation.md#54-过程重表达)。脚本声明是理解输入，不能成为宿主执行授权。

### 22. 任务调度

#### 22.1 目标

按时以原创建者身份启动普通 Run。

#### 22.2 决策

见[TaskSchedule](../design/task-scheduling.md)。

#### 22.3 工作包

领域/API/Worker/Web 已实现；真实发火观测待验。

#### 22.4 非目标

条件监控、停机补跑、排队与全局精确一次调度。

### 23. 并行子 Agent

#### 23.1 目标

同一 Run 中的有界只读子分析。

#### 23.2 决策

见[并行子分析](../design/subagents.md)。

#### 23.3 工作包

dispatch、能力收窄、结果呈现与 Session 持久化已实现；共享总预算账本未实现。

#### 23.4 非目标

子 Agent 写入、提案、用户交互、递归扇出和跨 Run 编排。

#### 23.5 验收

分别验证安全边界、跨多次 dispatch 的预算、失败覆盖范围与真实模型汇总质量。

### 24. generated FrontendModule

#### 24.1 目标

生成源码，经构建与安全投放后在隔离 iframe 中呈现。

#### 24.2 决策

见[生成模块设计](../design/generated-modules.md)。CSP opaque origin 不等价于独立站点，也不保证导航请求不带 Cookie。

#### 24.3 工作包

版本/静态拒绝/构建前置已实现；构建 service → 安全投放与 Host API → 运行时回退依次实施。首次执行前必须完成威胁模型。

#### 24.4 非目标

生成后端、数据库表、权限；直接网络访问；绕过 Host 执行任务或批准。

### 25. 前端信息架构（已实现）

现行页面与职责见[Workspace](../design/workspace.md)。不在计划中继续维护旧页面诊断。

### 26. 任务流程视图

#### 26.1 目标

让用户看懂资源、处理、确认点和产物。

#### 26.2 决策

见[Task Flow 设计](../design/task-flow.md)。

#### 26.3 目标契约

后续 TaskFlowProjection 为可选协议；不是当前已冻结的 JSON Schema。

#### 26.4 用户体验

Task 说明建议流程，Run 显示实际发生的活动与待办。

#### 26.5 实施工作包

只读投影 → Flow 契约/Draft → Run 快照与观察 → 交互/证据联动。当前尚未开始实现。

#### 26.6 非目标

严格 DAG、循环执行、任意表达式、拖拽扩权及第二套 Run lifecycle。

#### 26.7 完成标准

见 Task Flow 的验收表；每阶段保留无 Flow 的旧 Skill 与历史 Run 回退。
