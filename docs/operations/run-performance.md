# Run 履历减负与耗时观测

本页说明不改变 Skill 语义的读路径优化及运行计时。任务身份、原文、审批、权限、事件持久化和恢复规则不变；不新增 HTTP 参数、数据库迁移或启动检查。

## 查询与展示

历史列表仍返回同一 DTO，SQL 只读取列表实际使用的 Run / Result 列。详情仅需要 Brief checksum，因此不读取 Brief 正文。详情的工具、证据、会话等明细集合仍完整返回；本批没有拆分概览 API 或实现服务端明细分页，也没有减少同一详情中的 SQL 查询次数。

工具列表、会话审计、原始结果的折叠区域只在展开时求值和挂载，关闭后卸载。待确认操作、失败和未知效果仍在原来的常显区域。模型生成的报告与业务结果不变。已收起的技术正文不参与浏览器页面内搜索，须展开后检索；这不是删除审计记录。

前端按序追加事件时不再排序整个历史；乱序插入二分定位，重复 sequence 继续 first-wins。会话展示缓存完整不变的前缀，只处理新事件；重放、中间替换或 Run 清空时回退到原全量投影。前缀引用校验仍是线性的，不宣称整个更新为 O(1)，也不裁剪已有事件。

## 固定日志事件

使用现有 JSON 日志，不另写 RunEvent、Outbox 或业务表。INFO 级别下新增如下事件，按 `run_id` 和 `run_attempt_id` 关联；列表查询没有可用 Run 身份时不补造。

| 事件 | duration_ms 的边界 |
| --- | --- |
| run.performance.history_query | 历史 repository 读取及 DTO 投影（含原标题查询） |
| run.performance.detail_query | 详情 repository 读取及 DTO 投影 |
| run.performance.prepare | Context 准备和 Brief 冻结 |
| run.performance.engine_total | 整个 engine 消费过程，含工具、持久化、校验和 cleanup |
| run.performance.engine_wait | 等待下一条 engine event 的累计时间；包含 SDK/模型/工具等待，不是纯推理时间 |
| run.performance.event_persist | 普通持久化 AgentEvent 的累计 await 时间 |
| run.performance.realtime_publish | 临时文字通知 publish 的累计 await 时间 |
| run.performance.result_finalize | 最终结果校验和终态保存时间 |

最后四项按 Attempt 汇总，`sample_count` 是 await 区间数量，不是模型请求数。`engine_total` 已包含这些区间，不能相加当作总耗时；部分分支和 cleanup 没有独立计时，不能声称完整无重叠分解。当前没有新增排队等待、纯模型推理、工具内部细分或首屏渲染耗时指标。

日志中不放 Skill 正文、工具参数、用户输入或异常正文。观测 handler 出错不会替换业务返回值、原异常或取消信号；不为了性能观测重试模型、延迟提交或增加业务门禁。日志仍使用原同步 sink，建议沿用现有本地 stdout 收集，不加入远程同步 handler。

## 验证与解释

运行相关核心计时、履历 query shape 和前端增量投影测试。浏览器另验证展开/收起、Run 切换、断线重放、待批准操作和 UNKNOWN 常显。查询列变少与 JSON 重解析次数减少，不等于已证明真实模型执行提速；用同样的任务、配置和并发情况比较日志与页面。

## MCP 和实时通知的执行开销

MCP catalog 和 Schema 的结构检查按完整 canonical 内容做有界进程内复用；真实调用参数和结果仍逐次验证，权限、凭据、业务响应不缓存。单工具 query 只解析目标工具，discovery 才投影全授权列表。相同 Schema 不再在每个子节点和每个工具上重复 meta-schema 校验。外部工具目录仍在真实调用前核对，不以缓存代替远端契约检查。

每个 Attempt 的 TEXT_DELTA 通过独立有界队列配送：最多 32 条、每条 16,384 字符，正常结束最多等待 100ms，取消时直接清理。仅过载的即时文字可丢弃，完整消息、结果、批准和持久事件不进入该队列。`realtime_publish` 现在与引擎消费并行，不能再与 `engine_wait` 相加解释总耗时；原授权、事件顺序和终态保存不减项。此项减少显示通道的反压，不宣称消除模型思考或 Effect 续行开销。
