# Run 创建、重发与幂等

本页定义创建身份、首次快照与原请求确认；资源规则见[资源快照](resource-snapshots.md)，实现状态见[计划](../planning/roadmap.md)。

## 先分清三种“再执行”

| 意图 | 正确结果 |
| --- | --- |
| 创建响应丢失，确认原请求 | 返回原 Run，不新增快照、Segment 或 dispatch |
| Worker 故障恢复 | 同 Segment 新增 Attempt，沿用冻结事实 |
| 用户改变输入/范围/目标 | 新键、新 Run，不改旧快照 |

答复/批准是已有 Run 的业务续行；Schedule 每个 occurrence 独立创建，同一 occurrence 的恢复不是新触发。

## 请求身份与执行快照分开保存

请求身份是用户意图，执行快照是首次固定的事实；重放均不改写。

| 内容 | 身份 / 首次快照 |
| --- | --- |
| actor、Project、精确 SkillVersion/task_key | 全部参与身份；快照另存 Manifest checksum |
| input、显式选项 | 按版本化规则比较；保存验证后的输入/Schema，不任意排序数组、转型或补默认值 |
| 文档单份/集合 | 比较 ID；集合排序且拒绝重复；快照固定成员、路径、MIME、size/hash |
| 文档全集 | 比较“全集”规则，不重新展开；首次成员单独冻结 |
| Integration/binding | 比较显式身份或默认选择规则；冻结实际 scope/配置版本/binding，不重读今天的默认值 |
| 权限/额度与追踪 | 上限由服务端冻结；trace、时间、生成 ID 不造成请求差异 |

文档必须显式选择，可选省略不授权全集；Integration 可解析默认 binding，但省略与显式指定仍是不同意图。新增限额选项须版本化纳入身份。

服务端用[共享 hashing](../../SKM/backend/src/skillmind/core/hashing.py)保存规范化意图、摘要及格式版本；客户端 fingerprint 不能代替认证或原内容比较。

### 幂等键的作用域

唯一约束为 (project_id, task_id, Idempotency-Key)，task_id 由精确 SkillVersion/task_key 导出，不按 actor 隔离。同作用域同键异 actor/input/选择冲突，跨作用域不承诺冲突；新意图生成新键。

`schedule:` 保留给持久 occurrence。普通入口仅可确认已有同身份/内容的 Run；不存在则 409 idempotency_conflict，不能绕过调度关联/重叠/计数新建。旧键/快照不改，发布时 API/Worker 配套切换，不混用旧 API。

## 目标创建流程

```text
入口认证 / CSRF / ProjectWriteActor / 请求校验
  → 确认事务：复核原授权，规范化意图，查询幂等作用域
  ├─ 已存在：验证原身份/意图 → 最终认证 → 返回原 Run
  └─ 不存在：最终认证 → 锁外解析精确任务/输入
       → 创建事务：再次复核原授权，先查同键胜者
       → 无胜者才解析/冻结资源并创建，唯一冲突则读取胜者
       → 最终认证 → commit 后交付结果
```

- 所有出口复核当前授权；重放另验原 actor，历史查看权不能代替。
- 先重放、后解析当前版本/资源；原 Run 不重新枚举、调用 Provider 或更新快照。新建另验输入/资源/策略，并在写入事务重新固定版本可用性。
- 一次提交 Run、Segment 1、全部快照、初始事件和 dispatch Outbox；失败整体回滚，模型/大文件物化在锁外。
- 唯一约束决定并发胜者，只接受其完整首次快照。解析失败也查询同键胜者，未见则返回原错误；不无限等待或换键。

确认不保证可执行：Provider 仍验冻结授权、Integration 状态和批准，版本停用后的确认不许可新建。

### 创建与确认的授权事务

普通请求以内部 UserAccess 保留原 cookie/CSRF、服务器 request UUID；不改 HTTP/持久身份格式，不只凭入口 actor 或换用该用户的新会话。actor 须匹配原会话，首次权限取锁内当前角色/成员，不接受调用者角色或 M0 默认授权。

```text
Organization UPDATE → 当前 User SHARE → 原 AuthSession UPDATE
  → Project SHARE → USER 的 ProjectMember SHARE
  → 原请求查询 / 首次快照写入 → flush → 最终认证 → commit
```

