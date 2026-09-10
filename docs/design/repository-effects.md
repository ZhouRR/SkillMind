# 受控外部写入

本页定义提案、批准、远端效果与恢复；实现状态见[计划 R08](../planning/roadmap.md#开发任务)，续行见 [Runtime](agent-runtime.md)，处置见[Runbook](../operations/runbook.md)。

## 先分清四种事实

Git push/回读成功而 PR 超时：commit 已成立，PR/平台保存仍可能未知；重启、再批准、取消 Run 均不撤销 commit。

| 对象 | 状态含义 |
| --- | --- |
| ChangeProposal | 冻结变更；APPROVED 是获准，不是已写入 |
| ChangeApproval | 对精确 version/checksum 的决定，不扩其他权限 |
| EffectExecution | APPLIED 是已保存 Provider 完成结果；FAILED 不证明远端未变，VERIFICATION_FAILED 属于此对象 |
| Run / Evidence | 分别是分析流程与审计依据，不代替远端执行事实 |

Effect attempt_no 非 RunAttempt。before_ref/after_ref 可空，仓库 before 只定位基线、不保证逆向补丁。模型 effects 的[保存记录核验](results-evaluation.md#效果摘要核对到哪一步)不证明远端原执行或解决可靠性缺口。

## 调用与批准链路

```text
observe → Evidence → change.propose → 精确批准/允许的预授权
  → TX 1：批准 / Effect / Outbox
  → TX 2：提案与 binding 校验 / claim
  → 锁外：凭据 / 前置检查 / 写入 / read-back / 可选 PR
  → TX 3：结果 / Evidence / 事件 / 后续调度
```

锁序 Run → Segment → Proposal → Effect，远端 I/O 在锁外，无跨 DB/仓库/forge 原子性。拒绝不产生可执行 Effect；批准后 Run 可仍 WAITING_FOR_APPROVAL，Effect Worker 独立执行。

有效 finalize 的结果/不可重试失败交新 Segment，临时失败沿原 Segment/Proposal/Effect 重调度。Agent 仅提案；[catalog](../../SKM/backend/src/skillmind/effects/catalog.py)统一 Provider/validator/预授权资格。

## 已注册写入

| capability | 批准与执行边界 |
| --- | --- |
| issue.update/v1 | Redmine CAS adapter；仅 system ADMIN 配置 LOW 风险精确 scope 可预授权，discovery → 前置 revision → 条件写入 → 回读 |
| repository.write/v1 | Git/SVN，始终人工批准，不 force；精确 CAS/恢复仍有缺口 |

标准 Redmine REST 不具本协议 CAS/幂等，须通过版本化 discovery 及真实竞争验收。批准复验 actor/Project/version/checksum/Integration/binding/scope；仅 Run 发起人或组织 system ADMIN 决策。

## 仓库写入模式

| write_mode | 目标与限制 |
| --- | --- |
| direct（默认） | Git 默认目标普通 fast-forward push；SVN 绑定 URL，不据此宣称精确 revision CAS |
| branch | skillmind/ 保留分支；SVN 为仓库根 branches/skillmind/，只应新建或证明重放原提交 |

Git branch 可经 forge 建 PR/MR，未配 forge 仅分支/commit；direct/SVN 不调 forge，PR 失败不撤 commit。

## 变更载体和冻结内容

提案冻结 binding、capability/operation、风险、Evidence、revision、预览、幂等身份。仓库只接受逐文件全文 SET/REMOVE，不执行 diff/Shell；Provider 使用独立可写副本，不从只读 input/ 提交。

### 契约与幂等身份

[提案契约](../../SKM/contracts/tools/change.propose/v1/request.schema.json)由 capability validator 收窄；[decision](../../SKM/backend/src/skillmind/api/routes/effects.py)携带 Idempotency-Key，内部 ClaimedEffectExecution 不暴露凭据/config/lease。

提案/决定/Run 创建键分开；决定重放须原 key/proposal/version/checksum/decision/reason，远端重试沿原 Proposal/Effect，不随 Worker 换键。[旧 repository.write Schema](../../SKM/contracts/tools/repository.write/v1/request.schema.json)仍限 skillmind/ branch，非 Effect Worker 输入/direct 示例；须审查版本/历史再同步，不据此开放直接 write。

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

reversible/rollback 不提供回滚操作；撤回须验真实 revision 再走新修正流程。禁止 force、自动合并、自批、repository 免审、跨仓库原子写及无 scope 更新。

## 可靠性修正要求

以下是待实现目标，不因 Provider 存在而视为完成。

### 执行身份与远端前置条件

当前 Git/SVN matches 只比变更路径内容，Redmine 不同 revision 同值也可报 replayed；同内容/同名 branch 不证明原执行。

目标重放须证原 Effect/请求、目标、批准基线、实际 revision/变更集；已知未执行但基线失效拒绝 stale，未知先核对。Git direct/branch 须在远端更新点校验精确旧 ref/不存在条件，GET/本地锁不足，仍禁 force。

SVN 当前 checkout 未固定 revision，回读取事后值；须固定 direct/branch 基线、提交回执并按对应 revision 回读。out-of-date 不保护 checkout 前变化，旧记录不补造回执。

### 阶段回执与不确定结果

当前 Provider 完整返回才存 before/after：forge 失败会丢 commit 返回，SVN copy/文件 commit 是两次写；首个同 source open PR 未完整验 target/提案/并发。

目标同 Effect 追加调用前身份、写后回执、回读，区分未执行/已执行待验证或 PR/未知。恢复查原身份、仅补未发生阶段；PR/MR 验仓库/source/target/提案，分别处理关闭/合并/并发/丢响应。

新载体未冻结，须同步存储/Provider/事件/消费者。旧空值不证明未执行，不靠改 SQL、删 Effect、换 key 修复；阶段记录不消除 DB/远端断连窗口。

### 执行权与取消

当前 Provider 无贯穿 heartbeat/取消监督，claim 时间在取锁前计算，过期重派时旧请求可能仍在写。

目标获锁后用新时间建/验 lease，每个新外部步骤前重验批准/取消/执行权。失权/超时先确认原事实，第二 Worker 不直接重写；只读核对与新写分开，取消保留已发生事实。

## 验收

覆盖决定丢响应/同键改 reason、同内容不同提案、预读后目标变化、SVN copy 中断及固定 revision 回读、commit 后 PR 超时/并发、Provider 超 lease/取消/旧 Worker 晚到、finalize 响应未知。原身份不重建，不误认他人提交，部分成功可追溯。

入口：[Provider](../../SKM/backend/tests/effects/)、[Worker](../../SKM/backend/tests/worker/test_effect_executor.py)。真实 DB/远端/forge/CAS 与[审批 UI](workspace.md#审批请求与执行结果)分别验收。
