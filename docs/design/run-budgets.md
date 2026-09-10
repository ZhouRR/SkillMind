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

### 原始用量观察入口

[Engine](../../PJM/backend/src/projectmind/agent/engine.py)提供服务装配专用的可选 `usage_observer`，在原 SDK Result 的任何展示事件交出前等待观察处理；普通 usage/rate-limit 通知不进入此口。观察失败关闭该执行的成功/等待输出，取消继续传递，不启动后台补写。

[内部观察类型](../../PJM/backend/src/projectmind/agent/metering.py)固定本次 adapter invocation、Project/Run/Attempt/actor、SDK Session、INITIAL/RESUME/FORK 与构建 SDK/CLI 版本。指令取实际 prompt 的 checksum；options 仅保存最终传给 client 的模型、局部限额、Session/续行与输出格式摘要，不含凭据、任意 usage 或结果正文，不冒充完整 options 审计。相同 invocation 的 Result 使用固定观察键，内容变化不能改键逃避核对；同 Session 的再次 resume 则是另一 invocation。

整数、缺失和无效值分开；有限非负成本 float 只保存其 binary64 hex 表示，不转换为精确 USD/nano-USD。该观察不是 `BudgetUsageReport`，不声明累计范围、final 或 stopped。流关闭、未收到 Result、观察提交不明也不补零或退款。