复用[共同凭据校验](authentication.md#认证与业务提交不是同一个事务)，身份锁、Project/成员锁及最终 flush 后取新时间。ADMIN 免 membership，不免组织/会话/归档检查。会话失效 401、CSRF 403、无权/不存在 404，已授权归档项目 409；不更新 User 或延长会话。

重放、解析失败/唯一冲突后的胜者、新建及独立查询命中/未命中均走同一授权出口；选择/来源/幂等拒绝也复核，不提交部分写入。查询未命中不授权下一事务，Skill 解析失败仍用原凭据查询胜者。

首次保存 Skill snapshot 前，[共享可用性检查](skill-interpretation.md#resourcebinding-与-readiness)依序持有精确 SkillVersion SHARE、ProjectSkillVersion SHARE 至事务结束，核对组织/Project、PUBLISHED、未停用。SHARE 兼容 occurrence 外键锁；停用/废弃先提交则整笔拒绝，同键原 Run 不重验可用性。

flush 后失效整体回滚，不改历史胜者。DB 异常、取消、commit 未知不自动重发；最终认证不保证物理 commit 瞬间未过期。创建响应使用 no-store。

调度使用[持久 occurrence participant](task-scheduling.md#持久认领与结算)，验证当前创建者/原 claim 并同事务结算，不依赖浏览器会话；UserAccess 不授予调度新建资格。此锁协议不代替文档引用保护或 Integration 配置竞争控制。

## 响应、界面与历史兼容

| 响应 | 含义 |
| --- | --- |
| 201 + Location | 首次创建成功，不代表执行成功 |
| 200 + Idempotent-Replay: true | 原 Run 的当前状态，不新增事件 |
| 409 idempotency_conflict | 同作用域的意图不同或旧身份无法证明；核对后显式新建 |
| 断网/超时/无法校验响应 | 提交未知，不能自动换键 |

### 提交结果未知时的界面责任

页面分开 TaskDraft 与已发送请求；提交固定 actor/Project/精确任务/input/sources/key，确认只发送原内容，不复制服务端 hash。

| 状态 | 界面行为 |
| --- | --- |
| sending | 同步防重；关闭弹窗后重新打开仍能查看 |
| unknown | 网络错误、30 秒等待超时、408/429/服务错误或无效响应；原键确认或新标签页核对历史 |
| rejected | 其余非 409 的 4xx；只说明本次拒绝，不清除先前“可能已创建”事实 |
| conflict | 不自动重试、刷新原内容或换键；明确核对后新建 |

确认不依赖草稿是否合法；另建前提示原 Run 可能存在，由用户明确提交新意图。30 秒只结束 HTTP 等待，不取消 Run。

重发用当前 CSRF 和原 actor/Project；每次 await 后验身份，合法回执才切换 detail/SSE。切换账号/Project、离页使旧响应失效。原请求只存内存：关弹窗保留，刷新/离页不保证恢复，正文/token 不进 URL/Web storage；后续拒绝不能抹去先前未知。

### 历史身份的证明范围

旧 request_hash 早于 binding 字段，不能直接重算 selected_sources。[兼容器](../../SKM/backend/src/skillmind/runs/creation_replay.py)仅移除可证明为初次补入的 binding_id/binding_checksum/binding_capability，用完整旧 hash 验证后，从保存的 candidate/default 恢复意图。

不读当前资源、不改旧行；缺原选择、未知版本或 hash 不符返回 409。历史可读、创建可确认、非终态可按新输入协议续行分别判断。

## 当前代码与实施入口

| 责任 | 入口 |
| --- | --- |
| 意图/持久格式 | [creation_request](../../SKM/backend/src/skillmind/runs/creation_request.py)；task_snapshot_json.creation_request 保存 v1，无独立公开字段 |
| 重放/创建/胜者 | [service](../../SKM/backend/src/skillmind/runs/service.py)、[repository](../../SKM/backend/src/skillmind/runs/repository.py) |
| Web 原请求 | [runSubmission](../../SKM/web/src/lib/runSubmission.ts)、[useRunSubmission](../../SKM/web/src/hooks/useRunSubmission.ts) |
| Schedule | [ScheduleService](../../SKM/backend/src/skillmind/schedules/service.py)提供 occurrence participant，不另建 Run/hash |

内部身份版本不等于 HTTP 版本；同步要求见[契约 workflow](../development/contract-workflow.md)。

## 验收清单

覆盖全集变化不改重放、集合换序/改 ID、并发唯一完整胜者、解析失败确认、跨 actor/权限拒绝、停用区分新建/确认、旧 hash 拒绝。各出口检验撤销、锁/flush 过期、移除成员、归档与整笔回滚；commit 未知不换键。

Web 验同 tick 防重、丢响应、草稿/身份切换及原键确认；调度另验[关联/计数](task-scheduling.md#实现与验收)。入口：[Backend](../../SKM/backend/tests/runs/)、[浏览器](../../SKM/web/tests/browser/check_run_submission.py)；真实事务与浏览器时序分别举证。
