# Run 预算与执行限额

同一 Run 的主执行、子分析、Segment 续行与 Attempt 重试必须共用账户。本页区分现行局部限制与尚未接入执行的持久账本；不是预算 API。当前状态见[计划 R02](../planning/roadmap.md#r02-run-统一预算)。

## 先区分上限、分配与消耗

冻结上限是授权，预留是已承诺额度，消耗是已确认用量。分支拿到 10 turns 不等于用掉 10；Session 数和摘要长度也不是消费。

### 当前实现的实际口径

| 限制 | 当前边界 |
| --- | --- |
| max_turns | SDK 每次执行上限，不累计所有主子 Session/重试 |
| max_budget_usd | 可选 SDK 局部限制；普通创建快照未设置，子 context 原值继承、不按支拆分 |
| max_output_bytes | Gateway 单个序列化 Tool response 的 UTF-8 字节上限，不是累计输出/磁盘配额 |
| 子分析 budget | 每次 dispatch 重拆父冻结 turns/output 上限；连续调用未扣共享余额 |
| RUN_MAX_ATTEMPTS | 当前 Segment 的技术重试次数，不限制全部业务续行或 Run 成本 |
| 输入文件数量/字节 | 逐根及跨根最终存量，规则见[资源总量](resource-snapshots.md#跨根总量) |

例如父快照 20 turns，两次各派两支时每次仍各分 10，不构成全 Run 20 的保证。账本已有内部实现，但创建、主 Executor、子 Provider 与 Worker startup 均未调用。

### 用量现在流向哪里

| 来源 | 当前保存方式与缺口 |
| --- | --- |
| SDK mapper | usage 与 rate-limit 通知都可为 USAGE_UPDATED，Result 终端/等待另带 turns/cost；不能逐事件相加 |
| 主 Executor / Session | Result 按 key 更新，Session usage 最新替换、cost 保存最近总值；无统一来源/去重账本 |
| 取消覆盖事件 | 保留该事件已观察 usage/cost，丢弃成功/提问正文；SESSION_INTERRUPTED 不更新 Session usage，不能从事件反推相同投影 |
| 子收集器 / Session | 校验终端和结果，但不归集用量；子 usage 仅 branch_key、cost 空，失败也不等于零消耗 |

计量必须先确认 SDK 报告是否含历史、重试或子调用，不能把 Session、Result 和最终报告重复入账。

### 现有计时器的覆盖范围

默认配置来自 [Settings](../../PJM/backend/src/projectmind/core/settings.py)、[创建快照](../../PJM/backend/src/projectmind/runs/service.py)和 [Worker 注册](../../PJM/backend/src/projectmind/worker/settings.py)，部署以实际注入值为准。

| 计时器 | 默认值与覆盖 |
| --- | --- |
| Attempt lease | 60 秒，可配 30–300；约每 1/3 续期，仅代表执行权 |
| 准备 timeout | 300 秒，可配 1–3600；包住 ContextBuilder 及其输入提交，不含 Brief/启动/终态事务 |
| 仓库单命令 | 120 秒，可配 5–600，不是所有根准备总时长 |
| 模型事件流 | 普通创建冻结 900 秒，准备后起算；只对下一事件 await 施加 deadline，不打断事件持久化 |
| 子分支 | 300 秒，可配 30–600，不赠送 Run 额度 |
| ARQ execute_run | 1200 + 准备秒数，默认 1500；整个 job 最终防线，其他 job/cron 独立 |
| 人工等待 | 持久 expires_at；释放主 lease，不刷新 Run 总额度 |

准备 240 秒加模型 850 秒可分别合法，但不是 900 秒的 Run 总预算。续 lease 不延长 deadline；准备/事件 timeout 为协作取消，线程 I/O、清理或 DB 等待可能晚返回，不能承诺按秒硬杀。Job timeout/关停仍可能中断终态提交。

Effect Worker 另有 lease，复用部分配置但无主 Executor 的贯穿监督；其[执行权/取消缺口](repository-effects.md#执行权与取消)不能由本表推导为已解决。

## 目标与非目标

目标是冻结授权不变、余额可审计变化，重试/续行不重领完整额度。不引入钱包、充值或组织计费；扩大上限新建 Run。Skill 解释、独立评估、模块构建另设限制，不借本 Run 账户。

### 分开定义计量维度

| 维度 | 必须冻结的口径 |
| --- | --- |
| turns | Adapter 定义新增 turn；不按 ToolCall/事件数代替，resume 历史不重复 |
| 成本 | 币种/精度/累计或增量/延迟规则；固定精度，不用 float 比余额 |
| 累计输出（后续） | 明确文本/结构化/Tool response 范围，DELTA 与 COMPLETED 不重复 |
| 活动时长（后续） | 累计墙钟区间，子并行重叠不相加，排队/人工等待不计，准备仍独立 timeout |

只有 adapter 可强制执行上界，才可承诺硬限额。未启用某维度不宣称受限；已启用但不可计量则阻止新收费执行。null 不等于零或免费。

### 计量报告如何归一化

| 报告 | 入账规则 |
| --- | --- |
| 增量 | 绑定原执行的稳定键；同键同内容一次入账，异内容阻断核对 |
| 累计 | 保存来源/水位，只计可信增量，旧报告不退款，最终值不整体再加 |
| resume/fork/范围变化 | 证明覆盖范围、历史/子调用与基线；不能共用一个 last_usage |
| 缺失/冲突/无效 | 保留未知占用；负数、非有限数、错单位或无执行归属不变成零，更正另存审计 |

可信 adapter 归一化并固定 SDK/CLI/规则版本，一个来源入账、其他交叉核对。BudgetUsageReport 只接收已归一化值；final=True 必须证明启用维度收齐，而非“最后事件”。增量须无漏报，累计 turns/cost 各自水位，缺测不推进。

## 一个账户，多个执行预留

每个可加维度：remaining = limit - consumed - reserved。预留前不启动，余额不足/账本不可用则拒绝；reserved 包含未知占用，确认用量原子转 consumed，不双算。实际超支保留真实差额并阻断新分配，不截断数字。活动时长按区间另算。

### 一个例子：已用、占用与可用

以下是目标账户，不是当前 dispatch 输出，假设已验证 20 turns 的共享硬上界：

| 时点 | 已用 | 占用 | 可用 |
| --- | ---: | ---: | ---: |
| 前一段结算 | 4 | 0 | 16 |
| 主执行预留 6 | 4 | 6 | 10 |
| 子 A/B 各预留 3 | 4 | 12 | 4 |
| A 用 2 且停止 | 6 | 9 | 5 |
| B 失联、主用 5 且停止 | 11 | 3 | 6 |

B 的 3 不因 lease 过期退还，新执行最多用 6；若不能证明 B 仍受 3 的上界约束，暂停整个账户分配。

### 持久账本的当前载体

[budget_store](../../PJM/backend/src/projectmind/runs/budget_store.py)、[repository_budgets](../../PJM/backend/src/projectmind/runs/repository_budgets.py)与 [0030](../../PJM/backend/migrations/versions/0030_run_budget_ledger.py)已有以下内部载体：

| 表 | 责任 |
| --- | --- |
| run_budget_accounts | Run 唯一，策略/限额 checksum、已用/占用、阻断与版本 |
| run_budget_reservations | Run/Segment/Attempt、操作组/执行键、parent、lease hash、授予量与启动/结算事实 |
| run_budget_receipts | 原预留+回执键去重，追加用量、停止、未启动释放或核对失败；不依赖 RunEvent |

内部 run-budget/v1 只定义 turns/可选成本，非 HTTP 协议。成本为 nano-USD，DTO 整数、列 Numeric(38,0)，计算先转整数、存储再转 Decimal；边界拒绝 float/不足最小单位，不静默舍入。旧数值须经版本化转换。

new_budget_account 仅构造未保存/未开始且限额匹配的新账户；未来与 Run 同事务按 FK 顺序保存。0030 不回填旧 Run、不启动核对；执行端不能补造零余额。

### 内部状态不能当作运行证明

| 状态 | 含义 |
| --- | --- |
| RESERVED | 已占用、无启动意图；未启动释放同时关闭启动资格 |
| START_INTENT | 可能已开始；重复读取不许可重启，未知占用继续保留 |
| SETTLED | 已提供停止依据和完整用量并释放未用量，不等于 Run 成功 |
| RELEASED | 未启动预留已关闭，不是启动后的退款 |

verified_evidence 目前只校验键形状，claim_reconciliation 只操作内部 lease。二者不验证真实停止、不建立核对方身份；须接受信服务、锁外证据校验和来源验证，不能开放给模型/用户/旧 Worker。

### 原子性与重复请求

预留同事务验证状态/权限/余额/身份。所需锁顺序为 Run → Segment → Attempt → 账户 → 预留，可跳不需要层，不得反向取锁；获锁后再判当前时间/fencing，锁外调用模型和核对。

同操作同键返回原记录，异内容冲突；真正重执行用新身份取余额。回放 Tool 结果不重调 Provider，但新模型执行仍计量。全组 dispatch 原子预留，全部获准才逐支启动，之后各自结算。

主执行未用预留不能复制给子支。主模型等待 Tool 不表示已停止或缩额；现有 adapter 无中途缩额协议时，只从未承诺余额分配，不足拒绝。转移额度须先证明主执行停止且局部上限同步收窄。

### 启动与结算的提交边界

```text
TX A：执行权 + 原子预留，确认原键
  → TX B：重验执行权 + START_INTENT
  → 锁外：带执行身份/局部上限调用 adapter
  → TX C：去重核对，结算或保留占用
```

DB 与模型不能原子启动。A 响应丢失查原键，不换键扣额；B 后崩溃/响应丢失可能已收费，只在证明未开始且不能再开始时释放。

当前 store 的 A 在 repository 返回后遇提交错误会新 session 按原组确认，仍要求有效 lease。B 仅首次明确提交返回 True；已有意图返回 False，提交未知抛 BudgetStartUncertainError，均不许可再次启动。C 冲突以结果返回并先提交阻断事实，不能改抛异常使审计回滚。repository.flush 不等于 commit。

B 后仍有取消窗口，需运行监督与硬上界。执行身份包含原 Tool/branch/重执行，不仅 branch_key；Attempt 接管不洗掉旧未决预留。

## 结束、取消与故障恢复

| 场景 | 处理 |
| --- | --- |
| 正常结束 | 幂等结算可信用量，只释放已证明未用部分 |
| 取消/timeout/等待 | 结算已知量；停止/未用尚不确定继续占用，interrupt 不代表停止 |
| 崩溃/丢报告/lease 失效 | 保留待核对，不全额退款 |
| 超预留/不可计量 | 记录真实消耗并阻止新收费执行 |
| 终态后晚到 | 独立追加核对，不重开 Run、不在末次 RUN_SNAPSHOT 后加事件 |

### 执行权与结算权分开

旧 Worker 无权启动、续期、增额或释放；可信核对方可按原执行身份、来源/水位和停止证据接受晚到报告，不恢复旧执行权。停止与结清分别证明，Session CLOSED/branch outcome/Run 终态不能清零占用；部分证明只结算该部分，不增手工退款绕行入口。

## 兼容、公开信息与实施顺序

旧 Run 无可靠账本时标累计未知，不从 Session 摘要回填零；旧非终态能可靠重建才续行，否则停止收费并明确新建。max_output_bytes 保持单响应含义；公开上限/已用/占用/待核对量取服务端投影，Brief 余额也只是带时点观察。

### 上线门禁与接线顺序

先固定计量契约/局部强制，再验证真实 DB 并发、未知提交与受信核对方；随后把创建重放、主子执行、等待/取消/重试全部接同一账户，最后开放投影、三语与新策略。缺一入口不回退完整上限。

切换前隔离不识别策略的旧 Worker，处理在途；回退保留账户/预留/报告，不删账让旧执行继续。0030 任一表有数据时拒绝 downgrade 删表，空表才可删除；不据此假定已具备完整恢复。公开变化同步 DTO、Schema/example/OpenAPI 与消费者。

## 验收矩阵

覆盖连续主子分配、最后余额竞争、同键异内容、重复/乱序/缺测报告、resume 历史去重、A/B/C 提交丢响应、启动后崩溃/接管、晚到结算、混合 Worker/回退与旧 Run。分别证明真实事务、adapter 硬限额和 SDK 计量完整性；split_budget 或内部账本单测不代表 Run 级预算已生效。
