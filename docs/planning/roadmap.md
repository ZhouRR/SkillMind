# ProjectMind 实施计划

本页只维护当前未完成范围与着手顺序；功能规则见[设计索引](../design/README.md)，代码入口见[变更指南](../development/change-guide.md)。
以下按当前代码与验证范围登记，不是全项目完成或部署证明。

## 当前执行状态

核心 Skill/Run/资源/身份/外部效果链已有代码；共享预算、完整管理页面、Task Flow、生成界面及真实环境验收尚未完成。
保留 R01–R13 作为开发任务 ID，不再按历次文档整理轮次登记进度。

### 完成状态的判定

- 实现完成：代码、契约、消费者和相应回归接齐。
- 部署完成：目标镜像、配置、迁移、健康检查与放行均确认。
- 专项验收：具体场景有输入、环境、可观察结果及未覆盖范围。
- 设计完成：规则可评审，不表示 API 或页面已经可用。

### 当前证据怎么用

局部 mock/fake 测试、真实 DB、浏览器、模型和部署各自证明不同范围；skip、测试文件存在或旧通过次数不算当前验收。
账户 API/Schema/OpenAPI/client 与 App 页面已接通；fake API 和 mock 浏览器不证明真实账户事务、会话部署或全站隔离。共享预算未接执行等缺口继续保留，文档构建不消除它们。

### 下一步与当前决策

1. 先收口身份/资源边界、Run 停止与事务、共享预算和调度可靠性，再扩大自主执行能力。
2. 保持账户链路回归，完善普通答复、评价、项目/文档管理的未知结果与上下文隔离。
3. Task Flow 先做只读投影，再接版本契约、Run 冻结和事件关联；不另建执行器。
4. 生成界面先满足威胁模型、构建身份、隔离与安全投放条件，静态 helper 通过不足以放行。
5. 有获准的隔离环境后补真实资源、DB、代理、模型、外部写入与恢复验收；不为验证临时放宽权限。

### 开发任务

范围覆盖 Backend、Web、contracts、Skill 执行资产、工具、构建/部署配置与对应测试。
已有组件应接续而非重建；明确列为非目标的能力不当作缺失功能。

