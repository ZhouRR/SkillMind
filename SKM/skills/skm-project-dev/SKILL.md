---
name: skm-project-dev
description: 在 Skillmind（SKM）仓库开发、重构、审查、验证或维护文档时使用；定位项目规约与契约，控制文档增量并按需验证。
---

# Skillmind 开发工作流

本 Skill 是阅读入口与操作补充，项目约束以 AGENTS.md 为准，不在这里复制设计和命令手册。

## 定位与阅读

定位目标代码根（含 AGENTS.md、backend/、web/、contracts/）；当前为 SKM/，docs/ 在其同级。下列路径相对代码根，不相对 Skill 安装目录；缺失时先定位或说明。

完整读 AGENTS.md，并按其要求确认当前计划、从变更指南选择领域设计；只加载本次相关细则，不默认遍历全部文档。设计与实现矛盾时先说明影响，再按用户任务范围对齐。

| 需要确认的内容 | 正本（相对代码根） |
| --- | --- |
| 当前状态 / 设计入口 | ../docs/planning/roadmap.md / ../docs/development/change-guide.md |
| 前端风格、信息密度与 PC 验收 | ../docs/design/workspace.md#视觉规范 |
| 代码结构、共享实现与同步点 | ../docs/development/coding-rules.md |
| 公开契约、持久格式与消费者 | ../docs/development/contract-workflow.md |
| 环境、验证命令及副作用 | ../docs/development/local-development.md |
| 文档、README/AGENTS 与浏览版 | ../docs/development/documentation.md |

## 避免过度设计

- 以本次用户场景的最小可用改动为终点；后置规划不自动纳入，不为假设中的扩展新建框架、抽象层或配置。
- 保持常用路径短，复用现有组件与原生命令。增加依赖、必填参数、交付文件或人工步骤前，先判断能否由现有信息自动完成；确需增加时说明当前问题和使用成本。
- 按场景与风险设置检查：构建、导出、部署、数据恢复各做本职工作，不把恢复/审计流程强加给日常操作。保留权限、数据保护、失败停止及可定位错误，不靠隐藏错误或忽略校验来“简化”。
- 验证范围与改动相称；请求已满足且相关回归通过就交付，不顺手重构无关模块，也不为本规则另建审批表或检查框架。

## 防止文档膨胀

- 稳定规则变化才原位更新正本，功能状态只改 roadmap 对应项；不追加交付日志、测试次数、临时路径或命令输出。
- README 做导航，AGENTS 留必需约束，Skill 留工作流。引用既有契约/操作指南，默认不建模块 README、历史页或重复 references。
- 精简重复表述，不删权限、冻结、批准、事务/结果未知与兼容边界；标题/路径变化同步引用，不靠挤排版缩短。

## 验证与收尾

- 遵守 AGENTS 的修改/授权边界，按开发指南选择本次检查并报告实际证据；先核实环境，不固化 Git/Docker 可用性等临时状态。
- 不建 venv，优先复用依赖；Python 检查用 PYTHONDONTWRITEBYTECODE=1，外置依赖用 PYTHONPATH。只清理本次创建且目标已确认的临时物，保留已有成果；不以删后重建绕过权限拒绝。
- 文档构建/浏览按维护正本执行，不手改 HTML。仅改本 Skill 时验证格式、引用和决策边界；浏览版未受影响就不重建，也不追加计划记录。
- Skill 更新只处理指定包，不自动同步安装副本或其他执行资产；system Interpreter 的版本/hash 同步要求见实现细则，不套用于本开发 Skill。
