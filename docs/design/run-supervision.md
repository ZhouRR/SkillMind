# 执行监督与停止

本页负责已有 Run 的取消、超时、失去 lease 与清理，不新增状态机。启动见 [Runtime](agent-runtime.md#从领取到模型启动的边界)，外部 Effect 有[独立边界](repository-effects.md#执行权与取消)，实施状态见[计划 R07](../planning/roadmap.md#开发任务)。

## 一个例子：点击取消之后

首事件前取消：API 保存意图，Worker 取消自己拥有的 await、等待 client 清理，再以有效 lease 提交 CANCELLED。须区分：

| 事实 | 只证明什么 |
| --- | --- |
| 请求已接受 | 收到取消意图，不是退出回执 |
| Run CANCELLED | 业务终态不可重开，不保证进程已停 |
| client 清理返回 | 清理调用结束，adapter 仍须证明外部停止 |
| 用量已确认 | 对应执行范围已核对，不把其他未知调用归零 |

已有首事件时终态/清理顺序不同；Web 无公开证明不得显示“进程全停/额度退还”。

## 谁负责停止

| 责任方 | 责任 |
| --- | --- |
| API / RunService | 授权并保存意图，不等待所有外部进程 |
| [Executor](../../SKM/backend/src/skillmind/worker/executor.py) | 监督准备、heartbeat、取消、事件等待；有效 lease 下提交 |
| [AgentEngine](../../SKM/backend/src/skillmind/agent/engine.py) | 向其拥有的 client 传递取消/关闭，清理并提供核对依据 |
| 子 Provider | TaskGroup 持有全组 task，等待各支清理，不遗留后台工作 |
| Repository / Recovery | 按持久状态与 fencing 决定提交/接管 |
| [预算协议](run-budgets.md#结束取消与故障恢复) | 确认消费、保留未知占用，lease 过期不退款 |

取消 Run 不撤销已发生的远端写入，也不证明 Effect Provider 已停。

## 不同阶段如何收束

| 阶段 | 当前停止路径 |
| --- | --- |
| ContextBuilder 准备 | 取消 builder task，等待退出，不再冻结 Brief/启动模型 |
| 最终 gate 后、首事件前 | _first_engine_event 同时等首次事件与取消；由拥有 await 的 task 取消 connect/receive，不依赖 Session ID |
| 已有首事件 | 以 session_ref interrupt，中断请求和 drain 共用有限正数期限（默认 30 秒）；超时或请求失败尝试 disconnect |
| 子执行中 | TaskGroup 取消并等待全组，某支完成不再次打断兄弟清理 |

中断期限从发送请求前起算，不因收到控制响应而重置。调用者取消继续传播，即使 SDK 捕获取消后返回也不改成成功或普通 timeout。兜底关闭仍可能失败或等待；不会设置停止证明、释放预算或据此确认进程退出。

以上依赖协作取消/清理，deadline 不承诺硬杀，见[计时器](run-budgets.md#现有计时器的覆盖范围)。线程 I/O 可晚返回，但不得再提交 READY/Brief 或启动；[输入现场](resource-snapshots.md#准备中断与再次使用)保留。启动 gate 与模型调用不原子，仍需运行监督和 DB fencing。

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

子收集器将 interrupted 作为分支失败，不直接决定 Run 终态；旧原因缺来源保持未记录，不改历史 CANCELLED。

## 提交时谁决定最终状态

先验事件身份/终态参数，再按 Run → Segment → Attempt 锁后事实和当前 lease 判定，不按浏览器/SDK 时间戳。

| 提交顺序 | 结果 |
| --- | --- |
| 取消意图先提交 | 保存 CANCELLED，不保存成功 Result，不覆盖取消占用的 sequence |
| SUCCEEDED/FAILED 先提交 | 保持原终态；后续取消返回 409 run_not_cancellable |
| 已 CANCELLED 再取消 | 返回已有状态，不重复终态 |
| 旧 lease 失效 | 不能因存在取消意图恢复提交权 |

[finalize_execution](../../SKM/backend/src/skillmind/runs/repository.py)锁内复查取消，拒绝无意图 CANCELLED，service 在 commit 确认后返回实际状态；未知只查原事实，不换身份重跑。

主终态只处理同 Run/Attempt 的 PRIMARY；取消覆盖保留已观察用量、丢弃成功正文，[Session 用量](run-budgets.md#用量现在流向哪里)与账本另算。

### 等待提交仍是独立边界

普通事件及 interaction/proposal 等待共用检查：锁后验 lease/状态/事件身份、持久取消，再分配 sequence/新增记录。取消不恢复旧 lease、不合法化外来事件；TEXT_DELTA 不持久化。

```text
TX A：合法身份/lease → 发现取消 → 回滚等待/事件
  ↓ 释放锁；Worker 收到 RunCancellationRequestedError
TX B：重新取锁/验证 lease 与持久意图
  ├─ 有效：保存 CANCELLED
  └─ 失效：旧 Worker 退出
```

A 不留待办/checkpoint/Session/事件/Outbox/预授权 Effect，也不证明 B 完成。两事务间崩溃或失权由合法恢复接手，不持锁 interrupt；等待先提交则按已存状态取消，不撤销独立 Effect。

### Tool 调用的提交与重放

ToolAuditLease 是调用回执，不是 Worker lease。Executor 在主 stream 消费/关闭范围绑定私有原 claim，MCP 捕获、子调用继承同 Run/Attempt；token 不进 RunContext、Provider 参数或 SDK options。退出撤销本地许可，不证明进程停止。

注册、调用前确认、结果保存均按 Run → Segment → Attempt → ToolCall 取锁，验完整原身份、状态、lease、取消；等锁、取消查询、flush 后取新时间。Provider I/O 在锁外，最终检查不保证物理 commit 时刻的 lease。

| 原调用事实 | 处理 |
| --- | --- |
| 首次注册提交已确认 | 仅该 runtime 的首次许可可执行一次；调用前再检查执行权 |
| RUNNING / FAILED / DENIED | 不发放新许可，不重跑 Provider，也不覆盖原失败/拒绝 |
| SUCCEEDED | 当前执行权有效且原 SDK Session/Tool/参数身份完整匹配时，只读重放原结果；仍校验响应契约与上限 |
| 已存成功属于旧 Attempt | 仅同 SDK Session 的合法 RESUME 可读；不把原 Tool/Evidence 改归当前 Attempt，fork/replace 不借用 |
| 注册或成功提交响应未知 | 不当作回滚，不补写失败或换键重跑；后续原调用查询区分成功与未决 |

首次 await 前复制参数、响应及 Evidence 的嵌套内容，frozen dataclass 不冻结 mapping。失权/取消后的晚到结果不发布成功 Evidence，已改 workspace 不回滚。[v2 附件](results-evaluation.md#可信附件的发布与读取)同门禁保存原字节/回执，重放再验内容；独立 Effect、子 Session recorder 和进程停止不由此保证。

## 收尾、终态与晚到信息

当前主执行先存 Result/终态再关 stream，子执行先关流/验结果再存 Session/Tool；首事件前取消/超时先收束 await。主 _close_stream 吞普通关闭异常，disconnect 失败也移除 active 注册，不能据“无活动 Session”证明退出。

待完善的停止协议必须满足：

1. 准备/等待/清理/提交各有唯一所有者；不重复取消兄弟清理，真实进程有界停止并核对，wait_for 不是硬杀。
2. 锁外清理、锁后 fencing；done 会影响 heartbeat，不假定 finally 持续续租，也不无限占权。
3. 独立追加停止核对，不重开终态、不在末次 RUN_SNAPSHOT 后补事件；完整持久回执/查询尚缺。
4. 可信核对方按原身份处理晚到用量，旧 Worker 不复权、未知占用不退款；子 v1 必需审计不降级。

## 兼容与开发接续

保持既有监督/Tool/Artifact/TaskGroup 门禁，停止核对与[共享预算](run-budgets.md)接续范围见[计划 R07](../planning/roadmap.md#开发任务)。endpoint/RunStatus/SSE 不变，旧 Brief/Result/事件不回写、缺回执不补造；新增公开信息须版本化同步，不临时增加 STOPPING。

## 验收矩阵

覆盖准备取消/超时、connect/receive 无首事件、清理暂停、兄弟清理交错、无原因 interrupted、取消与终态两种提交顺序、等待拒绝后失去 lease、等待先提交后取消、多子 Session 下只终态化 PRIMARY、disconnect 失败和终态后晚到用量。

入口：[执行结果](../../SKM/backend/tests/worker/test_execution_outcomes.py)、[取消写入](../../SKM/backend/tests/runs/test_cancelled_execution_writes.py)。真实事务、进程停止与 Web 展示分别验收。
