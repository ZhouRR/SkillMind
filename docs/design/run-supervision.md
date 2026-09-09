# 执行监督与停止

> 定位：已有 Run 的取消、超时、失去执行权与清理边界。前置阅读：[Runtime 启动流程](agent-runtime.md#74-从领取到模型启动的边界)。本页不新增 Run 状态机，也不负责外部 Effect 的停止；实现与验证状态见[计划](../planning/roadmap.md#13-当前执行状态)。

按问题阅读：[点击取消意味着什么](#一个例子点击取消之后)、[谁负责停止](#谁负责停止)、[首事件前怎么办](#不同阶段如何收束)、[怎样判定原因](#原因与执行权如何分类)、[取消与完成谁先提交](#提交时谁决定最终状态)、[终态之后还有什么](#收尾终态与晚到信息)、[开发接续](#兼容与开发接续)。

## 一个例子：点击取消之后

一次评审已经进入 RUNNING，但模型还没有返回首个事件。用户点击取消，API 保存意图；Worker 随后观察到它，取消自己等待的 SDK 操作并等待清理。仍有有效 lease 时，Worker 才能提交 CANCELLED。

| 看到的事实 | 可以得出的结论 |
| --- | --- |
| 取消请求已接受 | 平台收到取消意图；不是进程退出回执 |
| Run 显示 CANCELLED | 平台业务执行已经终态化；不重新打开这个 Run |
| client 清理已返回 | 对应清理调用结束；仍需按 adapter 证明外部执行已停止 |
| 用量已确认 | 只确认该报告的执行范围；不是把剩余所有调用自动归零 |

这不是四个保证依次完成的 UI 步骤。主执行有首事件时，终态提交与通用 stream 清理的顺序不同；未知的停止和用量不能由 CANCELLED 反推。Web 只能显示已公开的事实，不能自行增加“所有进程已停止”或“已退还预算”。

当前首事件前已有取消接线和 fake client 回归；它没有运行真实 SDK 子进程或验证真实数据库提交。具体范围由计划登记，不能把“仍需真实验收”误写成“完全没有实现”。

## 谁负责停止

| 责任边界 | 唯一应负责的事情 |
| --- | --- |
| API / RunService | 授权并保存取消意图；请求返回不等待所有外部进程 |
| [Run Executor](../../PJM/backend/src/projectmind/worker/executor.py) | 监督准备、heartbeat、取消和事件等待；只在有效执行权下保存 Run 状态 |
| [AgentEngine](../../PJM/backend/src/projectmind/agent/engine.py) | 把取消/关闭传入自己拥有的 client，清理执行并提供可核对的结果 |
| [子分析 Provider](../../PJM/backend/src/projectmind/agent/subagent_provider.py) | 管理整组分支 task 的寿命；主分支收束不能留下未等待的子清理 |
| Run repository / Recovery | 按持久状态和 lease 决定谁能提交、谁能接管；不把进程内异常当作授权 |
| [预算协议](run-budgets.md#4-结束取消与故障恢复) | 处理已确认消费、未知占用和晚到结算；不以 lease 过期退款 |

Effect Worker 是独立执行边界。取消 Run 不撤销已发生的远端写入，也不证明正在 apply 的 Provider 已停止；批准、Effect lease 与部分失败按[受控写入](repository-effects.md#执行权与取消)处理。

## 不同阶段如何收束

| 阶段 | 当前工作副本的停止路径 |
| --- | --- |
| ContextBuilder 准备中 | 取消监督通知 builder task；等待其退出，不再冻结新 Brief 或启动模型 |
| 最终启动校验之后、首事件之前 | `_first_engine_event` 同时等待首次取事件和取消通知；取消由拥有该 await 的 task 传入 connect/receive，不必等 Session ID |
| 已取得首个事件 | 监督器使用 `session_ref` 调用 interrupt，事件消费侧继续处理返回的终端/等待或 deadline；interrupt 失败本身不是停止证明 |
| 子分析正在执行 | Provider 的 TaskGroup 持有整组 task，取消后等待各支退出；一个分支结束不能再次打断另一支已在执行的清理 |

首事件前的用户取消路径可以这样读：

```text
观察到持久取消意图
  ↓
取消首事件等待任务
  ↓
SDK connect / receive 退出
  ↓
等待 client 清理返回
  ↓
以当前 lease 提交 CANCELLED
```

该图假设取消可协作传递、清理返回且没有失去 lease；条件不成立时按下一节处理。它不承诺固定毫秒延迟。准备、模型事件流和 job 的 deadline 使用[各自计时器](run-budgets.md#现有计时器的覆盖范围)，超时不能覆盖终态事务来“保证按秒返回”。

线程中的文件 I/O 可能在取消后才返回。晚到结果不能重新推动已取消的 coroutine 提交 READY、冻结 Brief 或调用模型；遗留世代按[输入中断协议](resource-snapshots.md#准备中断与再次使用)保留，不能删掉现场后重建。

启动前短事务与外部模型调用并不原子。最终 gate 之后仍可能有取消或接管；因此 gate、运行期监督和数据库提交的 fencing 都必须保留，不能因补了首事件等待取消就删除任一层。

## 原因与执行权如何分类

先判断谁有权写，再判断为什么停止。取消意图、deadline 到期、SDK 中断和 Worker 关停不能只按一个异常类型合并。

| 原因/条件 | 处理规则 |
| --- | --- |
| lease 失效 | 旧 Worker 停止并退出，不以旧身份写 FAILED/CANCELLED；后续由合法恢复流程决定 |
| 有持久用户取消、提交时 lease 有效 | 以取消流程收尾；没有已观察到的 Session identity 时，不伪造 Session event |
| 准备 deadline 到期 | 按 `preparation_timeout` 分类；Provider 自己报 TimeoutError 仍是 `context_build_failed` |
| 模型事件等待 deadline 到期 | 按 `wall_timeout` 分类；已成立的持久取消另行检查，不把超时当作用户取消 |
| Worker job 取消/关停 | 传播执行取消，不能据此创造用户意图；是否接管由持久状态和 lease 决定 |
| SDK 报 interrupted，但没有持久用户取消 | 主执行按 FAILED / `agent_session_interrupted` 收尾；事件名或 payload 自称 user 都不授予取消语义 |

2026-09-09 核对的工作副本已有上述主执行分类，以及提交事务内的取消复查；[原因回归](../../PJM/backend/tests/worker/test_execution_outcomes.py)覆盖无原因和自称 `user_interrupted` 的事件。子收集器仍把 interrupted 作为失败终端拒绝。两者都不把 SDK 通知当作用户意图，但分支失败与整个 Run 失败仍是不同层级。

这部分局部回归不覆盖真实数据库竞争、进程停止或完整原因审计。历史已保存的 CANCELLED 不根据新规则改写；没有来源证明的旧原因保持未记录。后续新增停止信息仍须同步契约与消费者，不能让模型自行选择“取消”来逃避失败。

## 提交时谁决定最终状态

取消与完成的先后，以同一 Run 行锁下的持久事实为准，不按浏览器点击时间、SDK event 的时间戳或 Worker 最后一次轮询排序。适用前提是事件身份合法、终态参数合法，且 Worker 在取得 Run → Segment → Attempt 锁后仍有有效 lease。

| 同一 Run 的提交顺序 | 对外应看到什么 |
| --- | --- |
| 取消意图先提交，终态事务后检查 | 终态按 CANCELLED 保存，不再保存成功 Result；取消请求占用的 sequence 不能被覆盖 |
| SUCCEEDED / FAILED 先提交，取消请求后取得锁 | 原终态不变；取消 API 返回 `409 run_not_cancellable`，不能显示成取消成功 |
| 已是 CANCELLED，再次请求取消 | 返回已有取消状态；不重开 Run，也不重复制造终态 |
| 旧 Worker 的 lease 已失效 | 即使存在取消意图也不能补写终态；执行权不会因取消而恢复 |

当前 [finalize_execution](../../PJM/backend/src/projectmind/runs/repository.py)已在锁内复查持久意图，拒绝没有意图的 CANCELLED 候选，并返回实际保存的状态。[RunService](../../PJM/backend/src/projectmind/runs/service.py)只在 transaction 成功退出后返回；调用方不能把候选 SUCCEEDED 或 repository 已返回当作 commit 成功。提交响应丢失仍是不确定结果，需要读取持久事实，不能换身份重跑模型。

同一 Attempt 可以包含主/子 Session。主终态只查找该 Run、该 Attempt 下的 PRIMARY Session，不用“本 Attempt 唯一 Session”定位。取消覆盖终端时，在取消事件中保留该事件已观察到的 SDK 用量，不保存成功正文；这不保证 Session usage 同步更新，更不是完整账本，保存口径见[用量流向](run-budgets.md#用量现在流向哪里)。没有已观察到的 Session 时允许只写终态快照，不伪造 Session event。

### 等待提交仍是独立边界

`INTERACTION_REQUESTED` 和 `CHANGE_PROPOSED` 分别进入 [suspend_for_interaction](../../PJM/backend/src/projectmind/runs/repository_interactions.py) 与 [suspend_for_proposal](../../PJM/backend/src/projectmind/runs/repository_effects.py)，不经过上述 finalize。当前工作副本已让这两条路径和普通持久事件的 `append_agent_event` 共用 [_reject_cancelled_execution](../../PJM/backend/src/projectmind/runs/repository_base.py)。

该检查位于 Run → Segment → Attempt 取锁、lease/Run 状态和事件 Run/Attempt 身份验证之后，序号分配与新增记录之前。若取消意图已提交，抛出 `RunCancellationRequestedError`，由 service 让本次 transaction 回滚；不新增待办、checkpoint、Session、事件 Outbox 或预授权 Effect。取消不会使失效 lease 或外来事件合法化；TEXT_DELTA 原本就不进入持久事件路径。

关键是：**拒绝等待提交，不等于已提交 CANCELLED**。Worker 即使尚未轮询到取消，也会处理这一明确的取消信号，在另一个事务中重新验证执行权和持久意图：

```text
事务 A：拒绝新增待办或事件
  取锁 → 验证执行权/身份
  → 发现取消 → 回滚
               ↓ 释放事务 A 的锁
事务 B：尝试保存取消终态
  重新取锁 → 重验 lease/意图
  ├─ 仍合法 → 提交 CANCELLED
  └─ lease 失效 → 旧 Worker 退出
```

两个事务之间可能崩溃或失去 lease。此时保留已提交的取消意图，不补写终态、不恢复旧 Worker 的执行权，由合法恢复流程接续。不得把两个事务描述为一次原子提交，也不得持锁 interrupt/等待外部进程。若等待事务先于取消提交，则按已保存的等待状态处理取消；已开始的独立 Effect 仍遵守[外部执行边界](repository-effects.md#执行权与取消)。

[等待/普通事件回归](../../PJM/backend/tests/runs/test_cancelled_execution_writes.py)覆盖序号落后与超前、错误身份/lease 以及 service 传播异常；[Worker 回归](../../PJM/backend/tests/worker/test_execution_outcomes.py)覆盖轮询仍为 false、观测用量和终态前失去 lease。它们使用 mock transaction/fake service，不证明真实 PostgreSQL 阻塞、回滚或接管已经通过。当前证据与剩余验收由[计划 R07](../planning/roadmap.md#r07-run-与审计)统一登记。

## 收尾、终态与晚到信息

当前主/子执行的完成顺序并不相同：

```text
主执行收到有效终端
  → 保存 Result / Run 终态
  → finally 关闭 stream

子分析收到有效终端
  → 消费结束并关闭 stream
  → 验证结果
  → 保存 Session / Tool 审计
```

首事件前取消和事件等待超时另有先收束等待任务的路径，不能用其中一个测试证明上面所有路径。主 Executor 的 `_close_stream` 还会吞掉普通关闭异常以保留既定收尾，Engine 的 active 注册也会在 disconnect 失败时移除；“找不到活动 Session”不能单独作为进程退出证据。

目标要求是把业务终态与执行停止的事实分别保存、分别核对，不把关闭失败改写成无事发生：

1. 准备/事件等待、清理、DB 提交各有明确所有者和寿命。同一取消流程不重复取消兄弟 task 的清理；真实进程拒绝协作时需要 adapter 的有界停止与核对策略，不能把 `wait_for` 写成硬杀保证。
2. DB 提交按 Run → Segment → Attempt 取锁，取得锁后检查当前时间、lease 和取消意图。模型/文件/进程清理在事务外；不能持锁等清理，也不能放宽 fencing 来补一条终态。
3. 主执行的 `done` 信号会影响 heartbeat 寿命，正常终态、异常组退出和最终清理要分别验证。不保证“所有 finally 都持续续租”，也不能让无限清理无限占有执行权。
4. 对未确认的停止保留独立、追加式的执行核对依据；Run 已终态时不重新打开、不在最后 RUN_SNAPSHOT 后追加 RunEvent。当前没有完整的持久停止回执/核对接口，不能在使用指南中假装已有查询命令。
5. 晚到信息经可信核对路径处理。失效 Worker 不恢复执行权；已发生消费也不能因为失去 lease 被丢弃。释放未用预算必须满足[共享预算的停止与结算条件](run-budgets.md#执行权与结算权分开)，关闭成功不等于零消费。

结果校验、Session 保存和停止核对是不同责任。子分析 v1 的必需 Session ID 与部分结果返回继续遵守[提交与兼容规则](subagents.md#提交顺序与版本兼容)，本页不放宽 Gateway 审计。

## 兼容与开发接续

先沿[Backend 监督接线](../../PJM/backend/README.md#実行の取消と停止を追う)核对已有代码，避免重做准备监督、首事件等待或分支 TaskGroup。

1. 保持已存在的主结果、Review/fork、首事件前、原因分类、终态取消覆盖、等待/普通事件取消检查与子清理回归；不要重做已经接入的路径。
2. 继续[等待提交](#等待提交仍是独立边界)与终态提交的真实事务验收，分别暂停在取消提交前后、两个事务之间和 lease 接管处；另补 commit 响应丢失的原执行确认。最终状态、Session、Result、Outbox 与 Web 重放需一起观察，不能只测 `_terminal_mapping`。
3. 确定停止核对的持久身份、记录和恢复权，与共享预算执行身份衔接；不把事后新建的 Session ID 当作收费前的预留身份。
4. 再验证真实 client/process、DB 提交竞争和接管。子任务新协议及自主能力扩展仍须先满足 [Run 预算上线门禁](run-budgets.md#上线门禁与接线顺序)。

已有取消 endpoint、RunStatus 与 SSE 契约保持不变。历史终态、Brief、Result 和事件不回写；旧记录没有停止原因或回执时明确为未记录，不猜测补齐。未来公开停止/结算信息必须通过版本化 DTO、授权 allowlist、Web validator 和三语展示，不能临时往 RunEvent 塞一个 STOPPING 状态。

## 验收矩阵

以下是验收要求，不是全部已通过的清单。每项记录具体暂停点、触发条件、可观察结果及使用 fake 还是真实基础设施。

| 场景 | 需要观察的事实 |
| --- | --- |
| 准备中取消/超时 | builder 退出；不再冻结 Brief/启动模型；正确区分准备 deadline 与 Provider 异常 |
| connect / receive 尚无首事件 | 用户取消、job 关停、失效 lease、wall timeout 四类原因分别到达清理；无伪造 Session event |
| 清理故意暂停 | 明确哪些路径已经保存业务终态、哪些仍在等待；不能只断言执行 task 最后结束 |
| 两个子分支先后结束 | A 清理结束不打断 B 清理；取消不重跑分支来补结果 |
| 无原因的 interrupted / 裸 CancelledError | 不伪装用户取消，不把引擎失败当成功结论 |
| 取消与终态先后提交 | 两种锁顺序分别测试；实际状态、Result 有无、Session、唯一 sequence 与最后 RUN_SNAPSHOT 一致 |
| 取消先提交，提问/提案/普通事件后保存 | 先检查取消再检查序号；拒绝事务不新增待办、checkpoint、Session、Outbox 或 Effect，不能只测 finalize |
| 等待拒绝后、终态提交前中断 | 分别注入进程退出和 lease 失效；意图保留，旧执行不补写，合法恢复可接续 |
| 等待先提交，随后取消 | 按持久等待状态处理；无新模型启动，已开始的独立 Effect 不被误报为已撤销 |
| 同 Attempt 存在多个子 Session | 主终态只更新 PRIMARY；不得误收尾子 Session 或因查询多行失败 |
| 终端后 disconnect 失败 | 失败可核对；不因 active 表已移除就断言真实进程停止 |
| 锁等待、取消与 lease 接管竞争 | 旧执行不终态化、不重新取得执行权；真实 DB 验证不能由 mock 通过代替 |
| Run 终态后用量晚到 | 不追加 RunEvent 或重开 Run；真实消耗按原身份结算，未知占用不自动退款 |

人工 UI 验收另外确认：取消响应只表示请求处理结果，终态是业务状态；没有公开的进程/账本信息不显示成已确认。文档浏览器检查仅验证这些说明的导航与布局，不属于上述业务验收。
