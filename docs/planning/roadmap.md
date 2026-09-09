# ProjectMind 实施计划

> 状态正本。2026-09-09 本轮继续文档整理：核对用户管理已经接入的 API/Schema，与未同步的 OpenAPI、未接入的 Web 分开；记录合跑收集失败，整理代码/契约入口和失败处理。没有修改应用实现、应用测试或公开契约；R01–R13 的完整范围、已有代码和真实环境残项保留。

工作副本、全量本地基线和部署不是同一个版本。先用[证据范围表](#当前证据怎么用)确定所读结论适用的范围，不从最近一次绿色数字推导全项目完成。

先看[当前状态](#13-当前执行状态)、[下一步](#132-下一步与当前决策)和[全项目工作登记](#133-全项目重构与缺失功能实施2026-09-05-启动)。设计归属见[设计责任索引](../design/README.md)，核对证据按[历史索引](../history/README.md)查找。本页只管理范围、状态和门禁。

章节 13 保留原编号以兼容已有链接，但已移到最前。其余旧章节收在文末的[引用索引](#旧章节引用索引)，用于定位代码中的 docs/01 略记，不是第二套当前设计或进度表。

## 13. 当前执行状态

| 能力 | 当前基础与接续 |
| --- | --- |
| Run 与审计 | 模块、契约、SSE 与评价已有历史交付；新部署仍需 authenticated smoke，当前停止与事务残项见 [R07](#r07-run-与审计) |
| 身份与授权 | 会话 v2/0031、三维防护、认证错误声明与登录表单有局部回归；用户生命周期、跨页面会话及实 DB/代理待验，见 [R05](#r05-领域与身份安全) |
| 创建与确认 | 原意图重放、Web 原内容/键、文档选择/详情有局部回归；实 DB 并发/回滚、混合版本与历史续行仍待验，见 [R01](#r01-资源冻结) |
| Skill 生命周期 | 导入/解释/发布、组织资产、项目启用已有模块；同版重新启用/审计与完整模型质量未完成，见 [R06](#r06-skill-生命周期) |
| 通用契约与交互 | Blueprint、Task Contract、Brief、Segment 与 effect 链已有代码；真实模型与 CAS 专项验收待补，见 [R07](#r07-run-与审计) / [R08](#r08-外部效果) |
| 三语与凭据 | i18n/偏好、文档清单与 MANAGED resolver 已有代码；局部 mock browser 不覆盖全页面或 Secret 端到端，见 [R10](#r10-全部-web-页面) / [R05](#r05-领域与身份安全) |
| 输入物化 | 回执/0029、监督/启动 gate、完成确认与命名空间独占有本地回归；实事务/迁移恢复、进程/HTTPS/模型仍待验，见 [R01](#r01-资源冻结) |
| 仓库回写 | Git/SVN direct/branch、Proposal/read-back 有局部回归；原执行 identity、精确 CAS、阶段回执/监督与审批重发待补，见 [R08](#r08-外部效果) |
| 过程重表达 | 静态信号、system Skill、能力引用守卫已有代码；模型语义保真和 JAF 实机仍待验，见 [R06](#r06-skill-生命周期) / [R12](#r12-业务质量验收) |
| 时刻起动 | ONCE/CRON、tick、API/Web/0025 有局部回归；重叠限于本 Schedule 摘要，原子编辑/恢复/计数/迟到/分页与时区待补，见 [R09](#r09-调度) |
| 并行子分析 | dispatch、终端校验、v1 Session 记录与清理有局部回归；子指令/输出、审计恢复/降级、完整停止与共享预算待补，见 [R07](#r07-run-与审计) / [R02](#r02-run-统一预算) |
| 共享预算 | DTO、三表/0030、repository/store 有离线回归，但创建/主子执行未调用；计量、核对方与实 DB/历史/公开投影待补，见 [R02](#r02-run-统一预算) |
| 页面组织 | Task Center、服务端待办、历史关联已有代码；完整浏览器专项待验，见 [R10](#r10-全部-web-页面) |
| Task Flow | 设计已定义；尚无 Flow Schema、持久化与页面，见 [R03](#r03-task-flow-完整链路) |
| 生成界面 | 模型/0026、状态与前置检查有局部回归；CSP/构建身份/发布证明和完整运行链未完成，Manifest 只允许 null，见 [R04](#r04-生成模块) |

概览把基础与限制放在同一格，详细范围和验收集中到对应 R 项；“有局部回归”不是该能力整体可交付的标签。

### 13.1 完成状态的判定

- **实现完成**：代码、契约和对应回归存在；不意味着外部系统与模型效果已通过。
- **部署基线通过**：对应镜像、migration、健康检查、preflight 的有日期记录。
- **专项验收通过**：具体场景有环境、输入版本、命令/操作、结果与未覆盖项的可追溯记录。
- **设计完成**：边界与验收条件可评审，不能据此宣称 API 或功能可用。

历史回归只证明对应版本和所列条件，不自动覆盖工作副本。2026-08-04 记录只证明部署基线，明确未覆盖应用认证 smoke、真实资源/效果、模型、调度触发和浏览器。最新局部检查也不能替代一次全量验收。

### 当前证据怎么用

| 要判断什么 | 证据与适用范围 |
| --- | --- |
| 用户生命周期接到哪一层 | [本轮核对 §81](../history/delivery-history.md#811-工作副本与失败证据)：已有 9 个管理 HTTP 操作、9 组 Schema/example、改密防护与 bootstrap 审计；快照缺少新操作、Web 未接。users 单跑通过，但与 API 合跑在 conftest 导入处失败；不是整条链路验收 |
| 登录防护与客户端的此前证据 | [此前核对 §78](../history/delivery-history.md#782-工作副本核对与验证范围)：当时 Backend/契约 123 项、Web 430 项、62 个 LoginPage mock API 场景通过，OpenAPI 与 exporter 一致。该快照结论不覆盖本轮已有的新管理路由；实 DB、App 多页面会话与代理仍未验 |
| 会话协议与此前公开同步 | [此前核对 §75](../history/delivery-history.md#75-会话现状对齐与文档维护指南续整2026-09-09)：当时只同步 GET session 的描述并通过；不能覆盖后续新增的登录防护响应，不证明实 DB/浏览器会话/部署 |
| 生成界面的前置基础 | [此前核对 §74](../history/delivery-history.md#74-生成模块边界与开发阅读路径续整2026-09-09)：局部函数/模型结构、Manifest gate 与业务 module API 的离线回归；不证明 builder、CSP、Host 或回退可运行 |
| 旧认证行为与 Secret 边界 | [此前核对 §73](../history/delivery-history.md#73-认证凭据与安全开发入口文档续整2026-09-09)：当时的 GET 轮换已被后续源码替代，Secret 轮换恢复的验收限制仍保留；不将当时的 78 项回归套用到新版认证 |
| 当前预算内部协议 | [此前核对 §72](../history/delivery-history.md#72-预算账本边界与开发入口文档续整2026-09-09)：Backend 66 项离线回归。覆盖 DTO、repository/store 模拟与迁移结构，不证明真实 DB、计量来源或 Worker 接入 |
| 等待拒绝与终态接续 | [此前核对 §71](../history/delivery-history.md#71-等待事务对齐与任务导航文档续整2026-09-09)：Backend 93 项执行回归。mock transaction/client 不证明实 DB 阻塞、回滚、接管或进程停止；不覆盖后续源码和全量应用 |
| 公开边界与 Web 投影 | [此前核对 §70](../history/delivery-history.md#70-提交边界与开发阅读入口文档续整2026-09-09)：Backend 37 项公开边界、Web 13 项投影。当时 157 项执行回归不覆盖后续等待取消改动 |
| 最近一次完整本地基线 | [2026-09-08 §66](../history/delivery-history.md#66-r01-输入世代隔离与多根不变量收口2026-09-08)：Backend 1121 通过 / 20 跳过，Web 335 通过。不覆盖后续源码；实 DB skip 不是通过 |
| 最近记录的部署 | [2026-08-04 §49](../history/delivery-history.md#49-服务器部署基线验收2026-08-04)。不能把当前工作副本当成已部署版本 |
| 文档是否可维护、可浏览 | 本轮 [§81 接线与阅读检查](../history/delivery-history.md#813-文档验证与保留范围)，此前 [§80 账户设计](../history/delivery-history.md#802-文档验证与保留范围)、[§79 运维分层](../history/delivery-history.md#792-核对与验证范围)等记录分别保留。按各自范围阅读，不刷新应用全量或部署基线 |

回归清单和具体命令放在链接到的历史记录中；本表只帮助选择证据。后续发现旧结论变化时追加记录，不倒改当时的失败或跳过。

### 13.2 下一步与当前决策

| 顺序 | 工作 | 交付和门禁 |
| --- | --- | --- |
| 1 | 补齐运行约束差距 | 保持原请求确认、公开文档和主/子结果回归；继续[身份与凭据](#r05-领域与身份安全)、[停止分类与核对](../design/run-supervision.md#兼容与开发接续)、真实事务、[可信缓存与跨根总量](../design/resource-snapshots.md#已知差距与后续设计)、[Run 共享预算](../design/run-budgets.md)和[调度可靠性](../design/task-scheduling.md#可靠性修正要求待实现) |
| 2 | Task Flow 只读投影 | 先复用现有数据，不增加执行控制器或假造节点完成；见 §26 |
| 并行准备 | 真实资源与模型专项验收 | 有合法测试项目/账号后跑 smoke、HTTPS Git/SVN、CAS/forge、调度与浏览器；测试写入使用专用目标 |
| 后续 | Flow 契约与 Run 快照 | 版本化 Draft、冻结内容、事件关联和旧 Run 回退 |
| 条件满足后 | generated 模块 | 内部依赖镜像、构建网络边界、威胁模型和浏览器隔离验证 |

共享预算差距会影响执行成本，资源范围与内容版本差距会影响数据可见性和重试可复现性，故优先于继续扩大自主能力。文档修订不改变 Runtime，也不替这些缺口作通过结论。

### 13.3 全项目重构与缺失功能实施（2026-09-05 启动）

此登记保留全项目代码重构与缺失功能的既有范围，不表示本轮文档整理同时实施这些任务。R01 已补本地多根/文件故障与世代隔离，R02 已有未接入执行的内部账本，R07 已有停止分类、终态复查与等待/普通事件取消检查；接续从剩余不变量和真实环境验收开始，不重做已有组件。R01–R13 不因既有回归绿色就记为完成。子指令/输出与审计降级的新协议必须先满足共享预算和版本门禁，不以修正 v1 的异常路径为由直接扩展自主能力。

范围覆盖 Backend、Web、contracts、Skill 执行资产、脚本、构建/部署配置及对应测试。以下是全量工作入口，
不是把目标缩减为第一批缺口；每一领域仍需逐条核对设计中的字段、行为、不变量、失败路径和验收条件。
现行规范是依据，history 中已被替代的设计不重新引入；文档明确禁止或列为非目标的能力不被解释为待实现功能。

按职责跳转，不必横向阅读四列表格；每项依次列出状态、范围和验收。下列分组是导航，不改变 §13.2 的优先顺序。

- 运行基础：[R01 资源](#r01-资源冻结) · [R02 预算](#r02-run-统一预算) · [R05 身份](#r05-领域与身份安全) · [R07 Run](#r07-run-与审计) · [R08 效果](#r08-外部效果) · [R09 调度](#r09-调度)
- 产品能力：[R03 Flow](#r03-task-flow-完整链路) · [R04 生成模块](#r04-生成模块) · [R06 Skill](#r06-skill-生命周期) · [R10 Web](#r10-全部-web-页面)
- 交付验收：[R11 工程与运维](#r11-运维与工程工具) · [R12 业务质量](#r12-业务质量验收) · [R13 全量审计](#r13-全量契约与最终审计)

环境、账号、内部依赖镜像或真实业务数据不足时，在获准的代码实施中先推进独立可验证部分，不擅自采用公网依赖或放宽安全门禁。文档整理不运行部署或未知生产数据操作。

#### R01 资源冻结

- 状态：实施中。命名空间独占、多根/篡改/失效提交/接管复用已有回归；已有回执与监督接线保持通过。真实事务、迁移/历史恢复、完整执行链与外部资源仍待验。
- 范围：[资源快照](../design/resource-snapshots.md)与[创建](../design/run-creation.md)；documents / integrations / runs / agent、Preflight 与 Run 详情。不重做已有选择/清单入口；回执修改同步 DB/migration、物化器、Worker heartbeat/fencing、workspace Provider、测试与恢复说明。
- 验收：显式单份/集合/全集、创建时冻结、不可越权/漂移、重放与历史兼容。

#### R02 Run 统一预算

- 状态：实施中，尚未启用。内部 DTO、三表/0030、repository/store 已有代码；§72 的 66 项离线回归通过。普通创建、主 Executor、子 Provider、Worker startup 与受信计量/停止核对方尚未接入，原执行仍使用局部上限。
- 范围：[预算设计](../design/run-budgets.md#持久账本的当前载体)、子分析与 Runtime；agent / runs / worker / DB。复用[账本接续入口](../../PJM/backend/README.md#台帳の実装を引き継ぐ)，不重复建表，也不把内部保存版当作公开协议。
- 验收：计量来源/完整性与核对方授权、真实并发/提交未知恢复、创建重放、主子共同计费及跨 Segment/Attempt 接入；旧 Run、混合 Worker、回退与公开投影另行验收。停止键与内部 lease 不是外部证据或服务认证，fake 提交回归不是实 DB 证明。

#### R03 Task Flow 完整链路

- 状态：全部工作包待实现。
- 范围：[Task Flow](../design/task-flow.md)、Workspace、Interpreter；contracts / skills / runs / Web。
- 验收：预览、契约/Draft/diff/发布、不可变 Run 流程、事件关联、待办/证据联动、三语与历史回退。

#### R04 生成模块

- 状态：前置代码已只读核对，101 项 Backend 局部离线回归通过；仍无构建/投放/Host/回退。CSP 常量与目标不符，source 唯一约束不能表达仅依赖/工具链/策略更新，现有发布 helper 不检查产物和报告。没有实施本轮业务代码修正。
- 范围：[generated FrontendModule](../design/generated-modules.md)；modules / API / Web / builder / Compose。按[Backend 接续](../../PJM/backend/README.md#生成-module-の前置実装を読む)复用已有基础，补完整构建身份、提交/授权、Manifest/Host 契约与独立展示选择，不借业务 modules API 或 composition 切版处理界面。
- 验收：[场景矩阵](../design/generated-modules.md#从场景验收)覆盖威胁模型、实际依赖/网络隔离、构建与提交恢复、安全投放、Host 实例切换、历史/缓存/停用和回退。Preview 也执行生成代码；静态通过、CSP 字符串断言或文档浏览器通过均不能授权首次运行。

#### R05 领域与身份安全

- 状态：会话 v2/0031、三维防护与 LoginPage 的既有证据见 §78。[用户生命周期](../design/user-lifecycle.md#工作副本与公开入口)已有 API 装配、9 个 HTTP 操作、9 组 Schema/example、改密来源/账号防护、服务器 UUID 与 bootstrap 审计。users 单跑 24 项通过；API/auth/契约组合为 57 通过、1 个 OpenAPI 一致性失败。users 与 API 合跑另在裸 conftest 导入处中断；Web client/page 尚缺，不能记为完整交付。本轮只读核对，保留既有业务改动。
- 范围：[领域模型](../design/domain-model.md)、[认证](../design/authentication.md)、[用户管理](../design/user-lifecycle.md)、[登录防护](../design/login-protection.md)、[Secret 保存与轮换](../design/secret-storage.md)；auth / users / projects / documents / integrations / core / DB 与 Web。沿[认证接续入口](../../PJM/backend/README.md#認証と-secret-の境界を追う)和[用户管理接线](../../PJM/backend/README.md#ユーザー管理の接続を引き継ぐ)复用已有组件，不以 ProjectMember 管理替代账户管理。
- 验收：保留[登录防护](../design/login-protection.md#开发接续与验收)的拒绝停止点、成功不清零、HTTP/三语，以及真实表单重复 submit、卸载/晚到结果、语言/键盘/窄屏回归。用户管理按[事务/公开链路验收](../design/user-lifecycle.md#开发接续与验收)补齐最后 ADMIN、并发登录/撤销、0032 与请求未知；接续 App 初次会话错误、多 tab、代理与 Redis 恢复。[会话场景](../design/authentication.md#验收从场景出发)的实 DB 双行锁、0031 与认证/业务提交边界、Secret 的[恢复场景](../design/secret-storage.md#开发接续与验收)另验。

实施按[用户管理接续步骤](../design/user-lifecycle.md#开发接续与验收)，先恢复合跑基线，再接齐契约/Web 与真实环境验收，不重建已有 API/Schema。

#### R06 Skill 生命周期

- 状态：发布/启停已只读核对；同版重新启用/审计与回滚待实现，组合尚无跨 Skill 规则求解；其余仍需逐条核对。
- 范围：[Skill 契约](../design/skill-contract.md)与[解释实现](../design/skill-interpretation.md)；skills / compositions / contracts / Web / system Skill。
- 验收：导入到解释、版本发布与项目启用、来源/identity、动态契约、兼容与错误恢复。

#### R07 Run 与审计

- 状态：主/子结果、首事件前停止、无意图 interrupted、终态取消复查与 PRIMARY 查询有局部通过。等待/普通事件现已在序号分配与新增记录前拒绝持久取消；§71 的 93 项回归包含 service 异常传播、旧轮询和终态前失去 lease，不再把这条检查列为待开发。
- 范围：[Runtime](../design/agent-runtime.md)与[执行监督](../design/run-supervision.md)；runs / worker / agent / events / evidence / evaluations / storage。等待拒绝和终态保存是两个事务，不能视为原子停止。
- 验收：继续真实 DB 锁竞争、回滚后崩溃/接管、commit 结果不明、持久停止核对、子审计恢复与进程退出；Segment/Attempt/Session、Outbox/lease、恢复/取消/等待期限、结果与 SSE 重放须有状态联验。局部 mock/fake 通过不覆盖这些条件。

#### R08 外部效果

- 状态：已核对内容比较不能证明原执行、SVN 基线/回读未固定、无独立阶段回执/贯穿监督；审批卡片换键与旧 branch-only Schema 待同步。
- 范围：[受控写入](../design/repository-effects.md#可靠性修正要求)；effects / integrations / repository / Redmine / Web。
- 验收：提案、精确批准/预授权、CAS/原执行身份、阶段回执、Effect lease、read-back、Git/SVN/PR 部分失败恢复。局部通过不替代修正或真实验收。

#### R09 调度

- 状态：已核对摘要/先读后写/首 100 条的限度；恢复协议、并发/计数/迟到、管理/编辑入口和时区修正待实现。
- 范围：[TaskSchedule](../design/task-scheduling.md)；schedules / worker / API / Web。
- 验收：时间/预览、同 Schedule 重叠、原子配置更新、持久在途/执行权、幂等计数与历史兼容。局部通过不证明真实事务。

#### R10 全部 Web 页面

- 状态：原请求、文档选择/详情与调度输入有局部 mock 浏览器证据；§78 已核对真实 LoginPage + mock API 的 62 个场景与 430 项 Web 回归，本轮不重记这些应用结果。Schedule 首批与 TaskCatalog 的漏显风险、App 跨页面会话及其余完整流程仍待逐项核对与重构。
- 范围：[Workspace](../design/workspace.md)与[产品概览](../overview/product.md)；pages / components / API / hooks / lib / styles / i18n。
- 验收：页面职责、服务端筛选、并发请求清理、三语/键盘/窄屏、真实用户流程。

#### R11 运维与工程工具

- 状态：已定位 ENV_FILE/容器 .env 来源不统一、deploy 不分阶段放行、0027 downgrade 删除审计会话的风险。本轮进一步核对 dispatch=false 只限制新 Outbox 投递，不能阻止既有 job、Schedule、recovery 或解释任务；操作说明已分层，统一停写/发布控制、配置修正与真实恢复仍待完成。既有业务浏览器 Ruff 问题见 §13.11。
- 范围：[本地开发](../development/local-development.md)、[发布迁移](../operations/deployment.md)、[备份恢复](../operations/backup-recovery.md)和[症状排障](../operations/runbook.md)；ops / migrations / scripts / images / Compose / Dockerfile。
- 验收：锁定依赖、静态检查、启动/迁移、backup/restore/rollback、KEK 保留、清理策略与 smoke；按[发布门禁](../operations/deployment.md#后续开发约束与验收)覆盖多实例、旧队列、cron 和失败不放行，不能只验关闭 dispatch 后不 enqueue。

#### R12 业务质量验收

- 状态：待构建/执行可获得的验收，其余保留外部条件。
- 范围：[JAF 迁移与运行](../acceptance/jaf-quality.md)、[Benchmark 样本与评分](../acceptance/jaf-benchmark.md)；通用运行链与业务 Skill/评价数据。§78 分清了执行/评价责任；本轮未运行模型或准备真实 Gold。
- 验收：文档定义的 case/指标/人工评价、Gold 隔离、规则保真。不把 fixture 结果冒充真实模型质量。

#### R13 全量契约与最终审计

- 状态：按[证据范围表](#当前证据怎么用)区分局部、全量和部署；实 DB 历史 skip、业务浏览器/外部/部署与其他领域缺口仍在。
- 范围：全部现行设计、[AGENTS](../../PJM/AGENTS.md)、[contracts](../../PJM/contracts/README.md)和工程入口。
- 验收：逐需求证据、Schema/example/OpenAPI 同步、Backend/Web/DB/浏览器/部署范围匹配的测试。测试数字不能证明全需求完成。

下面的有日期章节只保留证据入口，历史细节不在计划页重复维护，后续代码结果单独记录。

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

调度的实际重叠范围、先读后写、摘要/账本和前 100 条管理可见性核对，以及当轮 65 项 Backend / 10 项 Web 局部通过见[交付记录 §65](../history/delivery-history.md#65-调度事实并发与管理文档续整2026-09-08)。该轮只整理文档，R09 的原子更新、持久恢复和管理界面仍未实现；不倒改其当时的验证范围。

### 13.19 R01 输入世代隔离与全量本地回归（2026-09-08）

输入命名空间独占、多根计量/篡改/接管回归和当时的全量本地验证见[交付记录 §66](../history/delivery-history.md#66-r01-输入世代隔离与多根不变量收口2026-09-08)。本锚点保留；与当前工作副本的关系只在[证据范围表](#当前证据怎么用)维护，不继续复制实施日志。

### 13.20 预算计量与子分析文档核对（2026-09-08）

预算的计量/预留/结算设计、当时复现的子失败误报完成与 ID 省略冲突，以及文档阅读验证见[交付记录 §67](../history/delivery-history.md#67-预算计量子分析与开发文档续整2026-09-08)。该轮只整理文档；后续工作副本已有代码变化，以 §13.21 接续，不倒改历史记录，也不把当时问题继续列为原样未修复。

### 13.21 子分析交接与验证状态文档核对（2026-09-08）

子分析指令/结果、Session 与 Tool 独立提交的设计整理，以及当时 **79 项通过、3 项失败**的证据见[交付记录 §68](../history/delivery-history.md#68-子分析交接与验证状态文档续整2026-09-08)。旧 validator fixture 失败已在 §13.22 的核对中不再复现；保留原证据，不从旧记录判断当前代码。

### 13.22 执行监督与停止文档核对（2026-09-09）

监督设计独立成页、当时主 interrupted 映射的差距、124 项局部通过与阅读检查见[交付记录 §69](../history/delivery-history.md#69-执行监督与停止文档续整2026-09-09)。之后已有停止分类和终态事务改动，以 §13.23 接续，不把旧原因映射继续列为原样未修复。

### 13.23 提交边界与开发阅读入口核对（2026-09-09）

当时取消分类、终态复查、PRIMARY 查询的只读回归，以及等待提交的独立风险见[交付记录 §70](../history/delivery-history.md#70-提交边界与开发阅读入口文档续整2026-09-09)。该轮只同步文档，没有修复等待竞争或新增协议；后续工作副本的等待检查见 §13.24，不倒改 §70 的历史事实。

### 13.24 等待事务与任务导航核对（2026-09-09）

等待/普通事件取消检查、回滚后终态接续、用量投影的只读核对，以及 R01–R13 独立入口的整理见[交付记录 §71](../history/delivery-history.md#71-等待事务对齐与任务导航文档续整2026-09-09)。该轮未修改业务代码；真实事务和停止验收继续按 [R07](#r07-run-与审计)接续。

### 13.25 预算账本与执行接入核对（2026-09-09）

已有内部账本、尚未接入的执行/可信核对边界，以及设计到代码、迁移与验收的阅读检查见[交付记录 §72](../history/delivery-history.md#72-预算账本边界与开发入口文档续整2026-09-09)。该轮只整理文档并做离线验证；R02 的状态不由方法或迁移文件存在推导为完成。

### 13.26 认证、凭据与安全开发入口核对（2026-09-09）

当时的登录/Secret 分离、旧 CSRF/时效差距与直接加密/恢复边界见[交付记录 §73](../history/delivery-history.md#73-认证凭据与安全开发入口文档续整2026-09-09)。后续源码已有 v2，当前核对见[§75](../history/delivery-history.md#75-会话现状对齐与文档维护指南续整2026-09-09)与 R05；保留旧证据，不再从本历史入口推导当前仍会轮换。

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

见[Secret 保存与轮换](../design/secret-storage.md)与[Runbook](../operations/runbook.md#88-managed-secret-の-kek-運用)。旧认证 §7 保留兼容入口，当前加密层级与版本含义以新正本为准。

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

本节保留旧入口，不另存实现进度。dispatch、终端/Session/Gateway 的现状见[子分析当前边界](../design/subagents.md#当前返回值的可信边界)；接续范围与证据分别查 [R02 预算](#r02-run-统一预算)、[R07 执行与审计](#r07-run-与审计)。

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
