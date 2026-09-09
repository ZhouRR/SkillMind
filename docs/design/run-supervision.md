# 执行监督与停止

本页负责已有 Run 的取消、超时、失去 lease 与清理，不新增状态机。启动见 [Runtime](agent-runtime.md#从领取到模型启动的边界)，外部 Effect 有[独立边界](repository-effects.md#执行权与取消)，实施状态见[计划 R07](../planning/roadmap.md#r07-run-与审计)。

## 一个例子：点击取消之后

模型尚未返回首事件时，API 先保存取消意图；Worker 观察后取消自己拥有的 await，等待 client 清理，有效 lease 下才提交 CANCELLED。以下事实不能互相替代：

| 事实 | 只证明什么 |
| --- | --- |
| 请求已接受 | 收到取消意图，不是退出回执 |
| Run CANCELLED | 业务终态不可重开，不保证进程已停 |
| client 清理返回 | 清理调用结束，adapter 仍须证明外部停止 |
| 用量已确认 | 对应执行范围已核对，不把其他未知调用归零 |

这不是固定顺序的 UI 向导。主执行已有首事件时，终态和清理的顺序不同；Web 不得在无公开证据时显示“进程全停/额度退还”。

## 谁负责停止

| 责任方 | 责任 |
| --- | --- |
| API / RunService | 授权并保存意图，不等待所有外部进程 |
| [Executor](../../PJM/backend/src/projectmind/worker/executor.py) | 监督准备、heartbeat、取消、事件等待；有效 lease 下提交 |
| [AgentEngine](../../PJM/backend/src/projectmind/agent/engine.py) | 向其拥有的 client 传递取消/关闭，清理并提供核对依据 |
| 子 Provider | TaskGroup 持有全组 task，等待各支清理，不遗留后台工作 |
| Repository / Recovery | 按持久状态与 fencing 决定提交/接管 |
| [预算协议](run-budgets.md#结束取消与故障恢复) | 确认消费、保留未知占用，lease 过期不退款 |

取消 Run 不撤销已发生的远端写入，也不证明 Effect Provider 已停。

## 不同阶段如何收束

| 阶段 | 当前停止路径 |
| --- | --- |
| ContextBuilder 准备 | 取消 builder task，等待退出，不再冻结 Brief/启动模型 |
| 最终 gate 后、首事件前 | _first_engine_event 同时等首次事件与取消；由拥有 await 的 task 取消 connect/receive，不依赖 Session ID |
| 已有首事件 | 以 session_ref interrupt，消费侧继续处理终端/等待/deadline；interrupt 失败不是停止证明 |
| 子执行中 | TaskGroup 取消并等待全组，某支完成不再次打断兄弟清理 |

首事件前路径为“持久意图 → 取消等待 → connect/receive 退出 → 清理 → 有效 lease 下 CANCELLED”，以协作取消、清理返回为前提。各 deadline 见[计时器](run-budgets.md#现有计时器的覆盖范围)，不承诺硬杀时间。

线程文件 I/O 可晚返回，但不能再提交 READY/Brief 或启动；[输入现场](resource-snapshots.md#准备中断与再次使用)保留不重建。启动 gate 与模型调用不原子，gate、运行监督和数据库 fencing 缺一不可。

## 原因与执行权如何分类

先判提交权，再判原因：

| 条件 | 处理 |
| --- | --- |
| lease 失效 | 旧 Worker 退出，不补 FAILED/CANCELLED，合法恢复接管 |
| 持久用户取消且 lease 有效 | 取消收尾；无已观察 Session identity 不伪造事件 |
| 准备 deadline / Provider TimeoutError | 分别 preparation_timeout / context_build_failed |
| 模型下一事件 deadline | wall_timeout；另查已成立的持久取消 |
| job 关停/裸取消 | 传播执行取消，不创造用户意图 |
| SDK interrupted 但无持久取消 | 主执行 FAILED / agent_session_interrupted；payload 自称 user 也不授权取消 |

上述主执行分类已接入；子收集器将 interrupted 作为失败终端，分支失败不等于整个 Run 失败。旧原因缺来源则保持未记录，不改历史 CANCELLED。

## 提交时谁决定最终状态

按 Run → Segment → Attempt 锁后的持久事实与当前 lease 判定，不按浏览器点击或 SDK 时间戳。前提是事件身份与终态参数合法。

| 提交顺序 | 结果 |
| --- | --- |
| 取消意图先提交 | 保存 CANCELLED，不保存成功 Result，不覆盖取消占用的 sequence |
| SUCCEEDED/FAILED 先提交 | 保持原终态；后续取消返回 409 run_not_cancellable |
| 已 CANCELLED 再取消 | 返回已有状态，不重复终态 |
| 旧 lease 失效 | 不能因存在取消意图恢复提交权 |

[finalize_execution](../../PJM/backend/src/projectmind/runs/repository.py)已锁内复查取消，拒绝无意图 CANCELLED 候选并返回实际状态；service 在事务成功退出后才返回。repository 返回不等于 commit，提交响应未知须读原事实，不换身份重跑。

主终态只查同 Run/Attempt 的 PRIMARY，不把多个子 Session 当查询错误。取消覆盖时保留事件已观察用量但不保存成功正文；Session usage 与账本另见[用量投影](run-budgets.md#用量现在流向哪里)。

### 等待提交仍是独立边界

普通事件、suspend_for_interaction、suspend_for_proposal 已共用取消拒绝检查：锁后先验 lease/状态/事件身份，再查持久取消，随后才分配 sequence/新增记录。取消不能使旧 lease 或外来事件合法；TEXT_DELTA 不走持久路径。

```text
TX A：合法身份/lease → 发现取消 → 回滚等待/事件
  ↓ 释放锁；Worker 收到 RunCancellationRequestedError
TX B：重新取锁/验证 lease 与持久意图
  ├─ 有效：保存 CANCELLED
  └─ 失效：旧 Worker 退出
```

A 不新增待办、checkpoint、Session、事件/Outbox 或预授权 Effect，但不等于 B 已提交。两事务间可崩溃/失去 lease；意图保留，由合法恢复处理，不持锁 interrupt。等待先提交时按已存状态取消，独立 Effect 不被视为撤销。

## 收尾、终态与晚到信息

当前主执行先保存 Result/终态，再 finally 关闭 stream；子执行先关闭 stream/校验结果，再保存 Session/Tool 审计。首事件前取消及超时另有先收束 await 的路径。

主 _close_stream 会吞普通关闭异常，Engine disconnect 失败也会移除 active 注册；没有活动 Session 不是退出证明。目标要求：

1. 准备、事件等待、清理和提交各有唯一所有者；不重复取消兄弟清理，真实进程须有界停止/核对，wait_for 不等于硬杀。
2. DB 锁外清理，锁后以当前时间 fencing。done 会影响 heartbeat 寿命，不能假定 finally 都续租，也不能无限清理无限占权。
3. 停止事实独立追加核对；终态 Run 不重开、不在最后 RUN_SNAPSHOT 后补事件。当前尚无完整持久停止回执/查询接口。
4. 晚到用量由可信核对方按原身份处理；旧 Worker 不复权，未知占用不退款。子 v1 的必需 Session/Gateway 审计不放宽。

## 兼容与开发接续

保持已有准备监督、首事件取消、原因分类、终态覆盖、等待/普通事件拒绝与子 TaskGroup；接续真实取消/终态锁竞争、两个事务之间崩溃、提交不明及进程清理，随后将停止核对身份与[共享预算](run-budgets.md)接齐。

现有 endpoint/RunStatus/SSE 不变；旧 Brief/Result/事件不回写，缺原因/回执不补造。新增公开信息同步版本化 DTO、授权 allowlist、Web validator 与三语，不临时塞 STOPPING 状态。

## 验收矩阵

覆盖准备取消/超时、connect/receive 无首事件、清理暂停、兄弟清理交错、无原因 interrupted、取消与终态两种提交顺序、等待拒绝后失去 lease、等待先提交后取消、多子 Session 下只终态化 PRIMARY、disconnect 失败和终态后晚到用量。

入口：[执行结果回归](../../PJM/backend/tests/worker/test_execution_outcomes.py)、[取消写入回归](../../PJM/backend/tests/runs/test_cancelled_execution_writes.py)。真实事务/进程与 Web 事实展示分别验证，不把 task 结束或文档浏览检查当业务停止验收。
