# 从设计到代码的变更指南

## 先确定改的是哪条链路

不确定设计归属时，先看[设计责任索引](../design/README.md)。本页只把选定的规则连接到代码与验证，不维护另一份领域进度表。

| 变更 | 设计依据 | 实现与验证入口 |
| --- | --- | --- |
| Skill 解释/发布/项目启停 | [阶段判断](../design/skill-contract.md#发布与就绪的判断顺序)、[版本回滚](../design/skill-contract.md#11-版本回滚与评价) | [候选到任务的接线](../design/skill-interpretation.md#从候选到项目任务的接线) → [Backend 入口](../../PJM/backend/README.md#skill-の公開と-project-可視性を追う)；版本/启用 Schema、SkillsPage 与生命周期回归一起核对 |
| 资源种类、Provider、scope | [资源快照](../design/resource-snapshots.md)、[Runtime](../design/agent-runtime.md) | [integrations](../../PJM/backend/src/projectmind/integrations/)、[documents](../../PJM/backend/src/projectmind/documents/)、[context_builder](../../PJM/backend/src/projectmind/agent/context_builder.py)、[Provider 测试](../../PJM/backend/tests/agent/) |
| 输入准备、可信缓存与物理路径 | [输入准备协议](../design/resource-snapshots.md#输入准备与可信缓存) | [Backend 接线表](../../PJM/backend/README.md#入力準備の接続を引き継ぐ)：DB/store → 物化/ContextBuilder → Worker 监督 → 实际 read/search；同字节、完整树与失败提交回归 |
| 准备超时、取消与启动门禁 | [Runtime 启动边界](../design/agent-runtime.md#74-从领取到模型启动的边界)、[计时器口径](../design/run-budgets.md#现有计时器的覆盖范围) | Executor → RunService/Repository → Settings/startup；分别验证慢准备、失效 lease、终态提交与首事件前取消，不混用 Provider timeout 和准备 deadline |
| 创建请求、幂等与重复提交 | [Run 创建](../design/run-creation.md) | [意图规范化](../../PJM/backend/src/projectmind/runs/creation_request.py)、[保存记录兼容](../../PJM/backend/src/projectmind/runs/creation_replay.py)、[Web 原请求状态](../../PJM/web/src/hooks/useRunSubmission.ts)、route/service/repository 与契约；真实 DB 的唯一约束与回滚 |
| Run 状态、等待、恢复 | [领域模型](../design/domain-model.md)、[Runtime](../design/agent-runtime.md) | [runs](../../PJM/backend/src/projectmind/runs/)、[worker](../../PJM/backend/src/projectmind/worker/)、[有状态回归](../../PJM/backend/tests/runs/)；DB/事件/Web 同步 |
| 外部效果 | [受控写入](../design/repository-effects.md) | effects/catalog 与 Provider；Proposal/API/Web；冲突与幂等测试 |
| 调度 | [TaskSchedule](../design/task-scheduling.md) | schedules、worker tick、API、ScheduleDialog；时间/恢复测试 |
| 多 Agent | [并行子分析](../design/subagents.md) | subagent Provider、Session、预算；并发与取消测试 |
| 限额、模型预算与用量 | [Run 预算](../design/run-budgets.md) | Run 快照与持久账户、SDK adapter、主/子执行；计量/预留/晚到结算回归 |
| Web 页面与流程图 | [Workspace](../design/workspace.md)、[Task Flow](../design/task-flow.md) | pages/components、api/lib、i18n；Web 与浏览器检查 |
| 生成模块 | [后续设计](../design/generated-modules.md) | modules；首次执行门禁未完成 |
| 认证/Secret | [认证](../design/authentication.md) | auth、actor dependency、integrations/secrets；跨 Project/CSRF/密文测试 |
| 配置/部署 | [环境文件边界](../operations/runbook.md#环境文件与配置边界)、[迁移审查](../operations/runbook.md#迁移与回退审查) | Settings、.env.example、lifespan/startup、Compose/Make；默认与自定义配置的一致性验证 |
| 备份/恢复与数据保留 | [恢复点](../operations/runbook.md#一致恢复点包含什么)、[恢复验收](../operations/runbook.md#恢复后验证) | ops、migration、存储/Outbox/Effect/权限；隔离演练，不以 preflight 代替恢复证明 |

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

先核对工作副本和[当前状态](../planning/roadmap.md#13-当前执行状态)。发现未完成改动时保留现场并标明范围，不能把旧测试数字视为这些改动已通过，也不能为让文档一致就覆盖原实现。

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

- document 选择、创建、读取/物化和公开清单已各有实现；按[契约联调](contract-workflow.md)维持跨层一致，继续真实事务、可信缓存及历史续行验收，不以可见清单证明缓存真实性。
- repository 冻结授权不等于创建时固定 commit；逐根、全输入存量和单次 search 的额度各自独立。总量累计与配置注入已有代码和局部回归，仍需跨根失败/恢复验收，不从设置项通过推导完整策略已通过。
- SDK/Tool 限额与子 Agent 单次分配不是 Run 共享预算；[预算设计](../design/run-budgets.md)明确计量、并发预留与不确定用量处理，不能把 lease 过期视为全额退款依据。
- Schedule 当前有认领、创建、回写三个事务；原键查询已先于重叠检查，但持久在途、配置版本、计数与迟到策略仍按[调度修正要求](../design/task-scheduling.md#可靠性修正要求待实现)补齐，不能仅凭稳定键或重放函数宣称可靠恢复。
- 有更新 API 不等于有编辑页面；时间格式化使用的浏览器时区也不等于 CRON 规则时区。调度表单的[现状与验收](../design/task-scheduling.md#保存表单与触发预览)应逐入口核对。
- generated 模块的静态检查尚不能证明完整供应链与浏览器隔离；需要独立验证。
- 输入回执与准备监督已有接线和局部回归；继续物化 fixture/消费者同步、提交结果未知、真实锁竞争与历史续行。按[接续入口](../../PJM/backend/README.md#入力準備の接続を引き継ぐ)核对 `claimed_run` 和 `PreparedInput`，不重新造回执、不把必需依赖改成可选；局部测试和未通过项以计划记录为准。
- 最终启动校验不等于模型启动的原子锁，取消接受也不等于外部进程已结束。[Runtime 取消边界](../design/agent-runtime.md#75-取消超时与失去执行权)明确首事件前窗口和异常清理的后续验收，不能仅测试准备取消就标记整个 Run 取消完整。
- `ENV_FILE` 目前只切换 Compose 插值，Backend 仍固定读取 `.env`；[配置修正](../operations/runbook.md#环境文件与配置边界)必须验证三个 Backend service 的实际注入，不能只更新使用手册或静态 YAML 检查。

这些只是开工风险提醒，精确进度见[计划](../planning/roadmap.md#132-下一步与当前决策)，不能仅修改文档就标记为运行时修复。

## 验证与记录

按[开发验证表](local-development.md#変更に応じた検証)运行相关检查。结果写明本地/实 DB/部署/模型/浏览器各自覆盖范围；skip 和未执行不能记为通过。

完成后更新对应设计、计划状态与必要的交付记录。不要把新一轮实施日志继续追加到设计主正文；具体文档维护方式见[文档约定](documentation.md)。
