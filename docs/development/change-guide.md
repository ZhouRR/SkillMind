# 从设计到代码的变更指南

## 先确定改的是哪条链路

| 变更 | 设计依据 | 实现与验证入口 |
| --- | --- | --- |
| Skill 解释/发布 | [Skill 契约](../design/skill-contract.md)、[解释实现](../design/skill-interpretation.md) | [skills](../../PJM/backend/src/projectmind/skills/)、[system Skill](../../PJM/skills/README.md)、[契约](../../PJM/contracts/README.md) |
| 资源种类、Provider、scope | [资源快照](../design/resource-snapshots.md)、[Runtime](../design/agent-runtime.md) | [integrations](../../PJM/backend/src/projectmind/integrations/)、[documents](../../PJM/backend/src/projectmind/documents/)、[context_builder](../../PJM/backend/src/projectmind/agent/context_builder.py)、[Provider 测试](../../PJM/backend/tests/agent/) |
| Run 状态、等待、恢复 | [领域模型](../design/domain-model.md)、[Runtime](../design/agent-runtime.md) | [runs](../../PJM/backend/src/projectmind/runs/)、[worker](../../PJM/backend/src/projectmind/worker/)、[有状态回归](../../PJM/backend/tests/runs/)；DB/事件/Web 同步 |
| 外部效果 | [受控写入](../design/repository-effects.md) | effects/catalog 与 Provider；Proposal/API/Web；冲突与幂等测试 |
| 调度 | [TaskSchedule](../design/task-scheduling.md) | schedules、worker tick、API、ScheduleDialog；时间/恢复测试 |
| 多 Agent | [并行子分析](../design/subagents.md) | subagent Provider、Session、预算；并发与取消测试 |
| Web 页面与流程图 | [Workspace](../design/workspace.md)、[Task Flow](../design/task-flow.md) | pages/components、api/lib、i18n；Web 与浏览器检查 |
| 生成模块 | [后续设计](../design/generated-modules.md) | modules；首次执行门禁未完成 |
| 认证/Secret | [认证](../design/authentication.md) | auth、actor dependency、integrations/secrets；跨 Project/CSRF/密文测试 |
| 配置/部署 | [Runbook](../operations/runbook.md) | Settings、.env.example、lifespan/startup、Compose |

路径均以 [PJM](../../PJM/README.md) 为起点。精确修改规则与同步点只维护在 [AGENTS.md](../../PJM/AGENTS.md)，本页负责带读者找到它们。

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

## 设计修改的交付内容

先写清触发场景、现有问题与新行为，再列出受影响的数据契约、调用方、失败/恢复语义和兼容方式。存在历史快照时说明如何继续读取，不能靠重写历史“对齐”。

对于未实现的设计，明确当前缺少的 Schema、数据库或 UI，给出进入实现所需条件和可观察的验收。对于已实现行为，链接代码/契约；不把类名、业务概念和数据库表混写。

示例：新增 Tool capability 需要同步版本化 request/response/error、Provider、registry、permission snapshot、就绪度和测试。只在文档或 catalog 写一个名称不能得到可执行 Tool。

### 示例：收窄 Run 的文档输入

这类变更先确定“单份/集合/全集”语义，再对齐四处：用户确认的选择、服务端冻结清单、Provider/物化器可读范围、Run 详情展示。只更换下拉框，或只给 metadata 加 checksum，都不足以完成隔离。

失败与兼容至少覆盖：排队期间新增/删除/同路径重传、同一请求重发、多个槽位交集、跨 Project、缓存篡改、旧 Run 没有清单。已有 JSON 列可否承载版本化快照需要核对实际模型；只有确实改变 DB schema 才增加 migration，不能把“设计有新对象”机械等同于“新建一张表”。完整规则只在[资源设计](../design/resource-snapshots.md)维护。

## 特别需要先修正的差距

- document 冻结改动在工作副本中尚未贯通；必须同步选择 UI、公开快照、幂等与历史兼容。
- repository 冻结授权不等于创建时固定 commit；逐根物化额度也不是 Run 总量账本。
- 子 Agent 当前只有单次 dispatch 分配；Run 共享预算、跨调用扣减与恢复尚未实现。
- Schedule 当前没有 claim 与 Run 创建的一体事务，也没有全局 task mutex；如要增强保证，先定义丢失/重复/重叠语义。
- generated 模块的静态检查尚不能证明完整供应链与浏览器隔离；需要独立验证。

这些只是开工风险提醒，精确进度见[计划](../planning/roadmap.md#132-下一步与当前决策)，不能仅修改文档就标记为运行时修复。

## 验证与记录

按[开发验证表](local-development.md#変更に応じた検証)运行相关检查。结果写明本地/实 DB/部署/模型/浏览器各自覆盖范围；skip 和未执行不能记为通过。

完成后更新对应设计、计划状态与必要的交付记录。不要把新一轮实施日志继续追加到设计主正文；具体文档维护方式见[文档约定](documentation.md)。
