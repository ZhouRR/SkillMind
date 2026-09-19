# Run 耗时观测与优化

先用相同任务、输入、模型配置和并发条件比较耗时与业务结果，再决定优化范围。关联 `run_id` / `run_attempt_id`，分别记录首次执行、等待批准/答复及续行；不把一次最快结果当成整体收益。

## 固定日志事件

INFO JSON 日志使用 `run.performance.` 前缀。实现入口为[计时器](../../SKM/backend/src/skillmind/core/timing.py)、[执行器](../../SKM/backend/src/skillmind/worker/executor.py)、[结果校验](../../SKM/backend/src/skillmind/agent/result_validation.py)与[查询](../../SKM/backend/src/skillmind/runs/repository.py)。

| 事件后缀 | 观测范围 |
| --- | --- |
| `history_query` / `detail_query` | 履历/详情读取与 DTO 投影 |
| `prepare` | Context 准备和 Brief 冻结 |
| `engine_total` | engine 消费、工具、持久化、校验和 cleanup |
| `engine_wait` | 等待 engine event 的累计时间，含 SDK/模型/工具等待 |
| `event_persist` / `realtime_publish` | 持久事件保存 / 即时文字通知 |
| `result_finalize` | 结果校验与终态收尾 |
| `result_validation` / `result_schema` / `result_references` | 结果完整校验 / Schema / 引用核对 |
| `terminal_save` | 终态持久化 |
| `final_output` | 最终数据 `output_bytes`，无 duration_ms |

耗时单位为 `duration_ms`；`sample_count` 是累计 await 区间数，不是模型请求数。区间存在包含和并行关系，不能相加为总耗时；`engine_wait` 也不是纯推理时间。当前日志不能完整拆分排队、工具内部和页面首屏时间。

## 按瓶颈行动

- 准备慢：查资源读取、冻结输入大小和外部连接，不用当前资源替换原快照。
- 执行慢：比较工具调用、批准/续行次数及最终输出量；区分模型等待和远端工具耗时。
- 收尾慢：比较 Schema、引用核对与终态保存区间；已有 `audit.export/v1` 可导出平台原记录，减少重复生成，仍需满足结果契约和保存要求。
- 页面慢：分别观察 API 响应和渲染；履历技术明细按展开挂载，页面内搜索前先展开相应内容。

业务 Worker 与维护 Worker 已分队列；健康检查正常不证明没有业务排队。实时文字配送采用有界队列，过载可能丢即时片段，完整消息和持久结果仍应从详情核对。

## 验证与解释

每次只调整已确认瓶颈，比较多次执行的耗时、输出完整性和业务准确性；需要真实模型/写入时使用获准环境。查询列减少或测试变快不证明真实任务提速，隔离实验也不代表生产恢复能力已闭环。

观测不记录正文或凭据，不为提速跳过授权、批准、read-back、持久事件或结果校验。质量缺陷与耗时问题分别记录，实施验证见[本地开发](../development/local-development.md)。
