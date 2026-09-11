# Run 预算与执行限额

主 Run 的账户创建、剩余额度预留与 Executor/Engine 回调已有可装配路径，生产 startup 尚未启用；可信计量、停止与结算仍待闭合。目标是主/子执行、Segment 续行和 Attempt 重试共用账户。本文不是预算 API，首版接线范围见[计划 R02](../planning/roadmap.md#开发任务)。

## 先区分上限、分配与消耗

上限是冻结授权，预留是承诺额度，消耗是确认用量；分配、Session 数、摘要长度都不是消费。

### 当前实现的实际口径

| 限制 | 当前边界 |
| --- | --- |
| max_turns | SDK 每次执行上限，不累计所有主子 Session/重试 |
| max_budget_usd | 可选 SDK 局部限制；普通创建快照未设置，子 context 原值继承、不按支拆分 |
| max_output_bytes | Gateway 单个序列化 Tool response 的 UTF-8 字节上限，不是累计输出/磁盘配额 |
| 子分析 budget | 每次 dispatch 重拆父冻结 turns/output 上限；连续调用未扣共享余额 |
| RUN_MAX_ATTEMPTS | 当前 Segment 的技术重试次数，不限制全部业务续行或 Run 成本 |
| 输入文件数量/字节 | 逐根及跨根最终存量，规则见[资源总量](resource-snapshots.md#跨根总量) |

父 20 turns 连续两次 dispatch 两支，每次仍各分 10，并非全 Run 20。生产 startup 与子 Provider 尚未接入账本；主路径的装配规则见下文。

### 用量现在流向哪里

| 来源 | 当前保存方式与缺口 |
| --- | --- |
| SDK mapper | usage 与 rate-limit 通知都可为 USAGE_UPDATED，Result 终端/等待另带 turns/cost；不能逐事件相加 |
| 主 Executor / Session | Result 按 key 更新，Session usage 最新替换、cost 保存最近总值；无统一来源/去重账本 |
| 取消覆盖事件 | 保留该事件已观察 usage/cost，丢弃成功/提问正文；SESSION_INTERRUPTED 不更新 Session usage，不能从事件反推相同投影 |
| 子收集器 / Session | 校验终端和结果，但不归集用量；子 usage 仅 branch_key、cost 空，失败也不等于零消耗 |

入账前须确认历史/重试/子调用范围，Session、Result、最终报告不可重复计量。

### 原始用量观察入口

[Engine](../../SKM/backend/src/skillmind/agent/engine.py)的可选 `usage_observer` 在原 SDK Result 展示前同步处理；普通 usage/rate-limit 不进入。失败阻止成功/等待输出，取消传播，不后台补写。

[观察](../../SKM/backend/src/skillmind/agent/metering.py)固定 invocation、Project/Run/Attempt/actor、SDK Session、续行模式、SDK/CLI 构建版本与实际 prompt checksum。options 仅记录实际模型、局部限额、Session/续行/输出格式摘要及已核对随包 CLI 的 `cli_checksum`，不含路径、凭据或结果，不冒充完整 options 审计。同 invocation 固定观察键，异内容不得换键；再次 resume 是新 invocation。旧记录缺少 CLI checksum 时保留原 JSON/hash，不补入当前文件身份，也不据此恢复旧启动许可。

整数、缺失、无效分开；有限非负 float 成本只存 binary64 hex，不转换为精确金额。观察不是 BudgetUsageReport，不声明累计范围/final/stopped；无 Result、流关闭、提交未知均不补零退款。

接入前，可信协调方须验证计量 profile、实际构建和局部强制再归一化。Run 与 Skill 解释显式指定固定 SDK 的随包 CLI，并核对实际 import 归属、分发版本、RECORD 中的文件大小及 SHA-256；缺失或漂移时拒绝，不回退系统 CLI。离线 probe 也检查文件身份，但不执行 CLI，不证明进程实际开始/停止、resume 范围或硬上界。依赖分发记录属于受信安装链，不是签名或远程证明；运行期间须保持 package 不可变。预算身份须经[绑定](#原调用绑定与启动)，不从 trace/Session/invocation ID 补造。

### 现有计时器的覆盖范围

默认值来自 [Settings](../../SKM/backend/src/skillmind/core/settings.py)、[创建快照](../../SKM/backend/src/skillmind/runs/service.py)、[Worker](../../SKM/backend/src/skillmind/worker/settings.py)，部署以注入值为准。

| 计时器 | 默认值与覆盖 |
| --- | --- |
| Attempt lease | 60 秒，可配 30–300；约每 1/3 续期，仅代表执行权 |
| 准备 timeout | 300 秒，可配 1–3600；包住 ContextBuilder 及其输入提交，不含 Brief/启动/终态事务 |
| 仓库单命令 | 120 秒，可配 5–600，不是所有根准备总时长 |
| 模型事件流 | 普通创建冻结 900 秒，准备后起算；只对下一事件 await 施加 deadline，不打断事件持久化 |
| 子分支 | 300 秒，可配 30–600，不赠送 Run 额度 |
| ARQ execute_run | 1200 + 准备秒数，默认 1500；整个 job 最终防线，其他 job/cron 独立 |
| 人工等待 | 持久 expires_at；释放主 lease，不刷新 Run 总额度 |

准备 240 秒加模型 850 秒不违反各自限制，但不是 Run 共计 900 秒。续 lease 不延长 deadline；timeout 为协作取消，I/O/清理/DB 可晚返回，job 关停可打断终态提交。Effect 有[独立监督缺口](repository-effects.md#执行权与取消)。

## 目标与非目标

重试/续行不重领额度，扩大上限新建 Run。不引入钱包/组织计费；Skill 解释、独立评估、模块构建另设限制。

### 分开定义计量维度

| 维度 | 必须冻结的口径 |
| --- | --- |
| turns | Adapter 定义新增 turn；不按 ToolCall/事件数代替，resume 历史不重复 |
| 成本 | 币种/精度/累计或增量/延迟规则；固定精度，不用 float 比余额 |
| 累计输出（后续） | 明确文本/结构化/Tool response 范围，DELTA 与 COMPLETED 不重复 |
| 活动时长（后续） | 累计墙钟区间，子并行重叠不相加，排队/人工等待不计，准备仍独立 timeout |

硬限额须有 adapter 强制上界。未启用不宣称受限，启用但不可计量则禁止新收费；null 不是零/免费。

### 计量报告如何归一化

| 报告 | 入账规则 |
| --- | --- |
| 增量 | 绑定原执行的稳定键；同键同内容一次入账，异内容阻断核对 |
| 累计 | 保存来源/水位，只计可信增量，旧报告不退款，最终值不整体再加 |
| resume/fork/范围变化 | 证明覆盖范围、历史/子调用与基线；不能共用一个 last_usage |
| 缺失/冲突/无效 | 保留未知占用；负数、非有限数、错单位或无执行归属不变成零，更正另存审计 |

可信 adapter 固定 SDK/CLI/规则版本，一个来源入账、其余交叉核对。BudgetUsageReport 只收归一化值；final=True 证明启用维度收齐，不指最后事件。增量无漏报，累计 turns/cost 各有水位，缺测不推进。

## 一个账户，多个执行预留

可加维度满足 remaining = limit - consumed - reserved。预留前不启动，余额不足/账本不可用则拒绝；未知仍占 reserved，用量原子转 consumed。超支记真实差额并阻断新分配，不截断；活动时长另按区间计。

### 一个例子：已用、占用与可用

假设目标账户已验证 20 turns 硬上界（非当前 dispatch 输出）：

| 时点 | 已用 | 占用 | 可用 |
| --- | ---: | ---: | ---: |
| 前一段结算 | 4 | 0 | 16 |
| 主执行预留 6 | 4 | 6 | 10 |
| 子 A/B 各预留 3 | 4 | 12 | 4 |
| A 用 2 且停止 | 6 | 9 | 5 |
| B 失联、主用 5 且停止 | 11 | 3 | 6 |

B 的 3 不因 lease 过期退还，新执行最多用 6；若不能证明 B 仍受 3 的上界约束，暂停整个账户分配。

### 持久账本的当前载体

[store](../../SKM/backend/src/skillmind/runs/budget_store.py)/[repository](../../SKM/backend/src/skillmind/runs/repository_budgets.py)使用 [0030](../../SKM/backend/migrations/versions/0030_run_budget_ledger.py) 账本、[0035](../../SKM/backend/migrations/versions/0035_budget_invocations.py) 调用/观察、[0043](../../SKM/backend/migrations/versions/0043_budget_start_owner.py) 启动所有权：

| 表 | 责任 |
| --- | --- |
| run_budget_accounts | Run 唯一，策略/限额 checksum、已用/占用、阻断与版本 |
| run_budget_reservations | Run/Segment/Attempt、操作组/执行键、parent、lease hash、授予量、原 invocation 绑定与启动/结算事实 |
| run_budget_receipts | 原预留+回执键去重，追加用量、停止、未启动释放或核对失败；不依赖 RunEvent |
| run_budget_observations | 原预留+原 invocation 的 SDK Result 观察；未归一化，不改变消耗、停止或结算事实 |

内部 run-budget/v1 定义 turns/可选 nano-USD，非 HTTP。DTO 为整数，列 Numeric(38,0)，计算用整数、存储用 Decimal；拒绝 float/不足最小单位，不舍入，旧值需版本化转换。

new_budget_account 只构造未保存/未开始、限额匹配的账户，须与 Run 按 FK 顺序同事务保存；迁移不回填旧 Run，执行端不补零。

### 主 Run 装配边界

受信装配向 RunService 注入已验证的 BudgetPolicy 时，新 Run 的 limits snapshot 同时冻结 `budget_policy` 与 turns 上限；仅 INSERT 胜者在同事务增加账户。重复请求返回原 Run，不能为旧 Run 补零账户。普通生产创建仍未注入 policy；当前主协调器拒绝启用费用维度，不能以只限制 turns 冒充费用上限。

[主协调器](../../SKM/backend/src/skillmind/worker/primary_budget.py)须同时装配到 Executor 与同一个 Engine 的启动/观察回调；回调不匹配在构造时拒绝，新版 Worker 缺少协调器也拒绝带预算标记的 Run。预留按原 Attempt 固定键，在 Run/账户锁内扣除已用及未决占用；确认提交只读回原授予量，不按新的余额重建。旧主执行未确认停止时不启动下一个主执行。

实际 SDK turns 上限收窄到授予量，Run 与 Segment Brief 的冻结授权上限保持原值；实际局部上限进入 Session options 审计与 invocation。一次启动、原观察保存和未知保留沿用下文协议。当前不把 Result、成功、等待、取消或流关闭转换为可信结算/停止，不自动释放未用额度；生产启用仍须满足计量及真实事务门禁，内部装配测试不证明它们。

### 原调用绑定与启动

invocation ID、完整 JSON/checksum 成组保存，一次 invocation 全局只绑定一份预留。内部 agent-invocation/v1 不改原 policy/request/group hash，不是 API。

| 边界 | 必须核对的原事实 |
| --- | --- |
| 绑定 | 有效 Run/Segment/Attempt lease、无取消、原 actor/Project/Run；首次仅 RESERVED，实际 max_turns 不超过授予量 |
| 启动 | 显式传原 invocation ID/checksum，并重验完整保存值、scope 和当前执行权；仅首次确认的 START_INTENT commit 允许调用 client |
| 观察 | 独立核对 lease 的 worker/token/到期世代、原预留与完整 invocation；重复同内容不重复写，异内容保留原值并先提交账户阻断及冲突审计 |

sdk-result-observation/v1 不是 USAGE 回执。冲突先保存阻断审计，不以换键/成功事件掩盖；未知保留占用。终态后可独立核对，不恢复旧 Attempt 或追加 RunEvent。

[Recorder](../../SKM/backend/src/skillmind/runs/budget_execution.py)成对装配 before_connect/usage_observer；只有首次明确 True 才创建 client，拒绝/未知/取消不许可启动，client 由原执行清理。捕获取消不能获得下一副作用许可。

prepared_invocation 可固定原 Session/调用，Engine 仍从实际 prompt/options/模式逐字段重建核对；描述子或 RESERVED 都不是启动许可。

### 启动所有权与协调方更替

描述子标识执行，owner 指定可尝试启动事务（TX B）的存活协调器。recorder 生成随机 token，首次 await 前取走并关闭复用，仅供本次绑定与启动；首次绑定同事务保存独立用途 hash。token 不从 lease/Session/invocation 派生，不进描述子、DTO、options、观察或 API。

| 恢复场景 | 必须保持的判定 |
| --- | --- |
| 原绑定事务响应丢失 | 只用原描述与原 token 确认，未保存时不补写 |
| 原 recorder 的 B 成功 | 仍须首次明确 commit 才允许 client；本地许可只使用一次 |
| B 结果未知，行仍是 RESERVED | 原 recorder 不重试；新 recorder 的 token 不匹配，不能重新取得许可 |
| 已有 START_INTENT | 读回不许可再次启动；换协调方也不能继承原 token |
| 旧绑定没有所有权 | 保留原值并拒绝启动，不为旧调用补造 owner |

owner 与原 lease/actor/绑定/父调用一起检查，阻止换 recorder 重获未知启动权；不证明开始、停止、结清，也不防受信代码复制内存 token 绕过 recorder。store/repository 仅供受信装配，不向未建身份的核对方开放。

丢 token 保留占用，由独立核对方按原事实关闭未启动或结算；核对 lease 不授启动权。0043 旧行留空，任何 owner 痕迹均在排他锁内阻止降级；隔离不识别它的旧调用方。恢复较早 DB 不证明后来未启动。

### 内部状态不能当作运行证明

| 状态 | 含义 |
| --- | --- |
| RESERVED | 已占用、无启动意图；未启动释放同时关闭启动资格 |
| START_INTENT | 可能已开始；重复读取不许可重启，未知占用继续保留 |
| SETTLED | 已提供停止依据和完整用量并释放未用量，不等于 Run 成功 |
| RELEASED | 未启动预留已关闭，不是启动后的退款 |

verified_evidence 仅验键形状，claim_reconciliation 仅管内部 lease；均不证明停止/核对方身份。须由受信服务锁外验来源/证据，不开放模型、用户或旧 Worker。

### 原子性与重复请求

同事务验状态/权限/余额/身份，按 Run → Segment → Attempt → 账户 → 预留取所需锁，不反向；锁后用新时间 fencing，锁外调用模型/核对。

同操作同键返原记录、异内容冲突，新执行以新身份取余额。Tool 重放不重调 Provider，新模型仍计量。整组 dispatch 原子预留，全部获准再逐支启动/独立结算。

主模型等 Tool 不释放预留；无中途缩额协议时，子支只用未承诺余额，不足拒绝。转移须先证明主执行停止并同步收窄局部上限。

### 启动与结算的提交边界

```text
TX A：执行权 + 原子预留，确认原键
  → 绑定事务：固定原 invocation + 实际参数，确认原值
  → TX B：重验执行权 + 原绑定 + START_INTENT
  → 锁外：带执行身份/局部上限调用 adapter
  → TX C：去重核对，结算或保留占用
```

DB/模型不原子。A 提交错误在 repository 返回后用新 session 按原组确认，绑定同样只确认、不补缺失值，均要求有效 lease。B 首次明确 commit 才返回 True；已有意图为 False，未知抛 BudgetStartUncertainError，均不重启。C 冲突先提交阻断审计，不抛异常回滚它；flush/fencing 不等于 commit 瞬间保证。

B 后崩溃/丢响应可能已收费，只有证明未开始且不能再开始才释放；取消窗口仍靠监督/硬上界。身份包括原 Tool/branch/重执行，接管不清旧未决预留。

## 结束、取消与故障恢复

| 场景 | 处理 |
| --- | --- |
| 正常结束 | 幂等结算可信用量，只释放已证明未用部分 |
| 取消/timeout/等待 | 结算已知量；停止/未用尚不确定继续占用，interrupt 不代表停止 |
| 崩溃/丢报告/lease 失效 | 保留待核对，不全额退款 |
| 超预留/不可计量 | 记录真实消耗并阻止新收费执行 |
| 终态后晚到 | 独立追加核对，不重开 Run、不在末次 RUN_SNAPSHOT 后加事件 |

### 执行权与结算权分开

旧 Worker 不得启动/续期/增额/释放。可信核对方按原身份、来源/水位、停止证据收晚到报告，不恢复执行权；Session CLOSED、branch outcome、Run 终态均不清零占用。部分证明只结部分，不设手工退款绕行。

## 兼容、公开信息与实施顺序

旧 Run 无账本标累计未知，不从 Session 回填零；非终态须可靠重建才续行，否则停止收费、明确新建。max_output_bytes 仍指单响应；公开余额取服务端投影，Brief 余额带观察时点。

### 上线门禁与接线顺序

接入 Run 级预算时，按计量/局部强制 → 真实 DB 并发/未知提交/受信核对 → 本期开放的执行入口共用账户 → 公开投影推进。首版先接主执行及其等待/取消/重试；子分析启用前再接共享预留/结算，未纳入的入口须服务端拒绝，缺入口不回退完整上限。累计输出/活动时长按后续范围推进，不是每次局部限额修改的附加任务。

切换前隔离旧 Worker 并处理在途；旧未绑定 START_INTENT 不补绑重启。0035 任一绑定/观察、0030 非空账本均阻止破坏性降级，0043 另验 owner；仍须全实例停写，不以 migration guard 代替恢复证明。公开变化按[契约流程](../development/contract-workflow.md)同步。

## 验收矩阵

覆盖连续主子分配、最后余额竞争、同键异内容、重复/乱序/缺测报告、resume 历史去重、A/B/C 提交丢响应、启动后崩溃/接管、晚到结算、混合 Worker/回退与旧 Run。分别证明真实事务、adapter 硬限额和 SDK 计量完整性；split_budget 或内部账本单测不代表 Run 级预算已生效。
