# Run 预算与执行限额

> 定位：现有约束核对与共享预算的后续设计。主 Agent、子分析和恢复共同遵守本页；不是已发布的预算 API。前置阅读：[Runtime](agent-runtime.md)、[子分析](subagents.md)。准备约束与共享账本分别在[计划 R01 / R02](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)记录状态。

按问题阅读：[当前限制](#当前实现的实际口径)、[哪个 timeout 生效](#现有计时器的覆盖范围)、[共享预算目标](#2-目标与非目标)、[预留与并发](#3-一个账户多个执行预留)、[取消后的用量](#4-结束取消与故障恢复)。

## 1. 先区分上限、分配与消耗

冻结上限回答“最多允许多少”，预留回答“已经承诺给哪些执行”，实际消耗回答“已经用了多少”。三者不能互相替代。当前 `RunLimits` 主要用于局部执行限制，不能据此宣称整个 Run 已有严格累计额度。

### 当前实现的实际口径

以下为 2026-09-08 对工作副本的只读核对，不是部署或模型验收。

| 字段/限制 | 当前执行边界 | 不保证什么 |
| --- | --- | --- |
| `max_turns` | [SDK options](../../PJM/backend/src/projectmind/agent/claude.py) 为每次模型执行传入上限 | 多个主 Session、子分析、重试共用一个剩余额度 |
| `max_budget_usd` | 可选 SDK 局部上限；普通创建的 [M0_LIMITS_SNAPSHOT](../../PJM/backend/src/projectmind/runs/service.py) 没有设置它 | Run 已有货币预算、实际账单精确等于上报用量 |
| `max_output_bytes` | [Tool Gateway](../../PJM/backend/src/projectmind/agent/tool_gateway.py) 检查单个序列化 Tool response 的 UTF-8 字节数 | 累计模型文本、所有 Tool 响应、Artifact 或磁盘总量受同一额度限制 |
| `wall_timeout_seconds` | [Executor](../../PJM/backend/src/projectmind/worker/executor.py) 在 context/Brief 准备后为本次 engine stream 建立 deadline | 准备阶段、排队、人工等待或所有 Attempt 的累计时长都被这个计时器覆盖 |
| `run_preparation_timeout_seconds` | Worker 注入 Executor，只包住本次 ContextBuilder 准备，默认 300 秒 | 包住整个 job 或终态事务；回滚已提交的输入回执；硬杀正在进行的线程 I/O |
| 子分析 `budget` | [dispatch](../../PJM/backend/src/projectmind/agent/subagent_provider.py) 每次重新拆分父快照的 turns/output 上限，返回每支分配值 | 返回值是实际消耗；美元上限已在分支之间拆分 |
| `PROJECTMIND_RUN_MAX_ATTEMPTS` | claim 按当前 Segment 计数并限制技术重试 | 它能代替模型、Run 总成本或业务续行次数的限制 |
| 文件数量/字节 | [物化器](resource-snapshots.md#跨根总量的修正口径待实现)分别检查单根与全部输入的最终存量；workspace Provider 另有扫描/响应限制 | 模型成本限制，或包含临时文件、workspace/output 的整个 Run 磁盘配额 |

子 context 当前只替换 turns/output 字段，美元上限仍从父 context 继承。子分析摘要截短也是展示限制，不是模型用量计费。Tool contract 中 `budget` 的描述与实际“分配值”语义需要在 R02 一并对齐，不能由字段名称推导消费账本已存在。

### 现有计时器的覆盖范围

下表是当前配置与代码的口径，不是 Run 级累计时长策略。数值来源为 [Settings](../../PJM/backend/src/projectmind/core/settings.py)、[创建限额快照](../../PJM/backend/src/projectmind/runs/service.py)与 [Worker job 注册](../../PJM/backend/src/projectmind/worker/settings.py)；部署还须核对实际注入值。

| 计时器 | 当前值与范围 | 覆盖范围 |
| --- | --- | --- |
| Attempt lease | `PROJECTMIND_RUN_LEASE_SECONDS`：默认 60 秒，允许 30–300 | 可续期的执行权，不是准备或模型总时限；默认按 lease 的 1/3 间隔心跳 |
| 资源准备 | `PROJECTMIND_RUN_PREPARATION_TIMEOUT_SECONDS`：默认 300 秒，允许 1–3600 | ContextBuilder，包含其内部输入回执提交；不含之前的状态推进、之后的 Brief/启动校验与终态事务 |
| 单次仓库命令 | `PROJECTMIND_REPOSITORY_COMMAND_TIMEOUT_SECONDS`：默认 120 秒，允许 5–600 | 单个受控 git/svn 命令，不是多个根相加的准备时长 |
| 模型事件流 | 当前普通创建冻结 `wall_timeout_seconds=900` | engine stream 的 deadline；只在等待下一事件时施加 timeout，不中断事件持久化/终态事务 |
| 单个子分析 | `PROJECTMIND_SUBAGENT_BRANCH_TIMEOUT_SECONDS`：默认 300 秒，允许 30–600 | 单 branch，不是给 Run 新增一份时长或消费额度 |
| ARQ Run job | `execute_run`：`1200 + 准备配置秒数`，默认 1500 秒 | 整个 job 的最终防线；其他普通 job 仍用全局 1200 秒，cron 有各自设置 |
| 人工回答/批准等待 | 各自持久化的 `expires_at`，不使用准备或模型 timeout | 等待时释放执行 lease；期限处理由交互/批准协议和恢复任务负责，不据此赠送新的 Run 额度 |

例如准备耗时 240 秒、模型事件流耗时 850 秒：两项各在自己的默认期限内，但不属于“900 秒的 Run 总预算”。反之，准备超过 300 秒应发起准备取消，而不是占用剩余模型时间继续准备。延长 lease 不会延长准备 deadline；提高仓库单命令 timeout 也不会提高准备总时限。

准备超时采用协作取消。取消清理、线程 I/O、DB 等待可能使实际返回晚于设定秒数，不能承诺操作系统进程恰在该秒被终止。准备及 engine 等待 timeout 都不包住终态事务；ARQ job timeout 或 Worker 关停仍可能中断整个 job，因此不能把终态提交称为不可中断。不得为了延长资源准备而直接扩大 `wall_timeout_seconds` 的旧语义，修改 Run 限额时还须复核 job 的余量。

lease 失效、用户取消、准备 deadline、Provider timeout 和 job 关停的处理分别见 [Runtime §7.5](agent-runtime.md#75-取消超时与失去执行权)。计时器存在不证明进程已停止或账单已结清，也不完成下面的共享账户设计。

独立的 Effect Worker 当前复用 run_lease_seconds / run_max_attempts 配置，但没有 Run Executor 的贯穿心跳监督。人工等待释放的是主执行 lease；获准 apply 后另有 Effect lease，即使 Run 仍显示 WAITING_FOR_APPROVAL。其慢调用、接管与取消的[独立修正要求](repository-effects.md#执行权与取消)不能由本表的 Attempt 心跳推导为已满足。

## 2. 目标与非目标

目标是让同一个 Run 的所有收费执行使用同一持久预算，技术重试、业务续行和并行分支都不能重新获得完整额度。冻结授权不变，余额可以随可审计的预留和结算变化。

本设计不引入组织计费、用户钱包、动态价格推荐或预算充值；扩大冻结上限需要创建新 Run。Skill 解释、独立评估和模块构建不属于某次 Run 的执行账户，应有自己的限额策略。文件快照、Tool 单响应、Artifact 保留等资源限制独立保留，不把不同单位混成一个数字。

### 分开定义计量维度

| 维度 | 目标口径 | 实施前必须固定的规则 |
| --- | --- | --- |
| 模型 turns | 主/子所有执行的新增 turn 合计 | adapter 定义 turn 的边界，不能用 ToolCall 数或任意事件数代替；resume 的历史累计值不得重复记账 |
| 模型成本 | 同一币种的主/子执行用量 | 金额精度、上报是增量还是累计、缺失/延迟处理；不用二进制浮点直接做余额比较 |
| Run 累计输出 | 若启用，使用独立于单响应上限的显式策略 | 文本、结构化结果、Tool response 各计什么；TEXT_DELTA 与 TEXT_COMPLETED 不重复计同一文本 |
| 活动时长 | Run 活动区间的累计墙钟时间 | 并行子任务重叠区间不重复求和，排队和人工等待不计入；准备阶段需独立 timeout，不悄悄改变旧字段口径 |

这些是待冻结的策略维度，不是新增公开字段清单。SDK 单次限制、子任务 timeout、Worker job timeout 仍是局部防线。只有 adapter 能提供可强制的执行上界时，才能承诺对应维度的硬上限；事后 `usage` 统计本身不是执行前控制。

## 3. 一个账户，多个执行预留

```text
Run 创建：固定预算策略和计量版本
  └── Run 预算账户（PostgreSQL）
       ├── 主执行预留 → 执行 → 结算
       ├── 子分支 A 预留 → 执行 → 结算
       └── 子分支 B 预留 → 执行 → 结算
                         ↓
            后续 Segment / Attempt 只取剩余额度
```

对每个可加总的维度维持：`remaining = limit - consumed - reserved`。预留成功前不能启动收费工作；余额不足或账本不可用时关闭新执行入口，不回退到冻结的完整上限。活动时长使用区间记录，不能机械套用分支秒数相加。

| 概念 | 保存内容与责任 |
| --- | --- |
| 冻结预算策略 | Run、计量版本、各维度上限；与输入/权限快照一样不可覆盖 |
| 预算账户 | consumed/reserved、并发版本；PostgreSQL 是正本，Redis 不是独立余额来源 |
| 执行预留 | Run/Segment/Attempt、主/子身份、执行键、预留量、lease 世代、结算状态 |
| 用量记录 | adapter 来源、计量版本、去重键、增量/累计标识、结算依据与不确定性 |

以上为逻辑对象，不假定已有同名 table。新增持久载体时同步 DB model、migration、repository DTO 与恢复测试；不能只给 AgentTaskBrief 加一个 remaining 字段就宣称账本完成。

### 原子性与重复请求

一次预留在同一事务中检查状态、权限、余额与执行身份后提交。需要同时锁定这些对象时，顺序为 Run → Segment → Attempt → 预算账户 → 预留记录；不需要的层可跳过，不得在持有预算锁后回头获取 Run 子对象锁。该顺序扩展现有 [Run aggregate](domain-model.md#72-执行不变量)约束。

同一逻辑操作的重复预留返回原记录，不多扣额度；相同键携带不同参数应冲突。技术重试若真的重新启动收费执行，要使用新执行身份并另取余额。回放已完成 Tool 结果不再次调用 Provider，也不重复累计原记录的用量；为生成这次调用而发生的新模型执行仍须计量。

主执行不能预留全部余额后再把“同一笔”无偿复制给子分支。可先预留主执行的有界片段，再从未承诺余额给子分支分配；只有能证明主执行已停止且其局部上限同步收窄时，才可原子转移未消耗预留。父子预留相加不得重复代表同一额度。

## 4. 结束、取消与故障恢复

| 场景 | 处理要求 |
| --- | --- |
| 正常结束，有可靠用量 | 幂等结算实际消耗，释放可证明未使用的部分 |
| 取消/timeout | 先确认执行停止再结算；提出 interrupt 不等于进程已经停下 |
| Worker 崩溃、结果或用量丢失 | 标记待核对，保持不确定部分已占用；不能因 lease 过期直接全额退还 |
| 旧 Worker 晚到 | 用执行键、lease 世代和版本校验拒绝越期操作，重复上报不重复扣减或释放 |
| 进入人工等待 | 停止收费执行并结算/保留未决用量，释放 Worker lease；等待不恢复完整 Run 上限 |
| 实际上报超过预留或 adapter 不再可计量 | 记录异常、禁止继续启动收费工作，保留真实用量；不能截断账本数字伪装未超限 |
| Run 终态后晚到的结算 | 不重新打开 Run，不追加终态 RUN_SNAPSHOT 之后的 RunEvent；预算核对使用独立追加式记录 |

租约失效不证明外部模型进程已结束。恢复必须同时处理“谁还有权执行”和“哪些用量尚不确定”，不能只依赖一条 Redis 锁或最终 Result。无法证明余额时，需要可见的限制/错误说明，不应允许模型无限循环尝试预算 Tool。

## 5. 兼容、公开信息与实施顺序

旧 Run 没有可靠账本时，历史仍可读并标记“累计用量未知”，不回填零消耗。旧非终态 Run 在升级前明确处置：能可靠重建才续行，否则停止收费执行并引导创建新 Run，不静默发放新的完整额度。

保留现有 `max_output_bytes` 的单响应口径。新策略必须有版本，并说明是否启用累计输出/成本限制；`null` 不表示免费。Web/Brief 展示“上限、预留、已确认消耗、待核对量”时使用服务端投影，不能用分支分配数、Session 数或历史成本估算余额。

实施依赖为：计量与 adapter 能力契约 → 原子账户/预留/结算 → 主执行与子 dispatch 接入 → 恢复/等待/取消 → 公开投影与三语 UI。同步入口：Run 创建与 repository、AgentEngine/SDK adapter、Tool Gateway、Worker、子分析、相关 Schema/example 与测试。

## 6. 验收矩阵

| 给定条件 | 必须观察到的结果 |
| --- | --- |
| 主执行已消耗部分额度，再连续两次 dispatch | 每次只能分配真实未承诺余额，不再次分配 Run 完整上限 |
| 两个并发预留争抢最后余额 | 最多允许余额内的组合成功，余额不为负 |
| 同一预留/结算重复投递 | 不重复扣减或返还；不同 payload 的同一键冲突 |
| Segment 续行与 Attempt 重试 | 继承同一账户；真实新执行计费，历史累计报告不重复计量 |
| 崩溃发生在启动后、结算前 | 不确定额度保留；晚到旧 Worker 不能花费或释放新世代额度 |
| 主子并行、人工等待、准备超时 | 墙钟、子 timeout、准备 timeout 分别计量和强制，无重复累计或无界准备 |
| 单 Tool 响应过大、累计输出耗尽、成本未知 | 三类情况分别表达，不以一种局部限制冒充另一种保证 |
| 终态后晚到用量与旧 Run | 结果/终态事件不变；未知用量显式可见，不伪造历史余额 |

账本回归和 adapter 模型验证分别举证。仅 `split_budget` 单元测试通过，不满足本页的 Run 级验收。
