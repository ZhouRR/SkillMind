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
| options | 必须存在，可空；新问题的 key 唯一，CHOICE 至少两项；按 allow_multiple 限制，recommended 仅提示 |
| 回答 | CLARIFICATION/REVIEW 非空 text；CHOICE 选已知 key，可附 text，不经文本换绑 |
| required/期限 | 所有普通交互暂停；过期可无信息续行。expires_at 从事件 occurred_at 解析，不按打开页面重算 |
| checkpoint/continuation | 冻结问题依据，回答追加到新段，Session 续行另守 Runtime 兼容 |

每 Run 最多一个 OPEN 交互。持久化校验 checkpoint 的 Evidence/Artifact/Proposal 归属，格式不证明可读；期限以服务端为准。

旧问题若有重复选项 key，原文保留只读，不再收取无法区分含义的新答案；已有原答复仍可按原身份确认，不改旧选项、payload 或 hash。

### 普通提问不能代替外部批准

interaction.request 只允许 CLARIFICATION/CHOICE/REVIEW；Tool Schema、parser 与直接持久化入口使用同一限制，普通等待只能进入 WAITING_FOR_INPUT。外部效果统一 change.propose → 关联批准 → decision/Effect，不能通过普通答案取得批准。

共享 enum 中合法 Proposal 与旧 detail/event 保持可读。旧无 Proposal 的 EFFECT_APPROVAL 只读辨认，既不接受普通答复，也不由普通过期恢复解锁；不伪造 Proposal、批准或改写历史记录。

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

答复事务须复核入口的原会话凭据，而不是只接收 actor_id。先复用 Organization → User → AuthSession 锁序，再锁当前 Project 与 USER 的成员资格，最后按 Run → Segment → Interaction 加锁并刷新问题。ADMIN 也不能跨组织或绕过归档。

Project/成员使用 FOR SHARE：阻止状态修改，同时兼容 Worker 保存 Project 外键引用需要的 KEY SHARE，避免答复等 Run、Worker 又等 Project 的锁环。模式依据 [PostgreSQL 行锁规则](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS)；仍须真实事务验收。

拿齐业务锁后，以当前时刻复查原会话、CSRF 与当前资格，再校验题型并查询原 Response；首次还验 OPEN、原版本、当前期限、Run 与原 Segment 的等待状态。同事务保存回答、关闭旧段、追加新段/事件/Outbox；flush 后再验会话期限，才允许提交。

首次返回 201 / Idempotent-Replay: false，重放返回 200 / Idempotent-Replay: true，且 header 与 body 的 idempotent_replay 必须一致。详情与答复不缓存；Web 核验状态码、header、白名单字段和原 Project/Run/Interaction，而非把任意 2xx 当成功。

| 身份项 | 准确边界 |
| --- | --- |
| interaction_version | 问题原版本，不是 Run.row_version，不刷新版本自动重发旧答案 |
| key/hash | 按 interaction 唯一答复比较原 key 与 ID/版本/payload hash；对象键规范化，选择数组顺序不变 |
| 重放响应 | response_id/run_segment_id 是原答复，status/row_version 是当前 Run，不强制退回 QUEUED |
| actor | 首次保存回答者；重放独立核对原 actor_id，仍须当前 Project 授权，不要求与 Run 发起人相同 |

原 actor 独立比较，不加入或重算旧 request_hash。其他成员不能用相同答案/键确认别人的请求；已有答复可以在当前授权详情中阅读。首次与重放都经过上述授权门禁；答复先持锁则先完成，撤权/归档先完成则拒绝。不外推为其他业务、已有 SSE 或模型立即停权。

### 过期与拒绝响应

首次答复锁后发现到期，或 Recovery 扫描到期：等待 Run 记录 EXPIRED/INTERACTION_EXPIRED、INTERACTION_TIMEOUT 新段与 dispatch，不造 Response；Run 已终态/续行时只关闭旧交互，不重开或追加末次事件之后的记录。

service 在事务内捕获 InteractionExpiredError，最终授权仍有效且过期事实已提交后才返回 410，所以 410 不等于回滚。最终认证失败则连同过期续行一起回滚并返回权限错误；commit 失败仍可能未知。QUEUED 也不证明 Worker 已恢复，dispatch/队列仍可阻塞。

## 答复界面与结果未知

草稿与已发送意图分开；同一浏览器页面内，以原 actor/会话/Project/Run/Interaction 为边界保留原版本、payload 与 key。答复状态只有一个所有者，不能因切换观察标签、详情重新加载，或问题从 OPEN 移到历史区而丢失。

| 情况 | 界面规则 |
| --- | --- |
| 编辑/发送 | 显示原问题/期限/推荐；同步防重，共用 30 秒请求期限，发送后草稿不改原请求 |
| 确认成功 | 回执确定原 response_id/续行；不以较旧 row_version 回退 Run，再读取当前详情 |
| 409 / 410 | 读取原 Project/Run 详情中的原问题、已有答复与续行；不覆盖别人，不声称没有发生变更 |
| 422 / 身份权限失败 | 用稳定 Problem 分类和三语提示，不直接显示服务端原始正文；登录后不自动重发 |
| 网络/超时/abort/无效响应 | 保留原请求，读取当前事实；不自动重试、换键或宣称 rollback |
| 详情刷新失败 | 保留待确认意图，不将旧详情当作当前可写资格；自动刷新与人工 GET 的明确权限拒绝均关闭确认门禁，普通读失败不证明提交失败 |

“读取当前事实”只发送 GET；相同正文、作者或已续行都不能证明丢失的那次 POST。“确认原答复”是用户明确发起的同 key、原版本和原 payload POST：若此前已提交，返回原回执；若此前未落库，可能完成首次提交。页面必须说明这一副作用，不把它命名为纯查询。

每次 await/callback 前验当前身份与请求世代；同一 ID 离开后再返回也不能接收旧结果。Abort 不撤销服务端。换账号/会话/Project/Run、离页后旧响应无效；原请求仅存页面内存，整页刷新不保证恢复，不放 URL/Web storage/日志。重新进入先读授权历史，不从本地无 key 推断未提交。

## 兼容与开发接续

这是普通提问入口的安全收窄：旧 Worker 不应继续产生 EFFECT_APPROVAL，API/Worker/Web 配套发布并停止旧写入者；历史读取 enum 与 hash 不变，不用数据迁移“修复”旧悬空等待。

回答请求遵守原公开 Schema：原版本为正整数，可选字段可省略但不接受显式 null、空选择或额外字段；不对已发送正文、对象或选择顺序做新一轮整形。Web 不接受无法无损表示的整数。批准和 Evaluation 仍有独立协议，不能复用普通答复的原请求确认。

## 验收条件

覆盖禁止普通 EFFECT_APPROVAL 但保留合法/旧记录；CHOICE/REVIEW/非必答无隐式选择；原请求同 Response/Segment/dispatch；同键改内容/版本/actor 冲突；双人/expiry 竞争只一续行；410 已提交过期而未保存答案；丢响应/同 tick/切换不乱写；终态不重开。

真实 DB 锁/回滚、浏览器时序、模型暂停退出分别验证。代码入口见[代码根 README](../../PJM/README.md)。
