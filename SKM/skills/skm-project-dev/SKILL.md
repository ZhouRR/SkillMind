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
| 代码结构、共享实现与同步点 | ../docs/development/coding-rules.md |
| 公开契约、持久格式与消费者 | ../docs/development/contract-workflow.md |
| 环境、验证命令及副作用 | ../docs/development/local-development.md |
| 文档、README/AGENTS 与浏览版 | ../docs/development/documentation.md |

## 防止文档膨胀

按文档维护正本执行，并在每次改动时检查：

- 是否改变稳定规则或操作流程？没有就不机械新增项目文档；功能状态变化只更新 roadmap 对应项，不复制进度。
- 能否原位更新或引用已有正本？替换旧段、删除重复；README 做导航，AGENTS 留必需约束，Skill 留路由。字段全集和命令指向契约/操作指南，必要短例可保留。
- 是否把交付日志写进长期文档？不追加轮次记录、本次测试次数、临时路径或命令输出；必要的兼容、回退和验收步骤仍归所属专题。
- 是否真正需要新文件？默认不建模块 README、历史/验收流水页，打包说明保持极短；新页应有独立职责，不靠搬到 references 掩盖重复。
- 精简后能否继续安全开发？保留正本中的权限、冻结、批准、事务/结果未知和兼容规则，允许删除其重复表述；不靠挤排版凑行数。标题/路径变化同步引用。

## 验证与收尾

- 遵守 AGENTS 的修改/授权边界，按开发指南选择本次检查并报告实际证据；先核实环境，不固化 Git/Docker 可用性等临时状态。
- 不建 venv，优先复用依赖；Python 检查用 PYTHONDONTWRITEBYTECODE=1，外置依赖用 PYTHONPATH。只清理本次创建且目标已确认的临时物，保留已有成果；不以删后重建绕过权限拒绝。
- 文档构建/浏览按维护正本执行，不手改 HTML。仅改本 Skill 时验证格式、引用和决策边界；浏览版未受影响就不重建，也不追加计划记录。
- Skill 更新只处理指定包，不自动同步安装副本或其他执行资产；system Interpreter 的版本/hash 同步要求见实现细则，不套用于本开发 Skill。
