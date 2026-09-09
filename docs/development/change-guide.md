# 从设计到代码的变更指南

## 先确定改的是哪条链路

不确定设计归属时，先看[设计责任索引](../design/README.md)。本页只把选定的规则连接到代码与验证，不维护另一份领域进度表。

按[运行与数据](#运行与数据)、[Skill 与界面](#skill-与界面)、[身份与交付](#身份与交付)选择一条路径。表格只给第一跳，逐文件接线由代码 README 负责；不要为开始一次改动先横向读完整张项目清单。

### 运行与数据

| 变更 | 设计 → 实现与验证 |
| --- | --- |
| 资源种类、Provider、scope | [资源快照](../design/resource-snapshots.md) → [Backend 变更入口](../../PJM/backend/README.md#一つの変更を追う)，核对绑定与 Provider 回归 |
| 文档上传、下载、目录与清理 | [文档生命周期](../design/document-lifecycle.md) → [保存接线](../../PJM/backend/README.md#project-文書の保存と清理を追う) / [管理界面](../../PJM/web/README.md#project-文書の管理を追う)，核对跨存储提交、并发配额、引用和未知结果；不与 Run 冻结混为一层 |
| 输入准备与可信缓存 | [准备协议](../design/resource-snapshots.md#输入准备与可信缓存) → [输入接线](../../PJM/backend/README.md#入力準備の接続を引き継ぐ)，追踪 DB/store、物化、Worker 与实际读取 |
| 创建、幂等与重复提交 | [Run 创建](../design/run-creation.md) → [Backend 入口](../../PJM/backend/README.md#一つの変更を追う) / [Web 原请求状态](../../PJM/web/src/hooks/useRunSubmission.ts)，区分原要求与新执行 |
| Run 状态、等待与恢复 | [领域模型](../design/domain-model.md) / [Runtime](../design/agent-runtime.md) → [runs](../../PJM/backend/src/projectmind/runs/) 与[有状态回归](../../PJM/backend/tests/runs/)，同步 DB、事件和 Web |
| 用户回答、过期与原答复确认 | [普通交互](../design/user-interactions.md) → [Backend 接线](../../PJM/backend/README.md#通常回答と期限処理を追う) / [Web 状态](../design/workspace.md#普通答复与续行状态)，区分普通答复、批准和评价；410 不等于无提交 |
| 结果校验、证据与人工修订 | [结果设计](../design/results-evaluation.md) → [Backend 接线](../../PJM/backend/README.md#結果検証と人工評価を追う) / [Web 接线](../../PJM/web/README.md#結果と人工評価を接続する)，检查全部引用位置、原值根与评价的独立提交 |
| timeout、取消与启动 | [监督与停止](../design/run-supervision.md) → [监督接线](../../PJM/backend/README.md#実行の取消と停止を追う)，按矩阵分开原因、终态、清理与未知消费 |
| 预算与多 Agent | [预算](../design/run-budgets.md) / [子分析](../design/subagents.md) → [主子接线](../../PJM/backend/README.md#予算と子分析の接続を追う)，先核对计量与账本，再验证输出/审计 |
| 外部效果与审批恢复 | [受控写入](../design/repository-effects.md) → [Effect 接线](../../PJM/backend/README.md#承認から外部変更まで追う) / [审批界面](../design/workspace.md#审批请求与执行结果)，分别验决定、远端与回执 |
| 调度 | [认领与恢复](../design/task-scheduling.md#认领记录与恢复权限) → [Schedule 接线](../../PJM/backend/README.md#schedule-の認領と回写を追う)，再到契约、Web 管理与分页验证 |

### Skill 与界面

| 变更 | 设计 → 实现与验证 |
| --- | --- |
| 解释、发布与项目启停 | [阶段判断](../design/skill-contract.md#发布与就绪的判断顺序) → [候选到任务](../design/skill-interpretation.md#从候选到项目任务的接线) → [Backend 接线](../../PJM/backend/README.md#skill-の公開と-project-可視性を追う) |
| Web 页面与流程图 | [Workspace](../design/workspace.md) / [Task Flow](../design/task-flow.md) → [画面入口](../../PJM/web/README.md#画面から実装へ進む)，同步 client、纯投影、三语与浏览器场景 |
| 生成界面 | [构建与提交](../design/generated-modules.md#构建输入与结果的提交) → [前置代码](../../PJM/backend/README.md#生成-module-の前置実装を読む) / [Web 边界](../../PJM/web/README.md#生成表示と業務-module-を分ける)，与业务 module API 分开 |
| 业务验收与评分 | [JAF 迁移/运行](../acceptance/jaf-quality.md) → [Benchmark 隔离与评分](../acceptance/jaf-benchmark.md)，区分普通 fixture、真实输入、Gold 与人工判断 |

### 身份与交付

| 变更 | 设计 → 实现与验证 |
| --- | --- |
| 登录入口防护 | [配额、公开响应与离页](../design/login-protection.md) → [Backend](../../PJM/backend/README.md#ログイン入口の防護を追う) / [Web](../../PJM/web/README.md#ログインと書込失敗を切り分ける)，分别验 API、真实 component 与真实环境 |
| 认证与 Secret | [会话](../design/authentication.md) / [外部凭据](../design/secret-storage.md) → [认证接线](../../PJM/backend/README.md#認証と-secret-の境界を追う)，不把用户生命周期与 ProjectMember 混用 |
| 用户创建、改密、停用与安全审计 | [已有调用与断点](../design/user-lifecycle.md#工作副本与公开入口) → [Backend 接线](../../PJM/backend/README.md#ユーザー管理の接続を引き継ぐ) → [管理验收](../design/user-lifecycle.md#开发接续与验收)；保持合跑，补契约同步，再接 Web 与真实事务验收，不重造已有服务 |
| 项目选择、成员、归档与删除 | [项目生命周期](../design/project-lifecycle.md) → [Backend 接线](../../PJM/backend/README.md#project-とメンバーの管理を追う) / [Web 接线](../../PJM/web/README.md#project-の切替と管理を追う)；核对实际引用、并发与删除失败，不把无 Run、行锁或归档当作充分保证 |
| 配置、部署与迁移 | [环境来源](../operations/deployment.md#环境文件与配置边界) / [迁移审查](../operations/deployment.md#迁移与回退审查) → [Backend CLI/Worker 边界](../../PJM/backend/README.md#運用-cli-と停止境界を確認する)、Settings、.env.example、Compose/Make；按[发布验收](../operations/deployment.md#后续开发约束与验收)核对所有写入者 |
| 备份、恢复与保留 | [一致恢复点](../operations/backup-recovery.md#一致恢复点包含什么) → [恢复验收](../operations/backup-recovery.md#恢复后验证)，核对 ops、存储、Outbox/Effect 与权限；preflight 不是恢复证明 |

路径均以 [PJM](../../PJM/README.md) 为起点。精确修改规则与同步点只维护在 [AGENTS.md](../../PJM/AGENTS.md)，本页负责带读者找到它们。涉及公开字段时继续读[契约变更与联调](contract-workflow.md)，先列消费者和历史兼容，再决定同步与发布顺序。

## 开工前必须回答的六个问题

| 问题 | 应留下的说明 |
| --- | --- |
| 用户要解决什么问题？ | 一个具体场景、现状影响、目标行为和非目标 |
| 谁拥有规则？ | 领域设计、现有 service/validator，是否已有可复用实现 |
| 什么时候固定事实？ | 创建、准备、Tool 读取、用户响应各自的快照与授权边界 |
| 哪些数据会变化？ | 公开 Schema/DTO、持久字段、checksum 和派生视图；不臆造同名表 |
| 失败或重发会怎样？ | 不可达、拒绝、部分成功、幂等、并发、取消与历史恢复 |
| 怎么证明完成？ | 对应测试、可观察结果及需另行提供的 DB/部署/模型条件 |

先核对工作副本和[当前状态](../planning/roadmap.md#13-当前执行状态)。发现未完成改动时按[交付链分层检查](contract-workflow.md#遇到未接齐的交付链)保留现场并标明范围，不能把旧测试数字视为这些改动已通过，也不能为让文档一致就覆盖原实现。

## 留下一条可接手的开发任务

不需要重读全部交付日志。把每次待实现工作写成下面六项，并链接正本；任务中的内容是可观察承诺，不只是“增加一个 class”。下例已有局部实现，示范如何承接剩余验收，不是要求重新开发同一功能。

| 项目 | 示例：创建响应丢失后再次确认 |
| --- | --- |
| 场景与风险 | 第一次 POST 可能已经成功，第二次点击换键会创建另一个 Run |
| 目标与非目标 | 同一次提交保留原内容/键；不新增 Run lifecycle，不持久化敏感输入来代替隐私设计 |
| 设计责任 | [创建 §4](../design/run-creation.md#4-响应界面与历史兼容)，不是 Schedule 恢复或 Worker Attempt |
| 已有基础与缺口 | Backend 意图/重放、Web 待确认提交和文档选择/清单消费者已存在；继续真实 DB 并发/回滚与版本兼容验收，不重做已有入口 |
| 同步范围 | Web 请求状态、共享 TaskDraft/输入、API client、三语提示和相关测试；若公开形状不变，不机械新增 Schema/migration |
| 完成证据 | 丢响应、abort、重复点击、编辑/切换账号/刷新分别测试；确认相同 Run ID 与原输入，不只检查按钮变灰 |

代码核对、纯测试、真实事务和人工业务质量是不同证据。测试自身报错应先恢复可信测试，再判断对应行为；不能跳过失败后把整个领域记为完成。阶段结果更新[计划](../planning/roadmap.md#131-完成状态的判定)，具体命令和失败原因追加到[历史](../history/README.md)。

## 设计修改的交付内容

先写清触发场景、现有问题与新行为，再列出受影响的数据契约、调用方、失败/恢复语义和兼容方式。存在历史快照时说明如何继续读取，不能靠重写历史“对齐”。

对于未实现的设计，明确当前缺少的 Schema、数据库或 UI，给出进入实现所需条件和可观察的验收。对于已实现行为，链接代码/契约；不把类名、业务概念和数据库表混写。

示例：新增 Tool capability 需要同步版本化 request/response/error、Provider、registry、permission snapshot、就绪度和测试。只在文档或 catalog 写一个名称不能得到可执行 Tool。

### 示例：收窄 Run 的文档输入

这类变更先确定“单份/集合/全集”语义，再对齐四处：用户确认的选择、服务端冻结清单、Provider/物化器可读范围、Run 详情展示。只更换下拉框，或只给 metadata 加 checksum，都不足以完成隔离。

失败与兼容至少覆盖：排队期间新增/删除/同路径重传、同一请求重发、多个槽位交集、跨 Project、缓存篡改、旧 Run 没有清单。已有 JSON 列可否承载版本化快照需要核对实际模型；只有确实改变 DB schema 才增加 migration，不能把“设计有新对象”机械等同于“新建一张表”。完整规则只在[资源设计](../design/resource-snapshots.md)维护。

## 特别需要先修正的差距

差距的当前实现、剩余范围与优先顺序只维护在[计划 R01–R13](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)。原先这里的逐项进度摘要已收敛为以下审查入口，不再复制一份容易过时的任务清单。

| 遇到的信号 | 开工前要核对什么 |
| --- | --- |
| 清单或 checksum 已存在 | [资源边界](../design/resource-snapshots.md#已知差距与后续设计)：选择、授权、实际 byte、逐根/总量和旧 Run 不是同一个证明 |
| 有预算 DTO、usage 或停止键 | [接线门禁](../design/run-budgets.md#上线门禁与接线顺序)：可信计量、主子调用方、结算权与实 DB，内部记录不自动约束执行 |
| 有取消检查或终态 | [等待与终态事务](../design/run-supervision.md#等待提交仍是独立边界)：锁竞争、提交未知、接管和进程退出分别举证 |
| 有 row_version、更新 API 或分页 | [调度修正](../design/task-scheduling.md#可靠性修正要求待实现)：实际条件写入、认领恢复、计数以及页面是否完整读取 |
| 远端内容相同或重试成功 | [效果恢复](../design/repository-effects.md#可靠性修正要求)：原执行身份、CAS、阶段回执与批准重发，不能只看最终内容 |
| CSP/静态检查已经通过 | [生成代码边界](../design/generated-modules.md#现有代码与公开契约)：完整构建身份、产物、隔离和 Host 证明，不能据此授权运行 |
| 配置检查、smoke 或均分通过 | 核对[实际环境来源](../operations/deployment.md#环境文件与配置边界)、[恢复条件](../operations/backup-recovery.md#恢复后验证)和[质量判断](../acceptance/jaf-benchmark.md#指标与通过条件)，不要跨范围借用结论 |

对应实现已存在时接续剩余工作，不重复造组件，也不把必需依赖改为 optional 来隐藏失败。新差距写回负责该规则的设计和计划，本页只保留可复用的核对方法。

## 验证与记录

按[开发验证表](local-development.md#変更に応じた検証)运行相关检查。结果写明本地/实 DB/部署/模型/浏览器各自覆盖范围；skip 和未执行不能记为通过。

完成后更新对应设计、计划状态与必要的交付记录。不要把新一轮实施日志继续追加到设计主正文；具体文档维护方式见[文档约定](documentation.md)。
