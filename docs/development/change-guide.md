# 从设计到代码的变更指南

先读 [AGENTS](../../PJM/AGENTS.md)，按下表选择领域；实现入口集中在[代码根 README](../../PJM/README.md)，状态只在[计划](../planning/roadmap.md)维护。完整设计分工见[设计索引](../design/README.md)。

## 先确定改的是哪条链路

### 运行与数据

| 变更 | 设计与首要核对点 |
| --- | --- |
| 上传、下载、目录、清理 | [文档生命周期](../design/document-lifecycle.md)：跨存储提交、配额、引用、未知结果 |
| 来源、scope、输入准备 | [资源快照](../design/resource-snapshots.md)：选择、冻结、物化、可信缓存及真实读取 |
| 创建与重复提交 | [Run 创建](../design/run-creation.md)：原 actor、内容、幂等键与新执行的区别 |
| 状态、等待与恢复 | [Runtime](../design/agent-runtime.md)：Segment/Attempt、DB、事件和 Web |
| 普通答复、过期 | [用户交互](../design/user-interactions.md)：答案、期限与原请求确认，不与批准混用 |
| 结果与评价 | [结果设计](../design/results-evaluation.md)：引用核验、不可变原值与评价的独立提交 |
| timeout、取消、预算 | [执行监督](../design/run-supervision.md) / [预算](../design/run-budgets.md)：提交权、真实停止、计量与主子共享 |
| 外部写入、调度 | [受控写入](../design/repository-effects.md) / [调度](../design/task-scheduling.md)：原身份、认领、阶段回执与恢复 |

### Skill 与界面

| 变更 | 设计与首要核对点 |
| --- | --- |
| 解释、发布、项目启停 | [Skill 契约](../design/skill-contract.md) / [解释器](../design/skill-interpretation.md)：来源身份、候选、发布与就绪 |
| 页面、任务流程 | [Workspace](../design/workspace.md) / [Task Flow](../design/task-flow.md)：client、投影、三语与实际用户流程 |
| 生成界面、子分析 | [生成模块](../design/generated-modules.md) / [子分析](../design/subagents.md)：完整身份、安全门禁与共享预算，不能以局部静态检查授权执行 |

### 身份与交付

| 变更 | 设计与首要核对点 |
| --- | --- |
| 登录、会话、账户 | [登录防护](../design/login-protection.md) / [认证](../design/authentication.md) / [用户生命周期](../design/user-lifecycle.md)：API、页面、并发和真实环境分层验证 |
| 项目、成员、归档、删除 | [项目生命周期](../design/project-lifecycle.md)：系统角色、引用、归档竞争与删除范围 |
| 外部凭据 | [Secret](../design/secret-storage.md)：密文、keyring、轮换与旧备份恢复 |
| 配置、迁移、发布 | [发布手册](../operations/deployment.md)：Settings/.env.example、Compose/Make、所有写入者与失败停止 |
| 备份、恢复、保留 | [恢复手册](../operations/backup-recovery.md)：DB/blob/workspace/KEK、权限与不可回滚的外部事实 |

## 开工前必须回答的六个问题

1. 场景、当前问题、目标和非目标是什么？
2. 规则由哪个设计与现有 service/validator 负责？
3. 创建、准备、读取、答复分别在何时冻结事实和复查授权？
4. 哪些公开字段、持久格式、checksum 与消费者受影响？
5. 拒绝、重发、并发、取消、部分成功与恢复怎样处理？
6. 哪些可观察结果证明完成，哪些需要独立的真实环境？

已有实现先接续，不重造组件；发现未完成接线时按[契约 workflow](contract-workflow.md#遇到未接齐的交付链)标明所在层，保留用户已有改动。字段、配置、事件、DB 和工具改动套用[实现细则的同步点](coding-rules.md#同步点)。

## 留下一条可接手的开发任务

任务写清“场景 → 目标/非目标 → 设计 → 已有基础与缺口 → 同步范围 → 验收”。例如创建响应丢失：继续复用原内容/键确认同一 Run；覆盖 abort、重复点击、编辑、账号切换和刷新，不新增 lifecycle 或保存敏感输入来回避问题。

设计修改还应说明历史数据如何读取、哪些版本可以共存。新增概念不必然需要新表；只变内部实现不必然新增公开字段。新增 Tool capability 则必须同步版本化契约、Provider、registry、权限、就绪度与测试。

## 验证与记录

按[本地验证表](local-development.md#変更に応じた検証)执行相关检查。区分文档、静态契约、mock 回归、真实事务、浏览器、部署与模型；失败、skip 和未执行如实报告。相同表名、checksum、row_version 或一次绿色测试都不证明完整交付。

完成后更新规则正本和计划中的当前缺口，不再追加历史文档或在设计中堆积轮次日志；链接与浏览版按[文档维护](documentation.md)同步。
