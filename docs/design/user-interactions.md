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

推荐 A、可选 B，提交 B 后丢响应：可能原答案/续行已提交，也可能服务端先提交过期续行再返回 410；另一成员抢先回答则 409，保留对方决定。

推荐非默认答案，required=false 不自动跳过/允许空答；当前无 skip。超时只记未答，不伪造选择。

## 提问与答复的实际形状

精确字段见[提问 Tool](../../SKM/contracts/tools/interaction.request/v1/request.schema.json)和[答复契约](../../SKM/contracts/runs/interaction-response/v1/request.schema.json)。

| 内容 | 规则 |
| --- | --- |
| prompt/rationale/impact | 公开说明，不含 hidden reasoning、系统 prompt 或 Secret |
| options | 必须存在，可空；新问题的 key 唯一，CHOICE 至少两项；按 allow_multiple 限制，recommended 仅提示 |
| 回答 | CLARIFICATION/REVIEW 非空 text；CHOICE 选已知 key，可附 text，不经文本换绑 |
| required/期限 | 所有普通交互暂停；过期可无信息续行。expires_at 从事件 occurred_at 解析，不按打开页面重算 |
| checkpoint/continuation | 冻结问题依据，回答追加到新段，Session 续行另守 Runtime 兼容 |

每 Run 最多一个 OPEN；保存时验 checkpoint 引用归属，期限以服务端为准。旧重复选项 key 保持只读，不收新答案；已有答复可按原身份确认，不改历史 payload/hash。

### 普通提问不能代替外部批准

Tool Schema/parser/持久化入口均只接受 CLARIFICATION/CHOICE/REVIEW，进入 WAITING_FOR_INPUT。效果必须 change.propose → 关联批准 → decision/Effect。

合法 Proposal 和旧 detail/event enum 保持可读；无 Proposal 的旧 EFFECT_APPROVAL 只读，普通答复/过期都不解锁，不补造批准或改历史。

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

提问按 Run → Segment → Attempt 取锁，验 lease/RUNNING/取消；不持锁等 I/O。等待提交/释放 lease 不证明退出或结清，另走[监督](run-supervision.md#等待提交仍是独立边界)。

### 首次答复与原答复重放

答复 POST 携带 Session/Origin/CSRF、Idempotency-Key、interaction_version/response，路径与字段见[答复契约](../../SKM/contracts/runs/interaction-response/v1/request.schema.json)。事务复用[原会话校验](authentication.md#认证与业务提交不是同一个事务)，不能只收 actor_id：

```text
Organization → User → 原 AuthSession → Project/USER 成员 SHARE
  → Run → Segment → Interaction
  → 原会话/CSRF/资格复核 → 原 Response / 首次答复
  → flush → 最终期限复核 → commit
```

Project/成员 SHARE 阻止状态修改且兼容 Worker 外键 KEY SHARE，避免双方反向等待；ADMIN 不免组织/归档检查。首次另验题型、OPEN、原版本、期限及原 Run/Segment 等待状态，同事务存答复、关旧段、增新段/事件/Outbox。

首次 201/Idempotent-Replay: false，重放 200/true，header 与 body 必须一致。详情/答复 no-store，Web 验状态/header/白名单及原 Project/Run/Interaction。

| 身份项 | 准确边界 |
| --- | --- |
| interaction_version | 问题原版本，不是 Run.row_version，不刷新版本自动重发旧答案 |
| key/hash | 按 interaction 唯一答复比较原 key 与 ID/版本/payload hash；对象键规范化，选择数组顺序不变 |
| 重放响应 | response_id/run_segment_id 是原答复，status/row_version 是当前 Run，不强制退回 QUEUED |
| actor | 首次保存回答者；重放独立核对原 actor_id，仍须当前 Project 授权，不要求与 Run 发起人相同 |

原 actor 独立比较，不重算旧 hash；他人可授权读历史，不能用同键/答案确认原请求。首次/重放同门禁：答复先持锁可先完成，撤权/归档先完成则拒绝；不代表 SSE/模型立即停权。

### 过期与拒绝响应

首次答复锁后到期或 Recovery 扫描到期：等待 Run 保存 EXPIRED/INTERACTION_EXPIRED、INTERACTION_TIMEOUT 新段/dispatch，不造 Response；已终态/续行只关闭旧交互，不重开或追加末次事件。

service 捕获 InteractionExpiredError，最终授权有效且过期提交后返回 410；认证失败整笔回滚，commit 失败仍可能未知。410 非回滚证明，QUEUED 非 Worker 已恢复证明。

## 答复界面与结果未知

草稿与原请求分开；单一页面 owner 按 actor/会话/Project/Run/Interaction 保留版本/payload/key，不因切标签、重载详情或移入历史而丢失。

| 情况 | 界面规则 |
| --- | --- |
| 编辑/发送 | 显示原问题/期限/推荐；同步防重，共用 30 秒请求期限，发送后草稿不改原请求 |
| 确认成功 | 回执确定原 response_id/续行；不以较旧 row_version 回退 Run，再读取当前详情 |
| 409 / 410 | 读取原 Project/Run 详情中的原问题、已有答复与续行；不覆盖别人，不声称没有发生变更 |
| 422 / 身份权限失败 | 用稳定 Problem 分类和三语提示，不直接显示服务端原始正文；登录后不自动重发 |
| 网络/超时/abort/无效响应 | 保留原请求，读取当前事实；不自动重试、换键或宣称 rollback |
| 详情刷新失败 | 保留待确认意图，不将旧详情当作当前可写资格；自动刷新与人工 GET 的明确权限拒绝均关闭确认门禁，普通读失败不证明提交失败 |

GET 只读当前事实；相同正文/作者或已续行不证明原 POST。“确认原答复”是用户明确的原 key/版本/payload POST，可能首次落库，必须说明此副作用，不能称纯查询。

每次 await/callback 验身份/世代，离开再回同 ID 也不收旧结果。Abort 不撤销服务端；换账号/会话/Project/Run 或离页使旧响应失效。原请求只存内存，不进 URL/Web storage/日志，刷新不承诺恢复；重新进入先读历史，无本地 key 不证明未提交。

## 兼容与开发接续

API/Worker/Web 配套发布并停旧写入者，不再产生普通 EFFECT_APPROVAL；旧 enum/hash 不变，不迁移“修复”悬空等待。

原版本为正整数，可选字段可省略但不接受 null/空选择/额外字段；已发送正文和选择顺序不重新整形，Web 拒绝无法无损表示的整数。批准/Evaluation 不复用本确认协议。

## 验收条件

覆盖禁止普通 EFFECT_APPROVAL 但保留合法/旧记录；CHOICE/REVIEW/非必答无隐式选择；原请求同 Response/Segment/dispatch；同键改内容/版本/actor 冲突；双人/expiry 竞争只一续行；410 已提交过期而未保存答案；丢响应/同 tick/切换不乱写；终态不重开。

真实 DB 锁/回滚、浏览器时序、模型暂停退出分别验证。代码入口见[代码根 README](../../SKM/README.md)。
