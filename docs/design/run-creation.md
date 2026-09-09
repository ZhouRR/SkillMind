# Run 创建、重发与幂等

本页定义创建身份、首次快照与原请求确认。Backend 版本化重放和 Web 确认已有接线；真实事务验收与调度缺口见[计划](../planning/roadmap.md)。资源成员规则由[资源快照](resource-snapshots.md)负责。

## 先分清三种“再执行”

| 意图 | 正确结果 |
| --- | --- |
| 创建响应丢失，确认原请求 | 返回原 Run，不新增快照、Segment 或 dispatch |
| Worker 故障恢复 | 同 Segment 新增 Attempt，沿用冻结事实 |
| 用户改变输入/范围/目标 | 新键、新 Run，不改旧快照 |

答复/批准是已有 Run 的业务续行；Schedule 每个 occurrence 独立创建，同一 occurrence 的恢复不是新触发。

## 请求身份与执行快照分开保存

请求身份保存“用户要创建什么”，执行快照保存“首次实际固定什么”。两者都不可在重放时改写。

| 内容 | 身份 / 首次快照 |
| --- | --- |
| actor、Project、精确 SkillVersion/task_key | 全部参与身份；快照另存 Manifest checksum |
| input、显式选项 | 按版本化规则比较；保存验证后的输入/Schema，不任意排序数组、转型或补默认值 |
| 文档单份/集合 | 比较 ID；集合排序且拒绝重复；快照固定成员、路径、MIME、size/hash |
| 文档全集 | 比较“全集”规则，不重新展开；首次成员单独冻结 |
| Integration/binding | 比较显式身份或默认选择规则；冻结实际 scope/配置版本/binding，不重读今天的默认值 |
| 权限/额度与追踪 | 上限由服务端冻结；trace、时间、生成 ID 不造成请求差异 |

文档必需显式选择，省略可选槽位不授权全集；Integration 可按 Task/Project 默认 binding 解析。省略与显式指定是不同意图。未来开放限额选项须版本化纳入身份。

服务端用[共享 hashing](../../PJM/backend/src/projectmind/core/hashing.py)保存规范化意图、摘要及格式版本；客户端 fingerprint 不能代替认证或原内容比较。

### 幂等键的作用域

唯一约束为 (project_id, task_id, Idempotency-Key)，task_id 从精确 SkillVersion/task_key 导出，不按 actor 隔离。作用域内同键不同 actor/input/选择冲突；不同 Project/精确任务不承诺冲突。每个新意图仍生成新键，不能使用常量。

`schedule:` 前缀保留给持久 occurrence 的内部创建。普通入口可以确认已有同身份/内容的原 Run，但不存在时返回 409 idempotency_conflict，不用该前缀新建；避免手动请求绕过调度关联、重叠和计数。旧键/快照不改写，发布时 API 与 Worker 一起切换，旧 API 也不能混用。

## 目标创建流程

以下流程已接入，仍需真实 PostgreSQL 并发/回滚验收：

```text
入口认证 / CSRF / ProjectWriteActor / 请求校验
  → 确认事务：复核原授权，规范化意图，查询幂等作用域
  ├─ 已存在：验证原身份/意图 → 最终认证 → 返回原 Run
  └─ 不存在：最终认证 → 锁外解析精确任务/输入
       → 创建事务：再次复核原授权，先查同键胜者
       → 无胜者才解析/冻结资源并创建，唯一冲突则读取胜者
       → 最终认证 → commit 后交付结果
```

- 所有请求都做当前授权；无权限者不能用 key 探测旧 Run。重放要求原 actor，不因有历史查看权即可冒充。
- 重放先于当前资源/版本解析，不再枚举文档、调用 Provider 或改写权限快照；仍复核当前访问资格。新建才检查版本可用、输入、资源与策略。
- 创建事务一次保存 Run、Segment 1、Skill/资源快照、初始事件与 dispatch Outbox；失败整体回滚，模型/大文件物化在事务外。
- 预查询不替代唯一约束。并发同意图只接受胜者的首次快照，不拼接两次解析。
- 解析失败时复查刚提交的同键胜者；仍不存在则返回原错误，不无限等待。再次确认继续保留原键。

确认已有 Run 不等于它仍可执行。运行时 Provider 继续验证冻结授权、Integration 状态与批准；版本停用后的旧请求确认也不许可新建。

### 创建与确认的授权事务

普通请求必须传入内部 UserAccess，保留原 cookie、CSRF 和服务器 request UUID；不能只凭入口 actor 或另选该用户的新会话。它不改变 HTTP 正文，也不是新的持久身份格式。请求 actor 必须与原会话一致；首次权限快照使用锁内当前角色及成员资格，不接受调用者提供角色或 M0 默认授权。

```text
Organization UPDATE → 当前 User SHARE → 原 AuthSession UPDATE
  → Project SHARE → USER 的 ProjectMember SHARE
  → 原请求查询 / 首次快照写入 → flush → 最终认证 → commit
```