原始观察已有独立持久入口，绑定和启动规则见[原调用绑定](#原调用绑定与启动)。当前 startup 尚未注入该服务；接入前仍须由可信协调方验证计量 profile、构建版本和实际局部强制，再归一化到现有报告。离线 compatibility probe 只核对包的版本标记和接口；SDK 可回退到系统 CLI，不能据此证明实际启动的二进制、resume 用量范围或硬上界。不能从 trace、Session 或 invocation ID 补造预算身份，这个入口本身不代表共享限额生效。

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

[budget_store](../../PJM/backend/src/projectmind/runs/budget_store.py)与 [repository_budgets](../../PJM/backend/src/projectmind/runs/repository_budgets.py)复用 [0030](../../PJM/backend/migrations/versions/0030_run_budget_ledger.py) 的三张账本表；[0035](../../PJM/backend/migrations/versions/0035_budget_invocations.py)追加调用绑定和原始观察，[0043](../../PJM/backend/migrations/versions/0043_budget_start_owner.py)追加启动所有权：

| 表 | 责任 |
| --- | --- |
| run_budget_accounts | Run 唯一，策略/限额 checksum、已用/占用、阻断与版本 |
| run_budget_reservations | Run/Segment/Attempt、操作组/执行键、parent、lease hash、授予量、原 invocation 绑定与启动/结算事实 |
| run_budget_receipts | 原预留+回执键去重，追加用量、停止、未启动释放或核对失败；不依赖 RunEvent |
| run_budget_observations | 原预留+原 invocation 的 SDK Result 观察；未归一化，不改变消耗、停止或结算事实 |

内部 run-budget/v1 只定义 turns/可选成本，非 HTTP 协议。成本为 nano-USD，DTO 整数、列 Numeric(38,0)，计算先转整数、存储再转 Decimal；边界拒绝 float/不足最小单位，不静默舍入。旧数值须经版本化转换。

new_budget_account 仅构造未保存/未开始且限额匹配的新账户；未来与 Run 同事务按 FK 顺序保存。0030 不回填旧 Run、不启动核对；执行端不能补造零余额。

### 原调用绑定与启动

预留的 invocation ID、完整描述子 JSON 和 checksum 必须同时为空或同时存在；一份 invocation 全局只绑定一个预留。描述子采用内部 `agent-invocation/v1`，不改变原预算 policy/request/group 的 hash，也不是公开 API。

| 边界 | 必须核对的原事实 |
| --- | --- |
| 绑定 | 有效 Run/Segment/Attempt lease、无取消、原 actor/Project/Run；首次仅 RESERVED，实际 max_turns 不超过授予量 |
| 启动 | 显式传原 invocation ID/checksum，并重验完整保存值、scope 和当前执行权；仅首次确认的 START_INTENT commit 允许调用 client |
| 观察 | 独立核对 lease 的 worker/token/到期世代、原预留与完整 invocation；重复同内容不重复写，异内容保留原值并先提交账户阻断及冲突审计 |

原观察使用 `sdk-result-observation/v1`，不写成归一化 USAGE 回执。同一次 invocation 的观察冲突不能换键重收，也不允许成功事件掩盖冲突；未知提交保留占用。终态后可按独立核对权保存，不恢复旧 Attempt 执行权或追加 RunEvent。

[BudgetInvocationRecorder](../../PJM/backend/src/projectmind/runs/budget_execution.py)把绑定、严格启动和观察保存接到 Engine 的 `before_connect` / `usage_observer`。两者成对装配；只有明确的 `True` 才创建 client，拒绝、提交未知或取消不得继续启动。依赖收尾捕获取消也不能据此获得下一副作用的许可；client 已创建后由同一执行负责清理。

可信协调方可传 `prepared_invocation` 固定原 SDK Session/调用身份；Engine 从实际 prompt/options/模式重建后逐字段核对，不把原描述子本身当许可。读到 RESERVED 也不能证明此前没有尝试 B。

### 启动所有权与协调方更替

原调用描述回答“是哪次执行”，启动所有权回答“哪个存活的协调器可以尝试 B”，两者分开。每个 recorder 自行生成随机 token，在首次 await 前从实例取走并关闭复用；局部变量仅供本次绑定/B 调用。首次绑定事务同时保存独立用途的 token hash。token 不进入调用描述、返回 DTO、SDK options、观察或公开接口，也不从 Worker lease、Session 或 invocation ID 推导。

| 恢复场景 | 必须保持的判定 |
| --- | --- |
| 原绑定事务响应丢失 | 只用原描述与原 token 确认，未保存时不补写 |
| 原 recorder 的 B 成功 | 仍须首次明确 commit 才允许 client；本地许可只使用一次 |
| B 结果未知，行仍是 RESERVED | 原 recorder 不重试；新 recorder 的 token 不匹配，不能重新取得许可 |
| 已有 START_INTENT | 读回不许可再次启动；换协调方也不能继承原 token |
| 旧绑定没有所有权 | 保留原值并拒绝启动，不为旧调用补造 owner |

所有权检查与原 lease、actor、绑定和父调用检查一起执行，不替代任何一项。它阻止重新装配 recorder 把未知启动变为新许可，不证明模型已开始/停止或费用已结清，也不防止任意受信代码复制内存 token 后绕过 recorder。store/repository 仅供受信装配；未完成的独立核对服务不能直接开放这些入口。

丢失 token 后仍保留占用；独立核对方依据原执行事实处理未启动关闭或停止/计量，不把核对 lease 当启动权。0043 对旧行保持空值，任何 owner 痕迹都在排他锁内阻止降级；旧预算调用方不检查此列，须隔离。恢复到较早的数据库状态也不能证明后来没有启动。

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
  → 绑定事务：固定原 invocation + 实际参数，确认原值
  → TX B：重验执行权 + 原绑定 + START_INTENT
  → 锁外：带执行身份/局部上限调用 adapter
  → TX C：去重核对，结算或保留占用
```

DB 与模型不能原子启动。A 响应丢失查原键，不换键扣额；B 后崩溃/响应丢失可能已收费，只在证明未开始且不能再开始时释放。

当前 store 的 A 在 repository 返回后遇提交错误会新 session 按原组确认，绑定事务同样只确认原描述子、不补写缺失绑定，二者仍要求有效 lease。B 仅首次明确提交返回 True；已有意图返回 False，提交未知抛 BudgetStartUncertainError，均不许可再次启动。C 冲突以结果返回并先提交阻断事实，不能改抛异常使审计回滚。repository.flush 不等于 commit；锁内最后一次等待后的 fencing 也不等于精确 commit 时刻的 lease 保证。

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

切换前隔离不识别策略或原调用绑定的旧 Worker，处理在途。0035 不给旧预留补造身份，旧未绑定 START_INTENT 不允许补绑后重启。回退保留账户/预留/报告：0035 在排他锁内检查，有任一绑定或原观察时拒绝删除；0030 也有非空拒绝，但仍须停写，不能把局部迁移 guard 当全链并发回退或完整恢复证明。公开变化同步 DTO、Schema/example/OpenAPI 与消费者。

## 验收矩阵

覆盖连续主子分配、最后余额竞争、同键异内容、重复/乱序/缺测报告、resume 历史去重、A/B/C 提交丢响应、启动后崩溃/接管、晚到结算、混合 Worker/回退与旧 Run。分别证明真实事务、adapter 硬限额和 SDK 计量完整性；split_budget 或内部账本单测不代表 Run 级预算已生效。
