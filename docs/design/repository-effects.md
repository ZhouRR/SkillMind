# 受控外部写入与代码仓库回写

本页负责 Proposal、批准、外部写入与恢复的语义；[Runtime](agent-runtime.md)负责 Run 续行，[认证](authentication.md)负责 actor/Project，[Runbook](../operations/runbook.md#87-incident-と-recovery)负责现场处置。代码阅读从 [Backend 接线](../../PJM/backend/README.md#承認から外部変更まで追う)进入。

状态：Provider 与审批链路已有实现；下文区分现行行为和待实现的可靠性要求。2026-09-08 核对依据为工作副本，不能把本地测试通过解释成真实系统的并发、幂等与恢复均已验收。进度统一见[计划 §13](../planning/roadmap.md#13-当前执行状态)。

## 先分清四种事实

例子：用户批准把 `src/config.txt` 改成提案中的全文。Git push 和回读成功，随后 PR 请求超时。此时“用户已批准”和“远端已有 commit”可以同时为真，“PR 是否创建”和“平台是否保存完成结果”仍可能未知。再点批准、重启 Worker 或把 Run 取消，都不能撤销这个 commit。

| 对象 | 怎样理解它的状态 |
| --- | --- |
| ChangeProposal | 冻结的具体变更；`APPROVED` 是获准执行，不是外部写入完成 |
| ChangeApproval | 对精确 version/checksum 的一次批准或拒绝，不授予其他写权限 |
| EffectExecution | `APPLIED` 是平台已保存 Provider 完成结果；`FAILED` 不证明外部未发生变更 |
| Run / Evidence | 分别描述分析流程与审计依据；Run 的终态不能单独证明外部写入结果 |

`VERIFICATION_FAILED` 属于 EffectExecution，不是 Proposal 或 Run 的新状态；Effect 的 `attempt_no` 也不是 RunAttempt。`before_ref / after_ref` 可能为空，仓库的 before Evidence 当前记录基线定位，不是自动生成的完整逆向补丁。原始 enum 见 [domain](../../PJM/backend/src/projectmind/effects/domain.py)，公开形状见[契约入口](../../PJM/contracts/README.md#外部変更の契約を読む)。

Result 中模型填写的 effects 摘要不替代上述平台记录。摘要的引用与状态目前尚未逐项核验，收口规则见[结果引用的可信性](results-evaluation.md#引用可信性的修正要求)；不从一段 APPLIED 文本推导已有批准、真实写入或 read-back。

## 调用与批准链路

```text
观察 → Evidence → change.propose/v1
  ↓
冻结提案并等待批准（或命中精确预授权）
  ↓
TX 1：保存批准、执行记录与 Outbox
  ↓
TX 2：校验提案/绑定、认领执行权
  ↓
事务外：凭据 → 检查 → 写入 → 回读
  ↓
Git branch：可选 PR/MR
  ↓
TX 3：结果/证据、事件与后续调度
```

Agent 只能调用提案工具，`repository.write/v1` 不进入它的直接 Tool 集合。平台以 [EFFECT_CAPABILITIES](../../PJM/backend/src/projectmind/effects/catalog.py) 统一决定 Provider、提案校验器和预授权资格。

以上不是一个跨数据库、仓库和 forge 的原子事务。数据库阶段按 Run → Segment → Proposal → Effect 顺序取得所需行锁，远端 I/O 在事务外。人工拒绝不创建可执行的 Effect；批准后 Run 仍可处于 `WAITING_FOR_APPROVAL`，此时由独立 Effect Worker 工作，不表示还需要再批准一次。

无取消且有效 finalize 后，平台把 apply 结果或不可重试失败交给新的 RunSegment；临时失败则在原 Segment、原 Proposal/Effect 下重新调度。Effect 的技术重试不能冒充用户提出了新需求。

## 已注册写入

| capability | Provider | 自动批准条件 | 前置与验证 |
| --- | --- | --- | --- |
| `issue.update/v1` | Redmine CAS adapter | 仅 system ADMIN 明确配置的 LOW 风险、精确 scope 策略 | discovery → 前置 revision → adapter 条件写入 → 回读 |
| `repository.write/v1` | Git、SVN | 不可预授权，始终人工批准 | 目标约束 → 提交 → remote 回读；严格 CAS/恢复差距见下文 |

标准 Redmine REST 接口不自动具备平台需要的 CAS（比较版本一致才更新）与幂等语义；未通过版本化 discovery 时不能 apply。discovery 声明也不能代替 adapter 的真实竞争验收。审批时重新校验 actor、Project、Proposal version/checksum、绑定和 Integration 状态；批准不能扩大最初 scope。当前人工决策只允许 Run 发起人或本组织 system ADMIN；普通 ProjectMember 不因能浏览提案就取得批准权。

## 仓库写入模式

| `write_mode` | 目标 | 约束 |
| --- | --- | --- |
| `direct`（默认） | Integration 配置的默认分支/目标 | Git 使用普通 fast-forward push，不 force；SVN 写绑定 URL。不能从这些命令推导完整的精确 revision CAS |
| `branch` | `projectmind/` 保留命名空间分支 | 设计要求只新建或重放本次原提交；SVN 落在 `<仓库根>/branches/projectmind/`。现有内容比较不足以证明归属 |

两种模式都须先批准。当前只有 Git branch Provider 接入 GitHub/GitLab forge transport；SVN 不开 PR/MR，direct 也不调用 forge。未配置 forge 时 Git branch 只留下分支与 commit。PR 失败不会撤回已经发生的提交，重试与部分成功按[阶段回执要求](#阶段回执与不确定结果)处理。

现行模式校验来自 [repository_write.py](../../PJM/backend/src/projectmind/effects/repository_write.py)，落地行为来自 [repository_effect.py](../../PJM/backend/src/projectmind/effects/repository_effect.py)。后者部分旧注释仍写“仅 Git 新分支”，不能据此覆盖 direct/SVN 实现；代码注释和旧契约的同步纳入后续工作，不重新禁止已有模式。

## 变更载体和冻结内容

提案保存目标 binding、capability、operation、风险、依据 Evidence、前置 revision 和变更预览。仓库载体为逐文件全文 `SET` / `REMOVE`；不是允许模型直接执行任意 unified diff 或 git/svn 命令。

Provider 使用独立临时可写副本，复用 [repository client](../../PJM/backend/src/projectmind/agent/repository_client.py) 的凭据和命令边界。它不能把 Agent 的只读 `input/` 当作提交工作副本。

### 契约与幂等身份

| 边界 | 当前载体 | 消费方 |
| --- | --- | --- |
| Agent 提案 | [change.propose request](../../PJM/contracts/tools/change.propose/v1/request.schema.json)：target、changes、precondition、Evidence 等 | Proposal validator 与冻结记录；capability 专属 validator 继续收窄可用操作 |
| 人工决策 | [effects route](../../PJM/backend/src/projectmind/api/routes/effects.py) 的 DecideProposalRequest、`Idempotency-Key` | RunService 与决定事务；不是 repository.write 的直接调用 |
| 已批准执行 | ClaimedEffectExecution 内部快照 | Effect registry / Provider；不公开凭据、config 或 lease |
| 展示与审计 | [Run detail](../../PJM/contracts/runs/detail/v1.schema.json)、decision response | Web validator 与 RunResultPanel；不从审批按钮推测执行结果 |

原提案的幂等键、人工决策请求的幂等键、创建 Run 的幂等键是三种身份。决策重放要求原 key 和相同 proposal/version/checksum/decision/reason；只重复同一 checksum 不够。远端执行沿原 Proposal/Effect 身份重试，不因新 Worker 或新的队列投递换键。

现有 [repository.write request Schema](../../PJM/contracts/tools/repository.write/v1/request.schema.json)仍限定 `projectmind/` branch，并保留“绝不写默认分支”的旧描述；它不是当前 Effect Worker 的输入，也不能作为 direct 模式的开发示例。后续需审查它与通用提案/Provider 的消费者、版本及历史兼容，再同步契约和测试。本次不改 Schema，也不把该旧 Schema 注册为可直接调用的 Agent Tool。

## 并发、重试和失败

| 情况 | 当前处理与解释边界 |
| --- | --- |
| Proposal 版本/checksum 不匹配 | 拒绝审批旧预览，重新取得待审版本 |
| Provider 判定目标陈旧/冲突 | `STALE / target_stale` 或不可重试的 `FAILED / target_branch_conflict`；不自动修订已批准内容 |
| 可重试 transport 异常 | 记录错误，再置为 `REQUESTED` 并经 Outbox 重进整个 Provider；不是从失败指令续跑。超过次数、取消或提案过期时不继续自动 apply |
| 回读内容不匹配 | `VERIFICATION_FAILED`，不可自动重试；外部可能已经改变，before/after 未必已保存 |
| 回读请求断连或 commit 后 PR 失败 | 按 transport 分类；可重试错误会重新进入整个 Provider。当前没有独立持久的“commit 已完成，待 PR”阶段 |
| apply 前发现取消 | claim 拒绝调用 Provider；lease 已取得之后还有并发窗口，不能承诺请求到达即停写 |
| Provider 期间取消 | finalize 能取得有效 lease 时先保存外部结果，再以取消终态结束 Run；不做逆向写入 |
| 需要撤回 | 由管理员审查后执行新的 revert/修正流程；当前没有自动回滚服务 |

提案中的 `reversible / rollback` 是可逆性与修正说明，不保证实际变更必定可撤回，也不表示平台已有一键撤回。是否能修正必须依据实际远端 revision 与已有审计重新审查；不能因为声明可逆就省略批准。禁止 force push、自动合并 PR、Agent 自批、repository 免审、跨仓库原子写入和无 scope 更新。

## 可靠性修正要求

以下为基于 2026-09-08 源码核对的差距与目标，不是已复现的生产事故，也不是已交付的恢复能力。保持人工批准、最小 scope 和注册 Provider 边界，不以降低规则来消除差距。

### 执行身份与远端前置条件

当前 Git 的 `_matches`、SVN 的 `_svn_matches` 只比较变更路径的期望内容；Redmine 在 revision 不同但目标字段相同时直接返回 `replayed=True`。这证明“现在的值相同”，不能证明“原幂等身份已经执行”。Git commit 虽带 Proposal ref，重放判断并未验证它；同名分支可能属于另一提案。

目标要求：

- 原提交的重放必须同时证明 Effect/请求身份、目标、批准基线与实际 revision/变更集。只匹配期望状态时不再记作本次已执行；能确认未执行则拒绝陈旧基线，仍未知则先对账。历史不完整记录保持原样，不补造 receipt 或改旧 Evidence。
- Git direct 的预读与普通 push 不是一个原子精确比较；branch 的“先查不存在，再 push”也不能排除其间新建分支。远端必须在更新点保证预期旧 ref 或不存在条件；禁止 force/覆盖仍生效。仅再加一次 GET 或本地锁不足以解决竞争。
- SVN `commit_files` 当前 checkout 未带冻结 revision，read-back 逐文件读取当前 URL，返回 revision 也来自事后查询。后续必须明确 direct/branch 各自的预期基线、提交回执和固定 revision 的回读；不能以 checkout 后的 out-of-date 检测证明 checkout 前的变化已被拒绝。

### 阶段回执与不确定结果

当前仅在整个 Provider 成功返回 `EffectProviderResult` 后才保存 before/after，中间事实尚未独立持久化：

- Git：forge 调用发生在 commit 回读之后，异常不会携带已经确认的 commit。
- SVN：branch copy 与文件 commit 是两次远端变更。copy 后失败会留下分支，再次运行可能被内容冲突拒绝。
- [forge](../../PJM/backend/src/projectmind/effects/forge.py)：先找同 source 的首个 open PR，尚无本地的 target/提案归属复核或并发创建冲突恢复。

目标是在一个 Effect 身份下追加阶段事实：调用前持久化原请求身份，调用后记录可验证的远端回执与读取结果；DB 与远端之间仍可能断连，不能宣称因此实现原子提交。区分“明确未执行”“已执行待验证/待 PR”“执行结果未知”，恢复先查原身份，只补尚未发生的阶段。PR/MR 的复用还要匹配仓库、source、target 和提案身份；关闭、合并、并发创建与返回丢失分别处理，不只依赖首个列表项。

这些是语义要求，尚未冻结新的 DB/Provider/public Schema。实施时同步 migration、repository、Provider port、事件/详情与 Web；旧 record 没有阶段事实时明确未知，不以空值推导未执行。运维不能用手工 SQL、删 Effect 或更换 key 代替恢复协议。

### 执行权与取消

当前 Effect Executor 是 claim → apply → finalize，没有贯穿 Provider I/O 的 heartbeat/取消监督。过期恢复会重新派发，而旧 Provider 可能尚未结束；数据库 lease 只能阻止旧执行者正常 finalize，不能撤销它已发出的远端请求。claim 的到期时间由 service 在取锁前计算，等待期间的时间消耗也必须纳入后续修正。

后续需在取锁后用新时刻建立/验证 lease，监督各阶段的执行权，并在新的外部步骤前重验取消与有效批准。失去 lease 或请求超时不代表远端未写入；先保留和确认原身份结果，再决定是否继续，不能直接让第二个 Worker 重写。read-back 对账与新外部写入分开，取消之后仍要保留已经发生的事实。不能用加长单次命令 timeout 或复用 Run 的准备监督测试证明此链路可靠。

## 验收

| 场景 | 必须观察到的结果 |
| --- | --- |
| 审批响应丢失、重复点击或修改 reason | 原决策可确认，新的内容不能借原 key 重放；不新增批准/Effect。Web 要保留已发送内容/键，见[审批界面](workspace.md#审批请求与执行结果) |
| 不同提案写出相同内容、目标在预读后变化 | 不误认原提交；精确前置条件拒绝未批准基线，不覆盖他人 branch |
| SVN copy 后中断、提交后目标再次变化 | 原身份对账，不把遗留分支认作新建成功；回读对应准确提交 revision |
| commit 成功后 PR 超时、PR 并发创建或目标不同 | commit 不重做；确认正确 PR，未知/部分成功明确保存 |
| Provider 慢于 lease、取消、旧 Worker 晚返回 | 不凭过期允许第二次副作用；旧执行者不能发布新的完成事实，已发生的事实可审计 |
| 外部已应用但 DB finalize 响应丢失 | 先确认原平台记录与远端回执，不换键、不重建 Effect，不臆造成功或未执行 |

现有[Provider 测试](../../PJM/backend/tests/effects/)、[Worker 测试](../../PJM/backend/tests/worker/test_effect_executor.py)与[审批事务测试](../../PJM/backend/tests/runs/test_effect_decision_service.py)是接续入口：本地临时 Git/SVN、fake transport 或 mock DB 各只证明各自范围。真实 DB/远端/forge/Redmine 使用专用目标做竞争和故障注入；批准 UI 的网络恢复也需真实浏览器回归。没有这些证据前，不把上表写成“全部通过”。
