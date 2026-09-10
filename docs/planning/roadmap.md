# Skillmind 实施计划

本页维护当前基础、缺口和顺序；规则见[设计索引](../design/README.md)，代码入口见[变更指南](../development/change-guide.md)。不记录历次整理或迁移流水。

## 当前执行状态

核心 Skill/Run/资源/身份/外部效果链已有代码；共享预算、完整管理页面、Task Flow、生成界面及真实环境验收未完成。R01–R13 是稳定任务 ID。

### 完成状态的判定

实现须接齐代码、契约、消费者和回归；部署须确认目标镜像、配置、迁移、健康检查与放行。设计可评审不等于功能可用。

### 当前证据怎么用

验收须记录场景、环境、结果及未覆盖范围。mock/fake、真实 DB、浏览器、模型、部署分别举证；skip、旧通过次数和文档构建不关闭功能缺口。

### 下一步与当前决策

1. 先收口身份/资源边界、Run 停止、共享预算和调度可靠性，再扩大自主执行。
2. 完善管理页面的未知结果与上下文隔离，保持账户、答复和评价回归。
3. Task Flow 从只读投影接版本、冻结与事件；生成界面先满足威胁模型和隔离门禁。
4. 获准隔离环境后补真实 DB/资源/模型/外部写入/恢复验收，不临时放宽权限。

### 开发任务

覆盖 Backend、Web、contracts、Skill 执行资产、工程配置和测试；接续已有组件，不将非目标列为缺失功能。

