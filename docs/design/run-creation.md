# Run 创建、重发与幂等

> 定位：Run 创建身份、重放与兼容的设计正本。工作副本已有 Backend 版本化意图/重放和 Web 原请求确认；实现入口见 §5，当前验收范围见[计划 R01 / R09](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)。资源成员与内容的冻结规则由[资源快照](resource-snapshots.md)负责，本文不把局部重试能力等同于完整资源或调度验收。

## 1. 先分清三种“再执行”

| 用户或系统意图 | 应发生什么 | 不应发生什么 |
| --- | --- | --- |
| 创建响应丢失，重发同一请求 | 返回首次成功创建的 Run，不新增 Segment、快照或 dispatch | 因“全集”新增文件而创建另一份输入 |
| Worker 故障，恢复已有 Run | 同一 Segment 追加 Attempt，沿用 Run 冻结事实 | 重新调用创建 API 获得新权限或新预算 |
| 用户改变输入、选择或执行目标 | 使用新幂等键创建新 Run | 修改旧 Run 快照，让旧请求看似相同 |

用户答复和批准属于已有 Run 的业务续行，见[生命周期](agent-runtime.md#71-生命周期)。Schedule 的每个 occurrence 是一次独立创建；同一 occurrence 的恢复不是新的业务触发。

## 2. 请求身份与执行快照分开保存

请求身份回答“是不是同一次创建意图”，执行快照回答“第一次实际批准并固定了什么”。两者都不可随重放改写，但不能使用同一份动态资源展开结果替代。

| 内容 | 请求身份 | 首次执行快照 |
| --- | --- | --- |
| 发起人、Project、精确 SkillVersion 与 task_key | 必须参与；不把当前登录者当成原发起人 | 保存实际 actor 与精确版本、Manifest checksum |
| input 与显式执行选项 | 按版本化规则规范化后参与 | 保存验证后的输入及所依据的 Schema |
| 单份/集合文档选择 | document ID；集合排序，重复项拒绝 | 原 ID、成员清单、路径、MIME、size、内容 hash |
| 显式项目全集 | “全集”这个选择规则参与，不实时展开后比较 | 首次创建取得的具体成员清单 |
| Integration / binding 选择 | 显式资源身份或明确的默认选择规则 | 实际解析的 scope、配置版本、Run binding 与 checksum |
| 权限与额度 | 当前请求不接受权限/额度字段；未来若开放限制选项，须版本化纳入身份 | 服务端独立计算并冻结上限；重放不覆盖 |
| trace、创建时间、生成的 Run/binding ID | 不作为业务请求差异 | 作为追踪或持久身份分别保存 |

规范化不是“尽量宽松”：input 的数组顺序、数字/字符串类型和缺省值有业务含义，不擅自排序、转型或填默认值。只有契约明确声明为集合的选择可以规范排序。

省略选择不能一概解释为“不授权”：document 必须显式选择，省略可选文档槽位不会退回全集；Integration 当前可以解析 Task/Project 默认 binding。省略与显式指定 Integration 是不同意图，重放省略选择的旧请求也不会重新读取今天的默认配置。来源解析仍须按 requirement 校验，不能把未知选择当成空值。精确资源语义见[资源快照](resource-snapshots.md#文档选择与冻结设计)。

请求摘要由服务端使用[共享 canonical JSON / hashing](../../PJM/backend/src/projectmind/core/hashing.py)生成，并记录身份格式版本。摘要不能代替认证，也不允许客户端提交 fingerprint 来跳过比较。当前内部载体见 §5；保存规范化原意图，不只保留无法解释的 hash。

### 幂等键的作用域

现有唯一约束是 `(project_id, task_id, Idempotency-Key)`；`task_id` 由服务端从精确 SkillVersion 与 task_key 导出。它不是全平台唯一键，也不按 actor 自动隔离。

因此，同一作用域、同键但不同 actor/input/资源选择必须冲突；不同 Project 或不同精确任务属于另一个作用域，不承诺也返回 409。客户端每次新的创建意图仍应生成新键，不能依赖这种分区反复使用常量。作用域定义如要改变，须另行同步 API、唯一约束与历史兼容。

## 3. 目标创建流程

以下顺序已进入 Backend 工作副本；§5 区分实现入口和仍未闭合的验收。图中“重放”只确认原 Run，不开始模型执行。

```text
认证 + CSRF/Origin + 当前 Project 授权 + 请求结构校验
                         ↓
                规范化创建意图，定位幂等作用域
                         ↓
                  查询首次成功提交的记录
                   ┌─────┴─────┐
                 已存在       不存在
                   ↓             ↓
           校验原身份/意图    解析当前精确任务与合法资源
              ┌────┴────┐        ↓
             相同      不同   事务内冻结并插入 Run/Segment/Outbox
              ↓         ↓         ↓
          返回原 Run   409    提交成功，或唯一冲突后读取胜者
```

1. 所有请求都经过当前身份与 Project 访问检查；无权限者不能通过猜测幂等键发现旧 Run。
2. 已有记录按保存的身份格式和精确任务验证原请求。重放只返回原 Run，不重新调用 Provider、枚举文档或刷新权限快照。
3. 新请求才检查当前版本是否允许新建、输入是否有效、必需资源是否就绪，并冻结服务端生成的执行事实。
4. Run、Segment 1、Skill/资源快照、初始事件和 dispatch Outbox 必须在同一创建事务中完成；失败全部回滚，不留下可派发的半成品。模型执行和大文件物化不放入该事务。
5. 预查询不能代替数据库唯一约束。两个同意图请求并发时，仅胜者的首次快照生效，另一方返回胜者；不同意图仍冲突。
6. 若当前任务/输入解析或资源解析失败，但期间另一请求已提交同键，应在保持授权检查的前提下重新确认已提交记录；不能把并发胜者的存在误报为前置缺失，也不能隐藏真正的新请求错误。这次复查不是无限等待：仍未观察到提交时返回当前错误，调用方保留原键以便再次确认。

版本停用或资源删除对“新建”和“返回已有记录”作用不同。当前 API 已先查询原请求，再解析新建所需的已发布任务。所有 POST 仍经过 `ProjectWriteActor` 和 CSRF/Origin，原 actor 必须匹配；不是只要有历史查看权就允许冒充原请求重放。新建仍受当前版本启用、输入、资源与安全策略约束。

重放不撤销运行时的授权与可达性再验证：既有 Run 的 Provider 仍按冻结边界、Integration 状态和有效批准判断是否执行。确认旧 Run 存在，不代表原资源现在仍可读取或写入。

## 4. 响应、界面与历史兼容

| 情况 | 对调用方的要求 |
| --- | --- |
| 首次提交成功 | 当前 API 使用 `201`，返回 Run ID 和 Location；创建成功不表示执行成功 |
| 同请求已成功提交 | `200` 与 `Idempotent-Replay: true`，返回原 Run 的当前状态；不重发初始事件 |
| 同作用域、同键、意图不同 | 沿用 `409 idempotency_conflict`；UI 提示核对或显式启动新的 Run |
| 网络中断，不知道是否提交 | 保留该次请求内容和键；先重发/确认，不自动生成新键碰碰运气 |
| 资源或权限已失效 | 新建按现行 Problem 契约拒绝；已有记录确认与运行时失败分开展示 |

共享的立即执行/调度输入组件应保存用户选择的语义，Run detail 展示首次冻结的资源范围。可见字段通过 response allowlist 与公开 Schema 明确列举，不直接发布内部 JSON、Secret locator 或完整权限结构。

### 提交结果未知时的界面责任

当前 [WorkspacePage](../../PJM/web/src/pages/WorkspacePage.tsx) 已把可编辑 TaskDraft 与已发送请求分开。首次明确提交时固定 actor、Project、精确任务、input、sources 和幂等键；再次确认发送原内容/原键，不读取今天的草稿或重新取得的候选。Web 不复制服务端 fingerprint 算法。

```text
可编辑草稿
  ↓ 固定请求内容与键
提交中
  ├─ 201/200：已确认，观察 Run
  ├─ 结果未知：原内容、原键重试
  └─ 拒绝/409：核对原因与历史
```

| 提交状态 | 表示什么 | 可执行的操作 |
| --- | --- | --- |
| `sending` 提交中 | 本次 HTTP 正在等待结果，不是 Run 状态 | 禁止重复发送；可关掉弹窗，重新打开仍能查看 |
| `unknown` 结果未知 | 断网、30 秒响应等待超时、408/429、服务错误，或响应无法通过 JSON/Run 校验 | 用原请求再次确认，或先在新标签页核对历史 |
| `rejected` 本次被拒绝 | 除 408/409/429 外的 4xx；只证明这次请求被拒绝 | 修复认证等问题后可原键确认；不能抹掉之前可能已创建的事实 |
| `conflict` 原键冲突 | 409；可能是不同意图，也可能是旧记录无法安全比较 | 核对原因与历史，不提供无意义的自动重试或自动换键 |

当前实现守护以下边界：

- HTTP 的 30 秒上限只停止等待响应，不是 Worker `wall_timeout_seconds`，也不是取消 Run。晚到响应不得覆盖较新的请求或账号/Project 上下文。
- 草稿可继续编辑，但原请求确认按钮不受草稿当前是否合法影响。需要用当前草稿另开 Run 时，先明确确认“原执行可能已创建”，再由用户点击新建；重复点击提交不能生成第二个键。
- 一旦出现过未知结果或冲突，后续 401/403 等拒绝不能清除“原 Run 可能存在”的提示。重发使用当前 CSRF，但仍属于原 actor/Project；切换身份或 Project 后不展示、发送或接收旧提交。
- 待确认内容只在页面内存中保存。关闭弹窗不丢失；刷新、离页或账号/Project 切换后不自动恢复。历史链接在新标签页打开，避免核对历史时立即卸载原页面。未来跨页持久化须另行设计敏感输入的保留/清理，不把请求正文或 session token 写入 Web storage。
- 确认合法 Run 响应后才切换到该 Run 的现有 detail/SSE 观察流程；新请求尚未确认时，不把当前正在观察的 Run 清空，也不创建第二套 lifecycle。

原请求确认、文档选择和清单展示各有独立责任，不能用一种重试状态替代全部验收。浏览器 fixture 与真实 Backend/数据库验收分别举证，见 §6。

### 历史身份的证明范围

历史记录不可用今天的资源反推旧请求。旧创建路径在同一事务后半段补入 Run binding ID/checksum，而旧 `request_hash` 计算于补入之前；不能对持久化的 selected_sources 直接重算并宣称那就是旧身份。

当前 [creation_replay](../../PJM/backend/src/projectmind/runs/creation_replay.py) 区分新版意图与旧格式。旧路径只移除可证明是初次初始化补入的三个 Integration 字段（`binding_id`、`binding_checksum`、`binding_capability`），用完整旧 hash 验证剩余冻结事实，再从保存的 candidate/default 证据恢复意图。它不查询当前资源，也不修改旧行。

旧文档缺少可证明的原选择、未知身份版本或 hash 不符时，返回 `409 idempotency_conflict`；历史读取不因此被改写。能够确认原创建，不等于旧非终态 Run 已满足新版文档读取或可信缓存要求，两者分别验收。

## 5. 当前代码与实施入口

以下为 2026-09-08 工作副本的只读核对，不是部署验收。

| 责任 | 工作副本中的实现 |
| --- | --- |
| 意图规范化 | [TaskRunIntent](../../PJM/backend/src/projectmind/runs/creation_request.py) 固定 actor、Project、精确任务、input、sources；只规范明确的 UUID/文档集合选择 |
| 内部持久载体 | `task_snapshot_json.creation_request` 保存 `request_version: v1` 与上述意图；未新增公开字段或独立数据库列 |
| 身份与兼容 | [request_hash](../../PJM/backend/src/projectmind/runs/domain.py) 新格式比较意图并校验快照身份；无版本意图的旧 command 保持旧 hash 路径；[creation_replay](../../PJM/backend/src/projectmind/runs/creation_replay.py) 负责保存记录的验证 |
| API 与服务 | [route](../../PJM/backend/src/projectmind/api/routes/runs.py) 在解析当前任务前查询重放；[RunService](../../PJM/backend/src/projectmind/runs/service.py) 在展开资源前再次查询，并在解析失败后复查并发胜者 |
| 并发最终裁决 | [RunRepository](../../PJM/backend/src/projectmind/runs/repository.py) 保留数据库唯一约束、同事务初始记录/Outbox与冲突后读取胜者 |
| Schedule 入口 | [ScheduleService](../../PJM/backend/src/projectmind/schedules/service.py) 当前先授权、再查原键、再查重叠；观察到重叠或任务解析失败后也复查原键 |
| Web 请求规则 | [runSubmission](../../PJM/web/src/lib/runSubmission.ts) 保存不可变原请求、判定结果与显式新建；与可编辑 [taskDraft](../../PJM/web/src/lib/taskDraft.ts) 分离 |
| Web 请求生命周期 | [useRunSubmission](../../PJM/web/src/hooks/useRunSubmission.ts) 负责响应等待、原键确认、上下文清理与晚到结果；HTTP 仍走共享 API client |
| 用户确认入口 | [RunSubmissionPanel](../../PJM/web/src/components/RunSubmissionPanel.tsx) 展示状态、历史与新建确认；[WorkspacePage](../../PJM/web/src/pages/WorkspacePage.tsx) 只在确认后切换观察对象 |

文档选择与公开清单的消费者已接入，见[资源设计](resource-snapshots.md#读取清单和资源摘要)；真实 PostgreSQL 并发/回滚仍须验证。调度的稳定重放入口不提供持久在途 occurrence，也不保证计数只增加一次；这些是[调度设计](task-scheduling.md)的独立责任。各轮检查见[计划](../planning/roadmap.md#13-当前执行状态)，不把本表当作部署或全项目通过证据。

后续更改公开面时同步 API Problem/response、Schema/example/OpenAPI、Web validator/共享输入和测试。内部身份版本不等于公开 API 版本；只有实际 DB schema 变化才增加 migration。

## 6. 验收清单

| 给定条件与操作 | 必须观察到的结果 |
| --- | --- |
| 全集创建后新增/删除文档，再发原请求 | 相同 Run ID、首次成员与 checksum 不变，无额外 Outbox |
| 同一显式集合换序；或换掉其中一个 ID | 前者按规范化规则重放，后者同键冲突；重复 ID 拒绝 |
| 两请求并发，资源目录在期间变化 | 同意图只提交一个 Run/完整初始事务，不拼接两次解析结果 |
| 资源解析失败时另一请求刚提交 | 可确认相同意图的已提交结果；不存在胜者时保持原失败 |
| actor 变化、Project 越权、伪造 fingerprint | 不重放他人的创建身份，不泄露原 Run 或放宽授权 |
| 精确版本停用，旧 Run 仍存在 | 区分旧记录确认与新建；不执行被停用版本的新 Run |
| 创建响应丢失，再点击确认、编辑草稿或刷新 | 原内容/键不被替换；确认旧 Run 后才观察，新意图明确告知可能已有执行 |
| 相同 Schedule occurrence 重试 | 返回已关联 Run，不误判为自身重叠，不重复计数 |
| 旧记录只有旧 hash / 缺少清单 | 历史可读；安全比较不可证明时不伪造兼容或自动新建 |

单元测试不足以证明唯一约束、事务回滚与并发可见性；这些场景还须经过真实 PostgreSQL 回归。测试入口为[runs](../../PJM/backend/tests/runs/)、[API](../../PJM/backend/tests/api/)、[DB](../../PJM/backend/tests/db/)与[schedules](../../PJM/backend/tests/schedules/)。

Web 的[原请求规则测试](../../PJM/web/tests/lib/runSubmission.test.ts)、[面板测试](../../PJM/web/tests/components/RunSubmissionPanel.test.tsx)和[浏览器场景](../../PJM/web/tests/browser/check_run_submission.py)分别检查纯状态、三语展示与真实 React 交互。浏览器场景使用全面 mock API，不连接实际数据库或模型；运行方式见[本地开发](../development/local-development.md#原要求確認のブラウザ回帰)。
