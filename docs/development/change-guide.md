# 代码变更指南

先读 [AGENTS](../../SKM/AGENTS.md)与[当前状态](../planning/roadmap.md#当前执行状态)，明确用户场景、具体问题和完成标准，再查下表。规则边界集中在[运行维护指南](runtime-guide.md)，详细形状以源码与 Contracts 为准。

## 按问题定位代码

Backend 路径相对 `SKM/backend/src/skillmind/`。

| 场景 | 主要入口 | 核对重点 |
| --- | --- | --- |
| 登录、账户、项目权限 | auth/、projects/、api/auth_dependencies.py | 原身份、撤权、CSRF/API Key、资源隔离 |
| Skill 导入与发布 | skills/service.py、skills/source_loader.py、skills/source_storage.py；agent/interpreter* | 预览/保存/重建共用源包解析与字节存储；授权和事务留在 Service，原文 hash 与发布校验不变 |
| 任务、调度、输入 | runs/、schedules/、agent/context_builder.py | 精确任务、原请求幂等、冻结选择与物化 |
| 执行、等待、恢复 | agent/、worker/、runs/ | Segment/Attempt、lease、事件、取消、未知结果 |
| 数据库、Git、MCP 写入 | effects/、agent/*_provider.py、agent/*_source.py | 绑定、批准、冲突、原操作回执与回读 |
| 文档、目录、回收站 | documents/、storage/、runs/ | 原始输入、共享引用、恢复与完全删除 |
| 结果与评价 | agent/result_validation.py、evaluations/ | 原结果不变、引用、追加评价 |
| 页面与交互 | web/src/pages、components、hooks、api、lib | 页面组合、请求生命周期、纯数据转换分开；[UI 指南](../design/workspace.md)、原请求及响应验证 |
| 配置、迁移与恢复 | core/settings.py、backend/migrations、compose.yml | [部署](../operations/deployment.md)与[恢复](../operations/backup-recovery.md) |

## 保持改动与问题相称

局部样式、文案或错误诊断直接使用现有组件和服务，不额外设计流程。涉及权限、持久化或公开协议时，再核对冻结点、消费者、失败/恢复与版本兼容，按[契约流程](contract-workflow.md)同步。

不因指南提到后置能力而顺带补齐框架；不把性能优化变成跳过业务写入、审批、回读或结果校验。运行效果不足时先读[耗时观测](../operations/run-performance.md)，用实际证据选择下一处优化。

## 验证与交付

按[同步点](coding-rules.md#同步点)和[变更验证表](local-development.md#変更に応じた検証)选择回归。区分局部通过、真实事务/浏览器/模型验证与部署状态；明确失败、skip 和未执行项。
长期规则更新对应指南，功能状态更新 Roadmap，临时排查结果不增加正式文档。文档生成见[维护方式](documentation.md)。
