# ProjectMind 实施计划

> 状态正本。2026-09-08 最新核对见 [§13.18](#1318-调度事实并发与管理文档核对2026-09-08)：纠正重叠范围与原子版本保护的描述，明确摘要、持久恢复和分页管理的差距。此前物化/效果局部回归见 §13.17，全量基线未重新执行。部署证据仍为 2026-08-04 的[交付记录 §49](../history/delivery-history.md#49-服务器部署基线验收2026-08-04)，不据文档或局部回归宣称全项目完成。

先看[当前状态](#13-当前执行状态)、[下一步](#132-下一步与当前决策)和[全项目工作登记](#133-全项目重构与缺失功能实施2026-09-05-启动)。设计归属见[设计责任索引](../design/README.md)，核对证据按[历史索引](../history/README.md)查找。本页只管理范围、状态和门禁。

章节 13 保留原编号以兼容已有链接，但已移到最前。其余旧章节收在文末的[引用索引](#旧章节引用索引)，用于定位代码中的 docs/01 略记，不是第二套当前设计或进度表。

## 13. 当前执行状态

| 能力 | 实现证据 | 验收与限制 |
| --- | --- | --- |
| Run/认证/证据/评价/SSE | 现有模块、契约和回归测试；历史已交付 | 新部署仍需 authenticated smoke |
| Run 创建与原请求确认 | Backend 意图/重放、Web 原内容/键、文档选择/详情；契约与消费者局部回归通过 | 真实 DB 并发/回滚、版本混合与历史续行仍需验收 |
| Skill 导入/解释/发布/组织资产/项目启用 | 现有 skills/compositions 模块与契约；发布/启停边界已核对 | 停用后同版重新启用与审计未实现；完整模型反复及人工质量报告待补 |
| Blueprint/Task Contract/Brief/交互与效果 | 现有通用契约、Segment 与 effect 链路 | 真实模型与 CAS adapter 专项验收待补 |
| 三语界面与托管凭据 | 现有 i18n、用户偏好和 MANAGED resolver；文档选择/清单三语已接入 | 原请求、显式范围与调度输入有局部 mock 浏览器证据；全页面与托管凭据端到端仍需复验 |
| 资源物化与 workspace.write（§19） | 回执/0029、Worker/Tool、准备监督、启动 gate 与一次性完成确认已有代码；物化消费者及相关回归通过 | 跨根故障、整链/真实提交与迁移恢复、首事件前取消、HTTPS remote 与模型使用待验 |
| repository.write（§20） | Git/SVN、direct/branch、Proposal 与 read-back；本地临时仓库和部分审批/Provider 回归通过 | 原执行身份证明、精确 CAS、阶段回执、Effect 监督和审批重发待补齐；真实 forge/并发/失败恢复端到端待验 |
| 过程重表达（§21） | 静态信号、system Skill、能力引用守卫 | 模型语义保真和 JAF 实机执行待验 |
| TaskSchedule（§22） | ONCE/CRON、Worker tick、API/Web、migration 0025；重放/时间/API 与 Web 投影有局部回归 | 重叠只查本 Schedule 的摘要指针；原子编辑、持久在途/计数/迟到、分页/失效可见性、编辑与时区修正待补 |
| 并行子分析（§23） | dispatch、能力收窄、单次预算分配、子 Session 记录 | Run 总预算尚无共享扣减；模型汇总效果待验 |
| 前端信息架构（§25） | Task Center、服务端待办、历史与任务关联 | 浏览器专项待验 |
| Task Flow（§26） | 设计完成 | 无 Flow Schema、持久化或页面实现 |
| generated FrontendModule（§24） | 版本模型、静态拒绝和构建前置检查 | 构建 service、投放、Host API 与回退未实现 |

### 13.1 完成状态的判定

- **实现完成**：代码、契约和对应回归存在；不意味着外部系统与模型效果已通过。
- **部署基线通过**：对应镜像、migration、健康检查、preflight 的有日期记录。
- **专项验收通过**：具体场景有环境、输入版本、命令/操作、结果与未覆盖项的可追溯记录。
- **设计完成**：边界与验收条件可评审，不能据此宣称 API 或功能可用。

历史回归只证明对应版本和所列条件，不自动覆盖工作副本。2026-08-04 记录只证明部署基线，明确未覆盖应用认证 smoke、真实资源/效果、模型、调度触发和浏览器。最新局部检查也不能替代一次全量验收。

### 13.2 下一步与当前决策

| 顺序 | 工作 | 交付和门禁 |
| --- | --- | --- |
| 1 | 补齐运行约束差距 | 保持原请求确认与公开文档链路的回归，继续真实事务验收、[可信缓存与跨根总量](../design/resource-snapshots.md#已知差距与后续设计)、[Run 共享预算](../design/run-budgets.md)和[调度可靠性](../design/task-scheduling.md#可靠性修正要求待实现)，同步契约与有状态回归 |
| 2 | Task Flow 只读投影 | 先复用现有数据，不增加执行控制器或假造节点完成；见 §26 |
| 并行准备 | 真实资源与模型专项验收 | 有合法测试项目/账号后跑 smoke、HTTPS Git/SVN、CAS/forge、调度与浏览器；测试写入使用专用目标 |
| 后续 | Flow 契约与 Run 快照 | 版本化 Draft、冻结内容、事件关联和旧 Run 回退 |
| 条件满足后 | generated 模块 | 内部依赖镜像、构建网络边界、威胁模型和浏览器隔离验证 |

共享预算差距会影响执行成本，资源范围与内容版本差距会影响数据可见性和重试可复现性，故优先于继续扩大自主能力。文档修订不改变 Runtime，也不替这些缺口作通过结论。

### 13.3 全项目重构与缺失功能实施（2026-09-05 启动）

此登记保留既有全项目实施范围；本次文档整理只核对现有代码、修正规则表达和接续入口，不执行全项目代码重构。R01 从跨根故障、真实提交确认/事务与历史续行继续，不重做已有选择、消费者、store、准备监督或启动 gate。一次文档整理或局部实现不代替整体完成。

范围覆盖 Backend、Web、contracts、Skill 执行资产、脚本、构建/部署配置及对应测试。以下是全量工作入口，
不是把目标缩减为第一批缺口；每一领域仍需逐条核对设计中的字段、行为、不变量、失败路径和验收条件。
现行规范是依据，history 中已被替代的设计不重新引入；文档明确禁止或列为非目标的能力不被解释为待实现功能。

| 工作 | 规范与代码范围 | 完成证据要求 | 当前状态 |
| --- | --- | --- | --- |
| R01 资源冻结 | 资源快照与创建；documents / integrations / runs / agent、Preflight 与 Run 详情 | 显式单份/集合/全集、创建时冻结、不可越权/漂移、重放与历史兼容 | 实施中：回执/Tool、监督/启动 gate、配置、消费者及一次性完成确认已有接线和局部回归；跨根故障、整链/真实提交与历史续行待验 |
| R02 Run 统一预算 | [预算设计](../design/run-budgets.md)、子分析与 Runtime；agent / runs / worker / DB | 主子共同计费、原子预留、幂等结算、跨 Segment/Attempt 恢复与并发测试 | 待实现；已细化计量口径、预留和不确定用量恢复要求 |
| R03 Task Flow 完整链路 | Task Flow、Workspace、Interpreter；contracts / skills / runs / Web | 预览、契约/Draft/diff/发布、不可变 Run 流程、事件关联、待办/证据联动、三语与历史回退 | 待实现全部工作包 |
| R04 生成模块 | generated FrontendModule；modules / API / Web / builder / Compose | 威胁模型、依赖与网络隔离、构建测试、安全投放、Host 协议、发布与回退；禁止仅静态通过即执行 | 前置代码待重新核对，其余待实现 |
| R05 领域与身份安全 | 领域模型、认证；auth / projects / documents / integrations / core / DB | 所有权、CSRF/Origin、角色、Secret、并发与不变数据约束的正负向回归 | 待逐模块核对与重构 |
| R06 Skill 生命周期 | Skill 契约与解释实现；skills / compositions / contracts / Web / system Skill | 导入到解释、版本发布与项目启用、来源/identity、动态契约、兼容与错误恢复 | 发布/启停已只读核对；同版重新启用/审计与回滚待实现，组合尚无跨 Skill 规则求解；其余仍需逐条核对 |
| R07 Run 与审计 | Runtime；runs / worker / agent / events / evidence / evaluations / storage | Segment/Attempt/Session、Outbox/lease、恢复/取消/等待期限、结果与 SSE 重放的有状态验证 | 准备监督与启动校验有局部回归；首事件前取消、实际进程停止、异常收尾及其余生命周期待逐条核对与验收 |
| R08 外部效果 | [受控写入](../design/repository-effects.md#可靠性修正要求)；effects / integrations / repository / Redmine / Web | 提案、精确批准/预授权、CAS/原执行身份、阶段回执、Effect lease、read-back、Git/SVN/PR 部分失败恢复 | 已核对内容比较不能证明原执行、SVN 基线/回读未固定、无独立阶段回执/贯穿监督；审批卡片换键与旧 branch-only Schema 待同步。局部通过不替代修正/真实验收 |
| R09 调度 | [TaskSchedule](../design/task-scheduling.md)；schedules / worker / API / Web | 时间/预览、同 Schedule 重叠、原子配置更新、持久在途/执行权、幂等计数与历史兼容 | 已核对摘要/先读后写/首 100 条的限度；恢复协议、并发/计数/迟到、管理/编辑入口和时区修正待实现，局部通过不证明真实事务 |
| R10 全部 Web 页面 | Workspace 与产品概览；pages / components / API / hooks / lib / styles / i18n | 页面职责、服务端筛选、并发请求清理、三语/键盘/窄屏、真实用户流程 | 原请求、文档选择/详情与调度输入有局部 mock 浏览器证据；已定位 Schedule 首批与 TaskCatalog 结合的漏显风险，其余页面仍待逐项核对与重构 |
| R11 运维与工程工具 | 开发/运维规范；ops / migrations / scripts / images / Compose / Dockerfile | 锁定依赖、静态检查、启动/迁移、backup/restore/rollback、KEK 保留、清理策略与 smoke | 已定位 ENV_FILE/容器 .env 来源不统一、deploy 不分阶段放行、0027 downgrade 删除审计会话的风险；配置修正/真实恢复演练仍待完成；既有业务浏览器 Ruff 问题见 §13.11 |
| R12 业务质量验收 | JAF acceptance；通用运行链与业务 Skill/评价数据 | 文档定义的 case/指标/人工评价、Gold 隔离、规则保真；不把 fixture 结果冒充真实模型质量 | 待构建/执行可获得的验收，其余保留外部条件 |
| R13 全量契约与最终审计 | 全部现行设计、AGENTS、contracts 和工程入口 | 逐需求证据、Schema/example/OpenAPI 同步、Backend/Web/DB/浏览器/部署范围匹配的测试 | §13.16 的失败在对应局部回归中不再复现；全量未再执行，其他领域仍待完整核对与复验，不能用局部绿色判定总目标完成 |

本表保留 R01–R13 全项目实施任务与既有进展。接续开发不重做已存在的选择/清单入口，从剩余不变量和未覆盖环境继续验收。资源准备回执需要同步 DB/migration、物化器、Worker heartbeat/fencing、workspace Provider、测试与恢复说明；不运行部署或未知生产数据操作。下面的有日期章节只保留证据入口，历史细节不在计划页重复维护，后续代码结果单独记录。
环境、账号、内部依赖镜像或真实业务数据不足时先完成可独立推进的代码与测试，不擅自采用公网依赖或放宽安全门禁。

### 13.4 最近文档核对（2026-09-08）

资源范围、仓库基线、JAF 数据隔离与运维旧描述的核对见[交付记录 §51](../history/delivery-history.md#51-文档设计边界与阅读体验续整2026-09-08)。本锚点保留供旧链接使用；当前差距由 §13.3 与领域设计维护。

### 13.5 预算、生命周期与文档工具续整（2026-09-08）

预算设计、Run 状态速查和文档工具的当轮验证见[交付记录 §52](../history/delivery-history.md#52-预算生命周期与文档工具续整2026-09-08)。当时未修改业务逻辑，不把文档回归作为 R02 实现证据。

### 13.6 R01 后端读取与物化接入（2026-09-08）

冻结清单进入读取/物化的当轮实现与 Backend 回归见[交付记录 §53](../history/delivery-history.md#53-r01-冻结文档读取与物化接入2026-09-08)。不覆盖之后的回执接口重构；当前未完成范围以 §13.3 为准。

### 13.7 创建与调度设计续整（2026-09-08）

Run 创建/幂等独立设计、调度事务与恢复要求的来源见[交付记录 §54](../history/delivery-history.md#54-创建调度与开发入口续整2026-09-08)。该轮是文档整理，不是运行时修复。

### 13.8 工作副本与交接核对（2026-09-08）

当时的客户端缺口与调度测试 NameError 见[交付记录 §55](../history/delivery-history.md#55-工作副本对齐与开发交接文档续整2026-09-08)。后续已有修正，不从此历史记录判断当前 UI 或测试。

### 13.9 设计导航与提交状态核对（2026-09-08）

设计责任索引、原请求确认与当轮局部回归见[交付记录 §56](../history/delivery-history.md#56-设计导航与提交状态文档续整2026-09-08)。mock API 浏览器证据不证明真实 DB 并发。

### 13.10 公开契约交接与章节检索（2026-09-08）

契约 example/OpenAPI/typecheck 的当时失败、交接指南与章节搜索见[交付记录 §57](../history/delivery-history.md#57-公开契约交接与章节检索文档续整2026-09-08)。这些失败在 §13.11 的后续核对中已不再复现；原证据不倒改，也不在本页保留一张易被误读为当前缺口的重复表。

### 13.11 资源公开链路与调度边界核对（2026-09-08）

公开选择/投影/三语的局部通过证据、调度时间与编辑缺口、业务浏览器脚本静态检查问题见[交付记录 §58](../history/delivery-history.md#58-资源公开链路与调度边界文档续整2026-09-08)。当轮收录的文档浏览工具只验证离线文档，不是业务 UI 或 R01 全部完成的证明。

### 13.12 运维恢复与工作副本边界核对（2026-09-08）

环境文件来源、完整恢复点、migration/回退风险和当时尚未接入的回执草稿见[交付记录 §59](../history/delivery-history.md#59-运维恢复与工作副本文档续整2026-09-08)。物化器/ContextBuilder 在之后已有新改动，以 §13.13 接续，不修改过去记录。

### 13.13 输入准备协议与文档交接核对（2026-09-08）

Run 级输入协议、物化器/ContextBuilder 接线和当时的构造器失败见[交付记录 §60](../history/delivery-history.md#60-输入准备协议与开发交接文档续整2026-09-08)。之后 Worker/Tool 已有进一步改动，按 §13.14 接续；不从当时的未接线说明推导当前代码，也不倒改原验证记录。

### 13.14 阅读导航、流程语义与现状核对（2026-09-08）

目的导航、Flow 语义和当时的 Worker/Tool 核对见[交付记录 §61](../history/delivery-history.md#61-阅读导航流程语义与现状文档续整2026-09-08)。当时的“准备之后才启动监督”已被后续工作副本修改，以 §13.15 接续，不倒改历史证据。

### 13.15 执行边界、计时器与交接文档核对（2026-09-08）

执行监督、计时器、原生刷新修正与当轮 78 项针对性通过见[交付记录 §62](../history/delivery-history.md#62-执行边界计时器与交接文档续整2026-09-08)。当时只复现一个旧物化调用失败；§13.16 的全量基线进一步明确了失败范围，不倒改原结果。

### 13.16 Skill 生命周期与开发文档核对（2026-09-08）

Skill 阶段、Manifest 标识、重新启用目标与当时 Backend 全量基线的 46 项失败见[交付记录 §63](../history/delivery-history.md#63-skill-生命周期与开发文档续整2026-09-08)。之后工作副本已有消费者/完成确认改动，按 §13.17 接续；旧成功/失败记录保留，不从旧 fixture 失败推导当前范围校验或迁移失败。

### 13.17 外部效果与恢复文档核对（2026-09-08）

受控写入的事实/事务/身份/阶段恢复核对、既有 Backend 174 项与 Web 25 项局部通过见[交付记录 §64](../history/delivery-history.md#64-外部效果与恢复文档续整2026-09-08)。该轮没有修复业务代码；物化消费者及迁移 head 的旧断言失败在相应回归中不再复现，input commit 确认仍只是 mock transaction 证据，不替代真实 PostgreSQL 恢复或全量验收。

### 13.18 调度事实、并发与管理文档核对（2026-09-08）

本轮继续整理文档，没有修改应用代码/测试、Schema、migration、配置或执行 Skill。调度设计改为具体例子、实际范围、故障边界与分项修正：重叠只检查本 Schedule 的 last_run_id；expected_row_version 尚非原子 CAS；last_* 不是完整账本；首 100 条与当前任务卡结合可能隐藏失效规则。认领执行权、原子编辑、未知提交的名额保留、幂等结算、历史迁移和管理可见性均保留为待实现要求。

当前调度相关 Backend **65 项通过**，Web API/Task Center 投影 **10 项通过**；前者含纯时间逻辑与 fake service/Worker，后者含 client 与静态投影，不覆盖真实锁竞争、持久恢复、100 条以上分页或失效任务管理。阅读检查与证据见[交付记录 §65](../history/delivery-history.md#65-调度事实并发与管理文档续整2026-09-08)。全量 Backend/Web、真实 DB、业务浏览器、模型、远端与部署未重跑，R01–R13 继续按 §13.3 执行。

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
