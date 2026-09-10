# 从设计到代码的变更指南

先读 [AGENTS](../../SKM/AGENTS.md)，再按下表选择正本。代码位置见[README](../../SKM/README.md)，当前缺口见[计划](../planning/roadmap.md)，完整分工见[设计索引](../design/README.md)。

## 先确定改的是哪条链路

### 运行与数据

| 变更 | 设计 / 核心问题 |
| --- | --- |
| 上传、下载、清理 | [文档资产](../design/document-lifecycle.md)：跨存储提交、配额、引用 |
| 选择、准备输入 | [资源快照](../design/resource-snapshots.md)：冻结、物化与可信缓存 |
| 创建、重复提交 | [Run 创建](../design/run-creation.md)：原身份、内容、幂等键 |
| 执行、等待、恢复 | [Runtime](../design/agent-runtime.md)：Segment/Attempt、事件 |
| 答复、评价 | [用户交互](../design/user-interactions.md) / [结果](../design/results-evaluation.md)：期限、原请求、不可变原值 |
| 停止、预算 | [监督](../design/run-supervision.md) / [预算](../design/run-budgets.md)：提交权、真实停止、主子计量 |
| 外部写、定时执行 | [受控写入](../design/repository-effects.md) / [调度](../design/task-scheduling.md)：认领、阶段回执、恢复 |

### Skill 与界面

| 变更 | 设计 / 核心问题 |
| --- | --- |
| 解释、发布、启停 | [Skill 契约](../design/skill-contract.md) / [解释器](../design/skill-interpretation.md)：来源、候选、精确版本 |
| 页面、流程预览 | [Workspace](../design/workspace.md) / [Task Flow](../design/task-flow.md)：投影、身份隔离、用户流程 |
| 生成 UI、子分析 | [生成模块](../design/generated-modules.md) / [子分析](../design/subagents.md)：安全门禁、能力与共享预算 |

### 身份与交付

| 变更 | 设计 / 核心问题 |
| --- | --- |
| 登录、账户 | [登录防护](../design/login-protection.md) / [认证](../design/authentication.md) / [用户](../design/user-lifecycle.md)：原会话、撤权、审计 |
| 项目、成员、删除 | [项目](../design/project-lifecycle.md)：角色、归档竞争、引用保护 |
| 外部凭据 | [Secret](../design/secret-storage.md)：密文、轮换、旧备份 |
| 配置、迁移、恢复 | [发布](../operations/deployment.md) / [恢复](../operations/backup-recovery.md)：所有写入者、同一恢复点、外部事实 |

## 开工前必须回答的六个问题

明确场景与非目标、规则正本、冻结/授权时点、受影响消费者、失败/重发/恢复行为、可观察验收结果。
已有 service/validator 应接续，不重造组件；新增概念不必然需要新表或公开字段。

## 留下一条可接手的开发任务

任务格式：**场景 → 目标/非目标 → 设计 → 当前缺口 → 同步范围 → 验收**。
涉及历史数据时说明读取与版本共存；未接齐的链路按[契约 workflow](contract-workflow.md#遇到未接齐的交付链)定位，不把单层完成当成交付。

## 验证与记录

应用[同步点](coding-rules.md#同步点)和[本地验证表](local-development.md#変更に応じた検証)。
规则回写领域设计，功能状态只更新计划；文档按[维护约定](documentation.md)生成检查。报告实际验证、失败/skip/未执行，区分 mock 与真实事务、浏览器、模型和部署。
