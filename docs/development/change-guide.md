# 从设计到代码的变更指南

## 先确定改的是哪条链路

| 变更 | 设计依据 | 实现与验证入口 |
| --- | --- | --- |
| Skill 解释/发布 | [Skill 契约](../design/skill-contract.md)、[解释实现](../design/skill-interpretation.md) | backend skills；system Skill；skills/contract tests |
| 资源种类、Provider、scope | [资源快照](../design/resource-snapshots.md)、[Runtime](../design/agent-runtime.md) | integrations、agent/run_binding/context_builder；Tool Schema；Provider tests |
| Run 状态、等待、恢复 | [领域模型](../design/domain-model.md)、[Runtime](../design/agent-runtime.md) | runs、worker、DB migration；SSE/Web replay；有状态回归 |
| 外部效果 | [受控写入](../design/repository-effects.md) | effects/catalog 与 Provider；Proposal/API/Web；冲突与幂等测试 |
| 调度 | [TaskSchedule](../design/task-scheduling.md) | schedules、worker tick、API、ScheduleDialog；时间/恢复测试 |
| 多 Agent | [并行子分析](../design/subagents.md) | subagent Provider、Session、预算；并发与取消测试 |
| Web 页面与流程图 | [Workspace](../design/workspace.md)、[Task Flow](../design/task-flow.md) | pages/components、api/lib、i18n；Web 与浏览器检查 |
| 生成模块 | [后续设计](../design/generated-modules.md) | modules；首次执行门禁未完成 |
| 认证/Secret | [认证](../design/authentication.md) | auth、actor dependency、integrations/secrets；跨 Project/CSRF/密文测试 |
| 配置/部署 | [Runbook](../operations/runbook.md) | Settings、.env.example、lifespan/startup、Compose |

路径均以 [PJM](../../PJM/README.md) 为起点。精确修改规则与同步点只维护在 [AGENTS.md](../../PJM/AGENTS.md)，本页负责带读者找到它们。

## 设计修改的交付内容

先写清触发场景、现有问题与新行为，再列出受影响的数据契约、调用方、失败/恢复语义和兼容方式。存在历史快照时说明如何继续读取，不能靠重写历史“对齐”。

对于未实现的设计，明确当前缺少的 Schema、数据库或 UI，给出进入实现所需条件和可观察的验收。对于已实现行为，链接代码/契约；不把类名、业务概念和数据库表混写。

示例：新增 Tool capability 需要同步版本化 request/response/error、Provider、registry、permission snapshot、就绪度和测试。只在文档或 catalog 写一个名称不能得到可执行 Tool。

## 特别需要先修正的差距

- document 当前按项目全集物化；未来需要创建时冻结明确集合与版本。
- 子 Agent 当前只有单次 dispatch 分配；Run 共享预算、跨调用扣减与恢复尚未实现。
- Schedule 当前没有 claim 与 Run 创建的一体事务，也没有全局 task mutex；如要增强保证，先定义丢失/重复/重叠语义。
- generated 模块的静态检查尚不能证明完整供应链与浏览器隔离；需要独立验证。

这些差距已进入[计划](../planning/roadmap.md#132-下一步与当前决策)，不能仅修改文档就标记为运行时修复。

## 验证与记录

按[开发验证表](local-development.md#変更に応じた検証)运行相关检查。结果写明本地/实 DB/部署/模型/浏览器各自覆盖范围；skip 和未执行不能记为通过。

完成后更新对应设计、计划状态与必要的交付记录。不要把新一轮实施日志继续追加到设计主正文；具体文档维护方式见[文档约定](documentation.md)。