运行基础：[R01](#r01-资源冻结) · [R02](#r02-run-统一预算) · [R05](#r05-领域与身份安全) · [R07](#r07-run-与审计) · [R08](#r08-外部效果) · [R09](#r09-调度)
产品能力：[R03](#r03-task-flow-完整链路) · [R04](#r04-生成模块) · [R06](#r06-skill-生命周期) · [R10](#r10-全部-web-页面)
交付验收：[R11](#r11-运维与工程工具) · [R12](#r12-业务质量验收) · [R13](#r13-全量契约与最终审计)

#### R01 资源冻结

- 基础：[资源冻结](../design/resource-snapshots.md)、输入回执/世代独占与跨根限额已接；[文档](../design/document-lifecycle.md)具备有界接收、原授权、存储归属、PUT 前意图/占用、原回执查询及显式停止发布。新旧删除保留清理记录，不双计费；相关记录阻止整项目清空。
- 缺口：精确清理/结算、旧资产迁移、服务端 namespace 身份及真实事务/恢复。
- 门禁：先落实[存储端封闭前提](../design/document-lifecycle.md#持久清理仍待补齐)；现有 MinIO 策略、客户端 header、DB lease 或停止发布均不证明远端停止。验关闭/发布、未知/取消、同键/同名、占用守恒、授权/引用/配额竞争和存储故障，关联 R05。

#### R02 Run 统一预算

- 基础：[预算](../design/run-budgets.md)账本、原调用绑定/观察与 Engine 桥接已有代码；首次绑定保存启动所有权，冲突观察先持久化，旧绑定不补造 owner。
- 缺口：已验证 profile、受信停止/未知核对、创建、主子执行与 Worker startup 接线及真实事务/迁移。
- 门禁：新协调器不得凭 RESERVED 重启未知调用；float/终端事件不证明精确计量或停止。验共同预约/结算、并发/未知、跨 Segment/Attempt、旧 Run/混合 Worker/回退和公开投影。

#### R03 Task Flow 完整链路

- 基础：[只读 Task 预览](../design/task-flow.md)已接精确版本、原来源核验、声明/readiness 分区及数值无损读取/校验/展示；缺失与损坏分开。
- 缺口：Skill 默认预览、版本化 Flow/DRAFT/diff/发布、Run 冻结及事件/待办/证据联动；响应总量/并发内存协议。
- 门禁：不增执行器或缩窄旧数值范围；保持原协议/hash，验大整数/浮点/嵌套 serializer 与浏览器。复用 R06 身份/来源门禁，验三语、历史无 Flow 回退及不可变 Run。

#### R04 生成模块

- 基础：[生成界面](../design/generated-modules.md)版本模型和静态前置已有代码，与 SkillComposition 管理分开。
- 缺口：完整构建身份、builder、CSP/安全投放、Host、发布和展示回退。
- 门禁：Preview 也须通过首次执行审查；验依赖/隔离、产物/报告、未知恢复、旧消息拒绝与独立展示回退。

#### R05 领域与身份安全

- 基础：[认证](../design/authentication.md)/[登录防护](../design/login-protection.md)、[账户](../design/user-lifecycle.md)与[项目](../design/project-lifecycle.md)页面已接。成员、项目 CRUD 和普通 Run 创建/确认复核原会话与当前权限；项目有版本冲突/未知核对，新建固定精确启用版本，重放不刷新快照。
- 缺口：独立项目审计、完整删除/blob 清理与真实事务、迁移、Redis/HTTPS 验收；[Secret](../design/secret-storage.md)恢复边界一并核验。
- 门禁：验最后 ADMIN、改密/撤销/成员/禁用、归档/创建竞争及真实回滚；成员审计、Schedule、文档/上传记录等引用不能用“无 Run”替代。

#### R06 Skill 生命周期

- 基础：[导入、版本及组合写入](../design/skill-contract.md)已接原会话/ADMIN 复核；上传有界接收、PUT 前后短事务复核，旧来源不重写。DRAFT/首次发布/预览共用身份、全部 Task/来源门禁，新建/调度/组合共用可用性检查，删除检查全部引用。
- 缺口：[异步解释](../design/skill-interpretation.md)原请求/Worker/SSE 授权、候选绑定、导入持久回执/存储归属、共享组合管理、同版重新启用审计、跨 Skill 规则组合及真实事务/回滚。
- 门禁：优先接持久原请求、一次模型启动、未知确认及 SSE 组织门禁，内容 key/终态唯一键不替代授权。保持接收/身份/契约/锁等待/引用回归，补撤销、双 ADMIN、启停/废弃竞争及导入/组合恢复；模型质量另验。

#### R07 Run 与审计

- 基础：[Runtime](../design/agent-runtime.md)/[监督](../design/run-supervision.md)已接主子结果、停止分类、取消检查与 Gateway 原 Worker 一次许可；[答复](../design/user-interactions.md)有原请求确认，[结果](../design/results-evaluation.md)有不可变 Artifact v2、授权/引用核验及评价原键/资格复核/分页。
- 缺口：持久停止、Session 审计、撤权竞争、真实锁/回滚/接管及迁移/备份恢复。
- 门禁：未决 Tool 不重跑，v1/历史不回填；验答复/批准分路、原 actor/Tool 重放、Artifact 原字节/跨 Run、评价同键/未知、进程退出与 SSE。平台匹配不代替 R08 远端验收。

#### R08 外部效果

- 基础：[受控写入](../design/repository-effects.md)已接 Git/SVN、Proposal 与 read-back。
- 缺口：SVN 固定基线、阶段回执、贯穿监督、批准重发及旧 Schema。
- 门禁：内容相同不证明原执行；验精确批准/预授权、CAS、原身份/lease、阶段回执、部分失败与真实外部恢复。

#### R09 调度

- 基础：[调度](../design/task-scheduling.md)已接 ONCE/CRON、持久 occurrence/原键恢复、创建事务内关联/结算、配置版本、独立管理与在途投影；管理复核原会话，ACTIVE 配置与新 Run 固定当前启用版本。
- 缺口：真实撤权/多 Worker 事务、历史迁移、人工处理及迟到策略。
- 门禁：暂停/归档和原 Run 确认不要求任务重新可用；验时间/DST、同 Schedule 重叠、原子配置、在途权、幂等计数、失效来源与兼容，不承诺全局精确一次或停机补跑。

#### R10 全部 Web 页面

- 基础：[Workspace](../design/workspace.md)已有精确项目边界及账户/成员/项目/答复共享请求隔离；[文档](../design/document-lifecycle.md)支持原上传/删除核对与静态预览。结果区分保存时 v1/v2 与模型/平台事实，附件校验字节，评价支持多修订/原回执/分页。
- 缺口：module、批准的完整 mutation/未知隔离，全站服务端筛选/分页、真实会话/存储及跨刷新上传批次恢复。
- 门禁：验防重、期限、切换/晚到、原请求核对、历史排序及三语/键盘/窄屏；真实授权和存储竞争不由 mock 替代。

#### R11 运维与工程工具

- 基础：[发布](../operations/deployment.md)统一配置入口与分阶段 load/migrate/api/worker，核对 daemon/project/image/有效配置；迁移拒绝有损审计回退，脚本/fake 回归已接。
- 缺口：统一全实例停写/清理，实际 Docker/Make/PowerShell 与[真实恢复](../operations/backup-recovery.md)。
- 门禁：验锁定依赖、迁移/回退、完整恢复点/KEK、引用清理与[smoke](../operations/runbook.md#通常-smoke)；操作前核清全部实例、旧队列、cron/recovery/解释写入者，dispatch=false 不是停写证明。

#### R12 业务质量验收

- 缺口：Skill 解释、通用运行链及[结果评价](../design/results-evaluation.md)的真实质量验收。
- 门禁：获准隔离场景验证规则保真、证据、输出及人工判定；输入与评价答案隔离，fixture 不证明模型质量。

#### R13 全量契约与最终审计

- 缺口：全项目最终审计；局部通过不关闭其他任务。
- 门禁：逐需求对应代码/[契约](../../SKM/README.md#contracts)/消费者/测试，按[AGENTS](../../SKM/AGENTS.md)同步 Schema/example/OpenAPI；真实 DB、浏览器、模型、外部与部署分别举证。