运行基础：[R01](#r01-资源冻结) · [R02](#r02-run-统一预算) · [R05](#r05-领域与身份安全) · [R07](#r07-run-与审计) · [R08](#r08-外部效果) · [R09](#r09-调度)
产品能力：[R03](#r03-task-flow-完整链路) · [R04](#r04-生成模块) · [R06](#r06-skill-生命周期) · [R10](#r10-全部-web-页面)
交付验收：[R11](#r11-运维与工程工具) · [R12](#r12-业务质量验收) · [R13](#r13-全量契约与最终审计)

#### R01 资源冻结

- 状态：选择/清单、输入回执、命名空间独占与跨根限额已有实现；文档有界接收、原授权/引用与0037存储归属已接。0038 在 PUT 前保存原意图/占用并同事务发布回执；0039 为新旧删除保存独立清理记录，旧占用转入、新意图不双计费。0042 已接原作者显式停止发布和独立回执，原回执与占用保持；原 key 可查，文档/意图/清理均阻止整项目清空。精确清理/结算、旧资产归属、服务端 namespace 身份及真实事务/恢复未完成。
- 范围：[文档资产](../design/document-lifecycle.md)、[资源快照](../design/resource-snapshots.md)、[创建](../design/run-creation.md)；documents/integrations/runs/agent、Preflight、Run 详情。
- 验收：保持原上传查询、关闭/发布竞争、提交未知/取消、同键/同名和占用守恒回归；停止发布不代替远端写入停止证明。落实[存储端封闭前提](../design/document-lifecycle.md#持久清理仍待补齐)后接精确清理/结算及旧资产/整项目/恢复。当前 MinIO 的条件策略与仲裁拒绝不足以直接放行，不以客户端 header、DB lease 或长期保留占用代替完整功能。补0042迁移/恢复、授权/引用/配额的真实竞争、大规模扫描和存储故障验收，关联 R05。

#### R02 Run 统一预算

- 状态：DTO、账本/0030、原调用绑定与观察/0035、repository/store 和 Engine 内部桥接已有代码；0043 将首次绑定与启动所有权一起保存，新协调器不能凭 RESERVED/原描述重启未知调用，旧绑定不补造 owner。观察冲突先持久化，不将 float 或终端事件当精确计量/停止。已验证 profile、受信停止/未知核对、创建、主子执行与 Worker startup 仍待接齐，真实事务/迁移未验。
- 范围：[预算](../design/run-budgets.md)、Runtime 与子分析；复用账本，不重建内部保存版或直接当作公开协议。
- 验收：可信计量、主子共同预约/结算、真实并发与提交未知、跨 Segment/Attempt、旧 Run/混合 Worker/回退和公开投影。

#### R03 Task Flow 完整链路

- 状态：任务中心已接精确版本的只读预览 API/契约/页面，Task 与 Skill 共享声明、原来源核验与当前全蓝图 readiness 分区；缺失和损坏不混同。参数/结果契约已接原数值无损读取、类型/判重/上下限校验及展示，保留原协议与 hash。Skill 页默认预览、版本化 Flow/DRAFT/diff/发布、Run 冻结与事件/待办/证据联动仍未完成，没有新增执行器。
- 范围：[Task Flow](../design/task-flow.md)、Workspace、Interpreter；contracts/skills/runs/Web。
- 验收：保持原数值类型、相邻大整数/浮点、范围与嵌套展示的真实 serializer/浏览器回归，不以缩窄 Blueprint 数值范围代替兼容。接续组织草稿/版本预览，与 Project readiness、身份分离；复用来源门禁并关联 R06。继续 Draft/diff/发布、不可变 Run 流程、事件/待办/证据关联、三语与无 Flow 数据回退；响应总量/并发内存预算另定完整协议。

#### R04 生成模块

- 状态：模型/迁移和静态前置已有代码，构建、投放、Host、回退链未完成；CSP、构建身份与发布证明仍有差距。
- 范围：[生成界面](../design/generated-modules.md)；modules/API/Web/builder/Compose，与 SkillComposition 管理分开。
- 验收：依赖来源、隔离、产物和报告、提交恢复、安全投放、旧 Host 消息、独立显示回退。Preview 同样须满足首次执行门禁。

#### R05 领域与身份安全

- 状态：会话 v2/0031、登录防护、账户与项目精确上下文已有实现；成员管理有业务事务重新认证、0033 审计与 ADMIN 页面。项目 CRUD 已接原会话复核、0034 版本、冲突比较与未知核对；普通 Run 创建/原请求确认已接原会话、当前成员与归档的事务内复核，新建另固定当前精确版本/启用关系，重放不刷新快照。成员审计、Schedule 和上传意图阻止整项目删除。真实事务、迁移和 HTTPS 仍待验。
- 范围：[领域](../design/domain-model.md)、[项目](../design/project-lifecycle.md)、[认证](../design/authentication.md)、[登录防护](../design/login-protection.md)、[用户](../design/user-lifecycle.md)、[Secret](../design/secret-storage.md)及相应 Web。
- 验收：最后 ADMIN、改密/撤销及成员/禁用竞争、0031–0034、真实 Redis/DB/HTTPS、多页面与未知提交；项目 CAS/成员审计真实回滚、独立项目审计、归档与创建竞争、完整删除引用和 blob 清理。TaskSchedule/成员审计的 RESTRICT 不能被“无 Run”替代。

#### R06 Skill 生命周期

- 状态：导入/解释/发布/启停已有实现；DRAFT、首次发布和只读预览共用原身份、全部 Task 与来源门禁。版本六操作及组合三写已接原会话/当前 ADMIN 的业务事务复核，原审计值不重写；版本删除保护纳入调度、occurrence 和生成模块引用，新 Run/调度/组合保存共用当前可用性检查。组合限定同组织，保留共享更新及当前项目解绑。候选持久绑定、导入/解释资格、共享组合采用/版本/回执与无关联资产管理、真实事务、同版重新启用审计及回滚仍待补齐；跨 Skill 规则组合尚未实现。
- 范围：[Skill 契约](../design/skill-contract.md)、[解释与发布](../design/skill-interpretation.md)；skills/compositions/contracts/Web/system Skill。
- 验收：保持 source/identity、动态契约、六操作原会话/锁等待/失败及全部引用回归；补撤销/登出、双 ADMIN 同版操作、启停/废弃与新建/保存、引用新增/删除的真实竞争和完整回滚。继续候选持久绑定、导入/解释资格、可审计重新启用与兼容恢复；模型解释质量另验。

#### R07 Run 与审计

- 状态：主子结果、停止分类、终态/等待取消检查与普通答复原请求确认已接。Gateway 使用原 Worker 私有执行权与一次许可，未决不重跑。workspace.write/v2 已接不可变 UTF-8 Artifact/0040、存量限制、授权读取与主子引用核验；v1/历史不回填。评价已接0041原键/内容绑定、同事务资格复核、只读确认与分页；真实事务/迁移恢复、撤权竞争、持久停止与Session审计仍待补齐。
- 范围：[Runtime](../design/agent-runtime.md)、[普通答复](../design/user-interactions.md)、[结果](../design/results-evaluation.md)、[监督](../design/run-supervision.md)；runs/worker/agent/events/evidence/evaluations/storage。
- 验收：保持答复/批准分路、原 actor/Tool 重放、Artifact 原字节/跨 Run/未知提交与主子核验；补真实锁/回滚/接管、0040/0041 和备份恢复、评价同键/撤权竞争、Session 审计、进程退出和 SSE。接续效果旧版本核验；平台记录匹配不代替 R08 远端验收。

#### R08 外部效果

- 状态：Git/SVN 与 Proposal/read-back 已有实现；内容相同不证明原执行，SVN 固定基线、阶段回执、贯穿监督、批准重发及旧 Schema 待补。
- 范围：[受控写入](../design/repository-effects.md)；effects/integrations/repository/Provider/Web。
- 验收：精确批准/预授权、CAS、原执行身份、Effect lease、阶段回执、read-back、Git/SVN/PR 部分失败与真实外部恢复。

#### R09 调度

- 状态：ONCE/CRON、0036 持久 occurrence、原键恢复、普通创建事务内关联/结算与锁内配置版本已接；时间/DST/预览、项目独立管理、版本冲突/未知核对与单 PENDING 只读投影已接。管理三写复核原会话、成员与归档；创建/编辑/恢复 ACTIVE 另固定当前版本及启用关系，新 Run 同样复验，暂停/归档和原 Run 确认不要求任务重新可用。真实撤权/多 Worker 事务、历史调度迁移、人工处理和迟到策略仍待补。
- 范围：[TaskSchedule](../design/task-scheduling.md)；schedules/worker/API/Web。
- 验收：精确时间、同 Schedule 重叠、原子配置、在途执行权、幂等计数、失效来源拒绝与兼容；不扩展成全局精确一次或停机补跑。

#### R10 全部 Web 页面

- 状态：账户、成员、项目 CRUD、普通答复共用防重/期限/旧响应隔离。文档原上传/删除核对、未知暂停与静态预览已接；App 保留精确项目边界。结果保留保存时 v1/v2 范围，模型效果与平台审计分开；附件按发布索引核对下载字节。评价已接多修订、原回执确认/显式重发与游标历史；module、批准的完整 mutation/未知隔离仍待收口，真实会话/存储与跨刷新上传批次恢复尚未证明。
- 范围：[Workspace](../design/workspace.md)与[文档管理](../design/document-lifecycle.md)；pages/components/API/hooks/lib/styles/i18n。
- 验收：保持答复、评价及上传/删除的切换、同步防重、晚到响应和原请求核对回归；补批准、全站服务端筛选/分页、三语、键盘、窄屏与真实用户流程。评价的同键/提交未知与历史排序、资产管理均须真实授权/存储竞争验收，mock 不替代实环境。

#### R11 运维与工程工具

- 状态：ENV_FILE 已统一到公共 Compose 入口；发布有独立 load/migrate/api/worker、daemon/project/image 与有效配置复核，0027 不再降级删子会话审计。脚本/fake 与迁移谓词回归已接；统一全实例停写/清理、实际 Docker/Make/PowerShell 与真实恢复仍待验。
- 范围：[开发验证](../development/local-development.md)、[发布](../operations/deployment.md)、[恢复](../operations/backup-recovery.md)、[Runbook](../operations/runbook.md)；ops/migrations/scripts/images/Compose/Dockerfile。
- 验收：锁定依赖、静态检查、迁移/回退、完整恢复点与 KEK、受引用资产清理、smoke；停止全部实例、旧队列、cron/recovery/解释写入者后再操作，dispatch=false 不构成停写证明。

#### R12 业务质量验收

- 状态：通用模型与结果质量需要获准、隔离的真实场景验证。
- 范围：Skill 解释、通用运行链、[结果与人工评价](../design/results-evaluation.md)。
- 验收：规则保真、证据可追溯、输出可用与人工判定；输入与评价答案隔离，fixture 成功不当作实模型质量结论。

#### R13 全量契约与最终审计

- 状态：全项目尚未完成，局部通过不关闭上述缺口。
- 范围：全部现行设计、[AGENTS](../../PJM/AGENTS.md)、[契约](../../PJM/README.md#contracts)和工程入口。
- 验收：逐需求对应代码/契约/消费者/测试；Schema/example/OpenAPI 同步，真实 DB、浏览器、模型、外部与部署各按适用范围举证。