复用[共同凭据校验](authentication.md#认证与业务提交不是同一个事务)，在身份锁后、Project/成员锁后和最终 flush 后取新时间。ADMIN 无需 membership，但仍受组织、原会话与归档限制。失效会话 401、CSRF 403、无权/不存在 404；仅已授权的归档项目返回 409。User 使用只读共享锁，不更新账户或延长会话。

首次重放、资源解析失败后的胜者、唯一冲突胜者、新建和独立查询的命中/未命中均经过同一出口；查询未命中不授权随后跨事务的创建。选择语法、来源与幂等冲突拒绝也复核当前资格，但不提交部分写入。Skill 解析失败后仍用原凭据再次查询，不能因旧入口认证已通过就直接返回旧结果。

最后 flush 可能等待约束；其后失效则整体回滚，历史胜者及快照不改写。未知 DB 异常、取消和 commit 响应未知保留原语义，不自动重发，也不宣称物理 commit 瞬间会话一定尚未过期。处理过的创建响应使用 no-store。

调度必须显式传入[持久 occurrence participant](task-scheduling.md#持久认领与结算)，继续校验当前创建者与原 claim 并同事务结算，不依赖浏览器会话存活。浏览器 UserAccess 不具有 `schedule:` 新建资格。上述锁协调项目/成员管理，不代替单文档删除引用保护、Integration 配置竞争或真实 PostgreSQL 验收。

## 响应、界面与历史兼容

| 响应 | 含义 |
| --- | --- |
| 201 + Location | 首次创建成功，不代表执行成功 |
| 200 + Idempotent-Replay: true | 原 Run 的当前状态，不新增事件 |
| 409 idempotency_conflict | 同作用域的意图不同或旧身份无法证明；核对后显式新建 |
| 断网/超时/无法校验响应 | 提交未知，不能自动换键 |

### 提交结果未知时的界面责任

[WorkspacePage](../../PJM/web/src/pages/WorkspacePage.tsx)把可编辑 TaskDraft 与已发送请求分开。提交时固定 actor/Project/精确任务/input/sources/key，确认只发送原内容，不复制服务端 hash 算法。

| 状态 | 界面行为 |
| --- | --- |
| sending | 同步防重；关闭弹窗后重新打开仍能查看 |
| unknown | 网络错误、30 秒等待超时、408/429/服务错误或无效响应；原键确认或新标签页核对历史 |
| rejected | 其余非 409 的 4xx；只说明本次拒绝，不清除先前“可能已创建”事实 |
| conflict | 不自动重试、刷新原内容或换键；明确核对后新建 |

草稿继续可编辑，原请求确认不依赖草稿现是否合法。另建前明确提示原 Run 可能存在，再由用户提交新意图。30 秒只结束 HTTP 等待，不取消 Run。

重发用当前 CSRF，但必须仍是原 actor/Project；每次 await 后检查请求身份。切换账号/Project、离页后不处理旧结果，确认合法响应后才切换 detail/SSE 观察对象。

待确认内容只存页面内存：关弹窗保留，刷新/离页/身份切换不保证恢复；正文和 token 不进 URL/Web storage。未知后再收到拒绝不能抹去不确定性。

### 历史身份的证明范围

旧 request_hash 计算早于 binding 字段补入，不能直接对持久 selected_sources 重算。当前[兼容器](../../PJM/backend/src/projectmind/runs/creation_replay.py)仅移除能证明属于初次初始化的 binding_id / binding_checksum / binding_capability，以完整旧 hash 验证剩余快照，再从保存的 candidate/default 恢复意图。

不读当前资源、不改旧行；缺原选择、未知版本或 hash 不符返回 409。历史可读、创建可确认、非终态可按新输入协议续行分别判断。

## 当前代码与实施入口

| 责任 | 入口 |
| --- | --- |
| 意图/持久格式 | [creation_request](../../PJM/backend/src/projectmind/runs/creation_request.py)；task_snapshot_json.creation_request 保存 v1，无独立公开字段 |
| 重放/创建/胜者 | [service](../../PJM/backend/src/projectmind/runs/service.py)、[repository](../../PJM/backend/src/projectmind/runs/repository.py)；API 在解析当前任务前先查询 |
| Web 原请求 | [runSubmission](../../PJM/web/src/lib/runSubmission.ts)、[useRunSubmission](../../PJM/web/src/hooks/useRunSubmission.ts) |
| Schedule | [ScheduleService](../../PJM/backend/src/projectmind/schedules/service.py)以内部 participant 在同一创建/确认事务校验当前授权与持久 claim，并关联原 occurrence、幂等计数；不另建 Run/hash |

公开修改同步 Problem/response、Schema/example/OpenAPI、Web validator 和测试。内部身份版本不等于 HTTP 版本；仅 DB schema 变化才加 migration。

## 验收清单

验收原全集在新增/删除后重放不变；集合换序重放、改 ID 冲突；并发创建只留一个完整初始事务；解析失败能确认并发胜者；换 actor/越权不泄露；版本停用区分确认与新建；旧 hash 无法证明时拒绝兼容。各创建/确认出口另验原会话撤销、锁等待/flush 过期、成员移除与归档，确认拒绝时无部分初始写入，commit 未知不换键。

Web 另验同 tick 防重、丢响应、编辑草稿、身份切换及原键确认。Schedule 同 occurrence 不重建/不重复计数属于[调度验收](task-scheduling.md#实现与验收)，不是创建服务独自保证。

测试入口：[Backend runs](../../PJM/backend/tests/runs/)、[原请求浏览器回归](../../PJM/web/tests/browser/check_run_submission.py)。数据库竞争与浏览器时序分别执行，mock API 不证明事务可靠。
