# 受控外部写入

本页负责提案、批准、远端效果与恢复。Provider/审批链已有实现，可靠性缺口见后文及[计划 R08](../planning/roadmap.md#r08-外部效果)。Run 续行见 [Runtime](agent-runtime.md)，现场处置见[Runbook](../operations/runbook.md)。

## 先分清四种事实

用户批准修改文件，Git push/回读成功，PR 请求超时：批准和 commit 可已成立，PR 与平台最终保存仍可能未知；重启、再批准或取消 Run 都不撤销 commit。

| 对象 | 状态含义 |
| --- | --- |
| ChangeProposal | 冻结变更；APPROVED 是获准，不是已写入 |
| ChangeApproval | 对精确 version/checksum 的决定，不扩其他权限 |
| EffectExecution | APPLIED 是已保存 Provider 完成结果；FAILED 不证明远端未变，VERIFICATION_FAILED 属于此对象 |
| Run / Evidence | 分别是分析流程与审计依据，不代替远端执行事实 |

Effect attempt_no 不是 RunAttempt。before_ref/after_ref 可空，仓库 before Evidence 仅定位基线，不保证逆向补丁。Result 中模型 effects 摘要尚未逐项核验，不能替代平台记录，见[结果引用](results-evaluation.md#引用可信性的修正要求)。

## 调用与批准链路

```text
observe → Evidence → change.propose → 精确批准/允许的预授权
  → TX 1：批准 / Effect / Outbox
  → TX 2：提案与 binding 校验 / claim
  → 锁外：凭据 / 前置检查 / 写入 / read-back / 可选 PR
  → TX 3：结果 / Evidence / 事件 / 后续调度
```

数据库所需锁按 Run → Segment → Proposal → Effect；不持锁远端 I/O，不宣称跨 DB/仓库/forge 原子。拒绝不产生可执行 Effect；批准后 Run 仍可 WAITING_FOR_APPROVAL，但已由独立 Effect Worker 工作。

有效 finalize 后，结果或不可重试失败交新 Segment；临时失败在原 Segment/Proposal/Effect 重调度，不制造新业务需求。Agent 只提案，不直接调用 write；[EFFECT_CAPABILITIES](../../PJM/backend/src/projectmind/effects/catalog.py)统一 Provider/validator/预授权资格。

## 已注册写入

| capability | 批准与执行边界 |
| --- | --- |
| issue.update/v1 | Redmine CAS adapter；仅 system ADMIN 配置 LOW 风险精确 scope 可预授权，discovery → 前置 revision → 条件写入 → 回读 |
| repository.write/v1 | Git/SVN，始终人工批准，不 force；精确 CAS/恢复仍有缺口 |

标准 Redmine REST 不自带本协议的 CAS/幂等，未通过版本化 discovery 禁止 apply；声明也须真实竞争验收。批准时重验 actor/Project/version/checksum/Integration/binding，不扩大原 scope；决策者限 Run 发起人或组织 system ADMIN，不是普通 ProjectMember。

## 仓库写入模式

| write_mode | 目标与限制 |
| --- | --- |
| direct（默认） | Git 默认目标普通 fast-forward push；SVN 绑定 URL，不据此宣称精确 revision CAS |
| branch | projectmind/ 保留分支；SVN 为仓库根 branches/projectmind/，只应新建或证明重放原提交 |

Git branch 可经 forge 创建 GitHub/GitLab PR/MR；未配 forge 仅分支/commit，direct 和 SVN 不调 forge。PR 失败不撤回 commit。[repository_effect](../../PJM/backend/src/projectmind/effects/repository_effect.py)旧“仅 Git 分支”注释不覆盖现有 direct/SVN 行为。

## 变更载体和冻结内容

提案冻结 binding、capability/operation、风险、Evidence、前置 revision、预览与幂等身份。仓库仅逐文件全文 SET/REMOVE，不执行任意 diff/Shell。Provider 使用独立可写临时副本，不能拿只读 input/ 提交。

### 契约与幂等身份

[change.propose request](../../PJM/contracts/tools/change.propose/v1/request.schema.json)由 capability validator 收窄；[decision route](../../PJM/backend/src/projectmind/api/routes/effects.py)接收决定与 Idempotency-Key；ClaimedEffectExecution 是内部冻结载体，不暴露凭据/config/lease。

提案键、决定键、Run 创建键分别定义。决策重放要求原 key 及同 proposal/version/checksum/decision/reason；远端沿原 Proposal/Effect 身份重试，不因新 Worker 换键。

现有 [repository.write Schema](../../PJM/contracts/tools/repository.write/v1/request.schema.json)仍限定 projectmind/ branch，非当前 Effect Worker 输入，也不是 direct 示例。后续审查消费者/版本/历史再同步，不能借旧 Schema 开放 Agent 直接 write。

## 并发、重试和失败

| 情况 | 当前处理及限制 |
| --- | --- |
| 决策版本/checksum 不符 | 拒绝旧预览，重新读取 |
| 目标陈旧/冲突 | STALE/target_stale 或 FAILED/target_branch_conflict，不改已批准内容 |
| 可重试 transport 错误 | 记错后 REQUESTED/Outbox 重进整个 Provider；次数耗尽、取消、过期不自动继续 |
| 回读不匹配 | VERIFICATION_FAILED，不自动重试；外部可能已变，证据未必保存 |
| 回读断连/commit 后 PR 失败 | 按 transport 分类；暂无持久“commit 完成待 PR”阶段 |
| claim 前取消 | 拒绝 Provider；claim 后仍有窗口 |
| Provider 中取消 | 有效 lease finalize 先存远端结果再取消 Run，不做逆向写入 |

reversible/rollback 仅说明可逆性，不提供一键回滚。撤回须审查真实 revision 后走新的修正流程；禁止 force、自动合并、自批、repository 免审、跨仓库原子写和无 scope 更新。

## 可靠性修正要求

以下是待实现目标，不因 Provider 存在而视为完成。

### 执行身份与远端前置条件

当前 Git/SVN 的 matches 仅比较变更路径内容；Redmine 在 revision 不同但字段相同时也可报 replayed。内容相同不能证明原身份执行，同名 branch 也可能属他人。

目标重放须同时证明原 Effect/请求、目标、批准基线及实际 revision/变更集；已知未执行则拒绝 stale，仍未知先核对。Git direct 预读+普通 push、branch 先查不存在+push 均需远端更新点的精确旧 ref/不存在条件；增加 GET/本地锁不足，不放开 force。

SVN 当前 checkout 未固定 revision，回读与返回 revision 取事后当前值。须固定 direct/branch 基线、提交回执与对应 revision 回读，out-of-date 不能覆盖 checkout 前变化。旧记录无证明不补造回执。

### 阶段回执与不确定结果

当前整个 Provider 成功返回后才保存 before/after：Git forge 失败丢失已确认 commit 的返回；SVN copy 与文件 commit 为两次写入；forge 复用同 source 首个 open PR，未完整核对 target/提案和并发。

目标在同 Effect 下追加调用前身份、写后可验证回执和读取结果，区分未执行、已执行待验证/PR、未知。恢复先查原身份，仅补未发生阶段；PR/MR 匹配仓库/source/target/提案，关闭/合并/并发/丢响应分别处理。

新载体尚未冻结，实施同步 migration/repository/Provider/事件/detail/Web。空旧字段不证明未执行；不靠 SQL、删 Effect 或换 key 修复，阶段记录也不能消除 DB 与远端断连窗口。

### 执行权与取消

当前 claim → apply → finalize 无贯穿 Provider 的 heartbeat/取消监督；过期重派时旧请求可能还在执行。claim 时间在取锁前计算，不能忽略等锁消耗。

目标获锁后用新时间建/验 lease，各新外部步骤前重验批准/取消与执行权。失权/超时不证明未写；保留并确认原结果后再决策，不能让第二 Worker 直接重写。只读核对与新写分开，取消也保留已发生事实。

## 验收

覆盖决定丢响应/同键改 reason、同内容不同提案、预读后目标变化、SVN copy 中断及固定 revision 回读、commit 后 PR 超时/并发、Provider 超 lease/取消/旧 Worker 晚到、finalize 响应未知。原身份不重建，不误认他人提交，部分成功可追溯。

入口：[Provider 回归](../../PJM/backend/tests/effects/)、[Effect Worker](../../PJM/backend/tests/worker/test_effect_executor.py)。真实 DB/远端/forge/CAS 故障注入与[审批 UI](workspace.md#审批请求与执行结果)恢复分别验收，局部 transport 测试不证明整链可靠。
