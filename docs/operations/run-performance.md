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

## 连续处理的边界

`audit.export/v1` 可通过 `include_index: true` 返回精确引用/hash 的小索引，完整导出 Artifact 不变。直接引用该 Artifact 保存原记录，避免为了汇总再次把全文送回模型。索引不是实测正文，也不替代业务 Schema 或必要外部保存。

`tool.sequence/v1` 将 1–5 个参数已确定的只读/本地工具交给同一 Gateway 逐项执行，每个子调用单独授权、审计和计量；可用 JSON Pointer 完全一致检查决定是否继续。失败、条件不成立或聚合上限时停止后续。没有脚本、循环、动态参数、外部写入、审批、递归或子模型。已有单步路径仍保留；这不是 MCP 写操作序列。

Codex Worker 对同一 Run 的确认自动批准续行，可在一个有限 job 中复用 SDK 进程。每个 Attempt 仍有新 lease、Gateway 和秘密 MCP URL，原 Segment/审批/回读/Outbox 不变。旧 native turn 终态已保存、thread 已 unsubscribe 后才复用；不重放未确认操作。至多连续五段，无法为下一段预留原完整时间、人工等待、非 APPLIED 或竞争认领时退回原 Queue。`sdk_start` 可用于观察进程启动次数；本优化减少重建，不等于生产 inline 回执或减少每段的模型判断。

SDK 生命周期还分别记录 `sdk_start`、`sdk_thread`、`sdk_turn_start`；`sdk_reuse` 的 `status` 区分 `new` / `reused`，不包含正文。它们属于 engine 等待内的子区间，不能与 `engine_wait` 重复相加。暖续行保留原 Segment/Attempt 和每步心跳，仅减少进程启动；不把一次复用写成少一次模型决策。

直接回执试运行时，比较相同业务下的 `SESSION_STARTED`/`Segment` 数、模型调用间隔及 `EFFECT_APPLIED` 的 `delivery: INLINE`，同时核对 Proposal/Approval/Effect/Evidence 未减少。SDK 进程复用和一次工具内回执是不同机制。开关关闭时保留原运行路径；没有真实模型、桌面和生产事务验收前，不把局部测试通过写成实测提速。
