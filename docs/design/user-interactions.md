# 用户答复、等待与续行

> 定位：普通 UserInteraction 的提出、答复、过期和原请求确认。前置阅读：[Run / Segment / Attempt](domain-model.md#run-状态速查)。外部批准由[受控写入](repository-effects.md)负责，页面布局由 [Workspace](workspace.md)负责。本文区分当前代码与修正要求，状态只在[计划 R07 / R10](../planning/roadmap.md#r07-run-与审计)维护。

## 先分清三种人工参与

| 操作 | 改变什么、不改变什么 |
| --- | --- |
| 普通答复 | 回答 CLARIFICATION / CHOICE / REVIEW，追加 InteractionResponse 和新 Segment；不修改原目标、资源或权限 |
| 外部批准 | 对精确 Proposal version/checksum 作决定，通过独立 decision API；APPROVED 不等于远端写入成功 |
| 结果评价 | 对已经保存的 Result 追加 Evaluation；不改 AI 原值，也不自动恢复终态 Run |

REVIEW 是运行中的反馈，[Evaluation 是结果上的评价](results-evaluation.md#评价请求与历史)，不能因中文都叫“评审”就共用写入协议。普通答复要求当前有效的 ProjectWriteActor；外部批准还限制为 Run 发起人或 system ADMIN。更多成员角色或指定审批人不是本轮新增设计。

## 一个例子：回答超时不等于什么都没发生

Run 请求用户选择分析口径，推荐 A，也允许 B。页面未自动勾选；用户选择 B 并提交，但没有收到响应。

1. 服务端可能已保存 B、完成旧 Segment、追加新 Segment 和 dispatch Outbox。此时应确认原答复，不用新 key 再提交。
2. 如果处理首次答复时已经过期，服务端可先记录 EXPIRED 和无答复的续行，再返回 `410 interaction_expired`。B 没有被接受，但不能显示“服务器未作任何变更”。
3. 如果另一成员先回答，当前提交收到 `409 interaction_conflict`。刷新已有答复和 Run 状态，不覆盖对方决定，也不自动套用新版本重试。

推荐 A 不等于默认回答。`required: false` 也不等于“自动跳过”或“允许提交空答案”；当前没有单独的 skip 操作。超时续行只记录“未收到回答”，不能把推荐项写成用户选择。

## 提问与答复的实际形状

精确字段由 [Tool request](../../PJM/contracts/tools/interaction.request/v1/request.schema.json)、[答复 request](../../PJM/contracts/runs/interaction-response/v1/request.schema.json)和 [response](../../PJM/contracts/runs/interaction-response/v1/response.schema.json)负责。本页只解释语义，不复制完整 JSON 字典。

| 内容 | 当前含义与限制 |
| --- | --- |
| 问题与理由 | prompt、rationale、impact 是公开说明，不能包含 hidden reasoning、Secret 或系统 prompt |
| 候选 | options 必须存在，但可为空；CHOICE 至少两项，并按 allow_multiple 限制选择数。recommended 是展示提示，没有 default_answer 字段 |
| 普通回答 | CLARIFICATION / REVIEW 使用非空 text；CHOICE 必须提交已知的 selected_option_keys，可附 text。当前 CLARIFICATION 没有专用资源选择器，也不能借文本换绑 |
| 是否必答 | required 保留业务要求；当前所有普通交互都暂停，过期后都可能产生缺失信息续行，不自动猜测答案 |
| 期限 | expires_in_seconds 的范围由 Schema 固定；Executor 以事件 occurred_at 解析 expires_at，不是浏览器打开页面时重新起算 |
| 续行上下文 | checkpoint 与 continuation_mode 冻结到交互，答复追加到新 Segment 的 checkpoint；最终 Session 恢复还需满足 Runtime 的兼容条件 |

一条 Run 同时最多一个 OPEN 交互。checkpoint 的 Evidence / Artifact / Proposal 引用在持久化时验证归属；Schema 合法不等于引用可读。超过期限的判断以服务端为准，页面倒计时和本地时钟不是授权依据。

### 普通提问不能代替外部批准

当前存在一条需要修正的入口差距：Tool request Schema 与 `parse_interaction_request` 接受 EFFECT_APPROVAL，Executor 的普通提问分支可将它保存为等待批准。但普通答复 validator 拒绝这种类型，普通过期恢复又排除它；该路径没有创建对应的 ChangeProposal。单靠“请从批准页处理”不能补出缺少的提案。

这是源码调用链推导出的悬空等待风险，不是本轮运行真实模型复现的结论。修正要求是：普通 interaction.request 只允许三种普通交互进入等待；外部效果由 change.propose 创建 Proposal 及关联批准交互，再由 decision / Effect 恢复路径处理。不能放开通用答复 endpoint 来绕过精确批准。

同步 Tool 定义/说明、运行时 validator、Executor 拒绝路径与测试；如收窄公开 request Schema，按[契约兼容](../development/contract-workflow.md)评审消费者。历史 detail/event 中的 EFFECT_APPROVAL 和合法 Proposal 关联交互仍须可读，不能从共享 enum 全局删除。既有悬空记录先只读辨认，不伪造批准或直接改表。

## 三个提交边界

下面只画普通交互。提问、答复和过期分别提交；后续 Worker 的领取、准备与启动又有自己的边界。阶段之间可能等待、失败或停机，不是一个跨请求事务，也不把输入 I/O 或模型运行放进数据库事务。

```text
提问事务
  问题 / checkpoint / 等待事件
  Segment WAITING / Attempt DEFERRED
  释放 Attempt lease
          ↓ 等待用户或期限处理
答复事务 或 过期事务
  关闭旧交互与旧 Segment
  追加新 Segment / 事件 / dispatch Outbox
  Run → QUEUED
          ↓ 异步配送与领取
Worker 新 Attempt
  准备输入 / 冻结 Brief / 启动校验
  才能恢复或启动 AgentSession
```

### 提问与等待

`suspend_for_interaction` 按 Run → Segment → Attempt 加锁，复验 lease、RUNNING 与取消意图，再保存交互、checkpoint 事件和 Run snapshot。等待提交成功后，原 Attempt 为 DEFERRED，主 Session 记 IDLE。

这证明持久化等待和释放 lease，不直接证明 SDK 进程退出、所有网络请求停止或费用已结清。实际清理由[执行监督](run-supervision.md#等待提交仍是独立边界)负责，不能把文档中的“等待不计模型时间”当作完整停止的观测结果。

### 首次答复与原答复重放

HTTP 入口是 `POST /api/v1/projects/{project_id}/runs/{run_id}/interactions/{interaction_id}/responses`，带当前 Session / Origin / CSRF、Idempotency-Key，以及正文 interaction_version 和 response。

ProjectWriteActor 是入口鉴权，不表示账户/会话与答复在同一事务提交。[认证与业务提交](authentication.md#认证与业务提交不是同一个事务)的边界仍适用，不能从下列 Run 锁顺序推导账户锁后的再次认证。

Repository 按 Run → Segment → Interaction 加锁，验证回答类型，并查询已有 InteractionResponse。首次答复还验证 OPEN、原版本、当前期限及 Run 等待状态；在同一事务中保存答复、关闭旧段、创建新段并写入事件/Outbox。首次成功返回 201，原请求重放返回 200 和 `Idempotent-Replay: true`。

| 容易误读的值 | 准确边界 |
| --- | --- |
| 问题版本 | interaction_version 是被回答问题的版本，不是 Run.row_version；不得刷新成新值后自动重发旧答案 |
| 请求键 | Idempotency-Key 是原回答的请求身份，不是新 Run 的创建键。当前按 interaction 查唯一答复，再比较 key 和规范化 payload hash |
| 重放内容 | hash 包含 interaction ID、原版本与 response；对象键顺序会规范化，但选择数组顺序仍属于内容，client 不自行重排 |
| 重放响应 | response_id / run_segment_id 指向原答复及其续行；status / row_version 反映读取时的 Run，可能已不是 QUEUED，也不能据此把页面退回原段 |
| 回答身份 | 首次答复保存 actor_id；现有重放比较未绑定 actor_id。每次仍需当前 Project 授权，但不能宣称已实现“仅原答复者可重放” |

界面上的原请求确认必须限定原 actor 和上下文，不能把页面遗留 payload/key 自动交给新账号。服务端若增加原 actor 约束，应单独检查旧 Response 的 actor_id 和历史重放，不改写既有 hash 或审计归属；该安全收敛与真实并发验证属于接续工作。

### 过期与拒绝响应

普通过期有两个入口：首次答复在锁后发现到期，或者 Worker recovery 扫描到期交互。仍在等待的 Run 会追加 INTERACTION_EXPIRED、INTERACTION_TIMEOUT Segment 和 dispatch；不创建虚假 InteractionResponse。恢复扫描发现 Run 已终态或已经续行时，仅关闭旧交互，不再打开 Run 或追加终态后的事件。

答复 service 特意在事务内捕获 InteractionExpiredError，提交过期事实后才向 API 抛出错误。因此 `410` 不是该事务回滚的证据。若 commit 自身失败，结果仍可能未知，不承诺一定返回 410。Run 回到 QUEUED 也不等于 Worker 已经恢复，dispatch 关闭或队列故障仍可能使其等待。

## 答复界面与结果未知

现有 [InteractionCard](../../PJM/web/src/components/RunResultPanel.tsx)展示问题、期限、选项与历史答复，通过 [runs client](../../PJM/web/src/api/runs.ts)提交。它每次 submit 生成新 key，使用 state 禁用按钮、unmount 时 abort；尚无原答复确认状态，同一 DOM tick 的同步防重与成功后的当前请求校验也未闭合。Run 创建已有恢复机制不代表此处自动具备同样能力。

| 用户看到的情况 | 修正后的界面责任 |
| --- | --- |
| 编辑中 | 显示原问题/版本、期限与推荐理由；推荐不预先提交，草稿不改变已发送的回答 |
| 发送中 | 用同步 request guard 防重；固定 actor / Project / Run / Interaction / 原版本 / payload / key |
| 回答已确认 | 使用服务端 response_id 与续行身份刷新当前 Run；不能把重放响应强行解释为新 Segment 或旧状态 |
| 有冲突 | `409 interaction_conflict`：读取已有答复和版本，解释已回答/已关闭等事实，不覆盖另一成员的答案 |
| 已过期 | `410 interaction_expired`：回答未接受，读取过期事实与可能的新段；不能显示“没有发生任何变更” |
| 需纠正输入 | `422 interaction_response_invalid`：说明回答与类型/选项不匹配。通用请求校验错误仍按实际 Problem 处理 |
| 身份或权限失败 | 401 / 403 / 404 分别沿认证/Project 隐藏边界处理；重新登录不触发旧答复自动重发 |
| 结果未知 | 网络断开、超时、abort、无法验证的成功响应：保留原请求的短期确认能力，不显示 rollback，也不自动换 key |

这是待补齐的交互要求，不是当前 UI 已交付清单。原请求保留在页面内存，回答正文、cookie、CSRF 不进 URL、持久存储或日志；离页/刷新后不保证恢复。重新进入先读取授权范围内的 Run detail 和交互历史，不能把缺少本地 key 当成服务器从未收到。

每次 await 后以及 callback 前检查请求仍属于当前上下文；切换账号、Project、Run、Interaction 或离页时废弃旧请求资格。AbortController 只限制 client 继续处理，不保证服务端撤销。三语文案、键盘焦点、窄屏长问题与已被他人回答的状态应同时验证。

## 兼容与开发接续

保留 [Runtime §8](agent-runtime.md#8-用户交互协议) 的旧编号入口。交互状态不是 Run 状态，过期续行不是新用户答复，Evaluation 也不沿用普通答复的幂等保证：当前评价 POST 没有 Idempotency-Key 协议，重复发送会追加评价，不能自动重放。

接续顺序：先关闭无 Proposal 的批准入口并验证历史可读；再明确原 actor / 原答复重放与期限竞争；然后补 Web 的发送/未知/确认状态及三语，最后做真实事务、恢复和浏览器联验。代码入口见 [Backend](../../PJM/backend/README.md#通常回答と期限処理を追う)、[Contracts](../../PJM/contracts/README.md#通常回答と評価の契約を読む)与 [Web](../../PJM/web/README.md#待処理と外部結果を表示する)。

## 验收条件

| 给定条件与操作 | 应观察到的结果 |
| --- | --- |
| 普通 Tool 请求 EFFECT_APPROVAL | 等待持久化前拒绝；合法 Proposal 批准与旧 detail/event 仍可使用 |
| CHOICE / REVIEW / required=false | 选择规则、非空回答和推荐含义一致；无自动选择、空答复或隐式 skip |
| 同一请求再次确认 | 同一 response_id / 续行 ID，不增加 Response / Segment / dispatch；当前 Run 状态不倒退 |
| 同键改内容、改原版本或换 actor | 按明确的身份/冲突规则拒绝，原审计保持不变；新规则覆盖历史 Response |
| 两成员回答，或回答与 expiry 竞争 | 持锁后只产生一条被接受的业务续行；完整事件/Outbox 与实际 rollback 分别检查 |
| 期限到达、commit 成功但返回 410 | 没有保存用户答案；过期事件和 timeout Segment 已保存，不使用推荐项 |
| 同 tick 双 submit、丢响应、context 切换 | 无重复发送，原请求可确认，旧完成不触发新页面 callback；三语/键盘/窄屏可读 |
| 终态/已续行交互、队列暂停 | 不重开终态；QUEUED 不显示为模型已恢复；不靠改表或补造答案解除等待 |

纯 validator、mock repository/transaction、ASGI fake service 与静态组件测试只证明各自边界。上述竞争/回滚需专用真实 DB，页面时序需真实 component + 故障响应，模型暂停/退出另需实际进程验证。本轮文档整理不执行这些业务写入，也不把新增验收条目计为通过。
