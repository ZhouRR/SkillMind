# 用户答复、等待与续行

本页定义普通交互的提出、答复、过期和原请求确认。外部批准见[受控效果](repository-effects.md)，结果评价见[结果设计](results-evaluation.md)；实施状态见[计划 R07](../planning/roadmap.md#r07-run-与审计)。

## 先分清三种人工参与

| 操作 | 改变什么 |
| --- | --- |
| 普通答复 | CLARIFICATION/CHOICE/REVIEW 追加 Response 与新 Segment，不改目标/资源/权限 |
| 外部批准 | 独立 decision API 决定精确 Proposal version/checksum，APPROVED 不等于已写入 |
| Evaluation | 对保存的 Result 追加判断，不改原值、不恢复终态 |

REVIEW 不等于 Evaluation。普通答复需当前 ProjectWriteActor，批准另外限发起人或 system ADMIN。

## 一个例子：回答超时不等于什么都没发生

问题推荐 A、也可选 B，用户提交 B 后丢响应：可能 B 与新 Segment/Outbox 已提交，应确认原请求；也可能已过期，服务端先提交无答复续行再返回 410；另一成员先回答则返回 409 并保留对方决定。

推荐不是默认答案，required=false 不自动跳过，也不允许空答案；当前无 skip。超时记录“未收到答复”，不能伪造用户选了推荐项。

## 提问与答复的实际形状

精确字段见[提问 Tool](../../PJM/contracts/tools/interaction.request/v1/request.schema.json)和[答复契约](../../PJM/contracts/runs/interaction-response/v1/request.schema.json)。

| 内容 | 规则 |
| --- | --- |
| prompt/rationale/impact | 公开说明，不含 hidden reasoning、系统 prompt 或 Secret |
| options | 必须存在，可空；CHOICE 至少两项，按 allow_multiple 限制，recommended 仅提示 |
| 回答 | CLARIFICATION/REVIEW 非空 text；CHOICE 选已知 key，可附 text，不经文本换绑 |
| required/期限 | 所有普通交互暂停；过期可无信息续行。expires_at 从事件 occurred_at 解析，不按打开页面重算 |
| checkpoint/continuation | 冻结问题依据，回答追加到新段，Session 续行另守 Runtime 兼容 |

每 Run 最多一个 OPEN 交互。持久化校验 checkpoint 的 Evidence/Artifact/Proposal 归属，格式不证明可读；期限以服务端为准。

### 普通提问不能代替外部批准

当前 Tool Schema/parser 接受 EFFECT_APPROVAL，Executor 可存等待；普通答复拒绝该类型、普通过期排除它，又没有对应 Proposal，形成悬空风险。

目标只允许三种普通类型从 interaction.request 进入等待；外部效果统一 change.propose → 关联批准 → decision/Effect。同步 Tool/validator/Executor/测试并审查 request 兼容，不放开通用答复批准权。共享 enum 中合法 Proposal 与旧 detail/event 仍保留可读；旧悬空记录只读辨认，不伪造批准或改表解锁。

## 三个提交边界

```text
提问事务：问题/checkpoint/事件 + Segment WAITING
          Attempt DEFERRED / 主 Session IDLE / 释放 lease
  ↓
答复或过期事务：关闭旧交互/Segment
               新 Segment + 事件/Outbox，Run QUEUED
  ↓
新 Attempt：准备输入 / 冻结 Brief / 启动校验 → Session
```

提问锁顺序 Run → Segment → Attempt，重验 lease/RUNNING/取消。等待保存与 lease 释放不证明进程退出/费用结清，[监督](run-supervision.md#等待提交仍是独立边界)另管；任何事务都不持锁等模型或资源 I/O。

### 首次答复与原答复重放

POST /api/v1/projects/{project_id}/runs/{run_id}/interactions/{interaction_id}/responses 带当前 Session/Origin/CSRF、Idempotency-Key、interaction_version/response。入口认证与业务事务分开。

Repository 按 Run → Segment → Interaction 加锁，校验类型并查询原 Response；首次还验 OPEN、原版本、当前期限及等待状态。同事务保存回答、关闭旧段、追加新段/事件/Outbox；首次 201，重放 200 + Idempotent-Replay。

| 身份项 | 准确边界 |
| --- | --- |
| interaction_version | 问题原版本，不是 Run.row_version，不刷新版本自动重发旧答案 |
| key/hash | 按 interaction 唯一答复比较原 key 与 ID/版本/payload hash；对象键规范化，选择数组顺序不变 |
| 重放响应 | response_id/run_segment_id 是原答复，status/row_version 是当前 Run，不强制退回 QUEUED |
| actor | 首次存 actor_id，但当前重放 hash 未绑定它；仍需 Project 授权，不宣称仅原作者可重放 |

Web 必须限定原 actor/context；服务端后续收口原 actor 时审查旧 Response 的归属/重放，不改旧 hash。

### 过期与拒绝响应

首次答复锁后发现到期，或 Recovery 扫描到期：等待 Run 记录 EXPIRED/INTERACTION_EXPIRED、INTERACTION_TIMEOUT 新段与 dispatch，不造 Response；Run 已终态/续行时只关闭旧交互，不重开或追加末次事件之后的记录。

service 在事务内捕获 InteractionExpiredError，提交过期事实后才抛 API 错误，所以 410 不等于回滚；commit 失败仍可能未知。QUEUED 也不证明 Worker 已恢复，dispatch/队列仍可阻塞。

## 答复界面与结果未知

当前 [InteractionCard](../../PJM/web/src/components/RunResultPanel.tsx)每次 submit 生成新 key，仅 state 禁用/卸载 abort，无原答复确认，当前请求和同 tick 防重仍未闭合。

| 情况 | 待补界面规则 |
| --- | --- |
| 编辑/发送 | 显示原问题/期限/推荐；同步 guard，冻结 actor/Project/Run/Interaction/版本/payload/key，草稿不改原请求 |
| 确认成功 | 核对 response_id/续行，刷新当前 Run，不把重放当新段或旧状态 |
| 409 / 410 | 分别读取已有答复/过期续行，不覆盖别人，不显示“没有发生任何变更” |
| 422 / 身份权限失败 | 区分无效回答与实际 Problem；登录后不自动重发 |
| 网络/超时/abort/无效响应 | 保留原请求短期确认，不自动换键或宣称 rollback |

每次 await/callback 前验当前身份；切账号/Project/Run/Interaction、离页后旧响应无效。Abort 不撤销服务端。原请求仅存页面内存，刷新不保证恢复，不放 URL/Web storage/日志；重新进入先读授权历史，不从本地无 key 推断未提交。

## 兼容与开发接续

先关无 Proposal 的批准入口，再明确原 actor 重放和期限竞争，后补 Web 发送/未知/确认与三语。Evaluation 当前没有本协议的幂等，不能通用重发。

## 验收条件

覆盖禁止普通 EFFECT_APPROVAL 但保留合法/旧记录；CHOICE/REVIEW/非必答无隐式选择；原请求同 Response/Segment/dispatch；同键改内容/版本/actor 冲突；双人/expiry 竞争只一续行；410 已提交过期而未保存答案；丢响应/同 tick/切换不乱写；终态不重开。

真实 DB 锁/回滚、浏览器时序、模型暂停退出分别验证。代码入口见[代码根 README](../../PJM/README.md)。
