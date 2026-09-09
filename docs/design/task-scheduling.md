# TaskSchedule 设计

Schedule 只决定何时、以谁和哪份配置调用普通 [Run 创建](run-creation.md)，不成为第二引擎。持久发火、项目管理与在途只读核对已接入；真实事务、人工处理与历史迁移仍待验收，状态见[计划 R09](../planning/roadmap.md#r09-调度)。

## 一页速览

| 概念 | 边界 |
| --- | --- |
| Schedule / Run | 前者存规则，每次成功触发创建独立 Run |
| occurrence / 实际开始 | occurrence 是计划时刻，tick 与 Run 都可能晚到 |
| 暂停 / 取消 | 暂停阻止后续认领，不撤回在途；已有 Run 单独取消 |
| 幂等 / 恢复 | 原键抑制重复，持久 occurrence 保存原配置；有界恢复不承诺无限重试 |
| 不批量补跑 / 不迟到 | 当前处理一个到期 occurrence，其余计 missed，无最大迟到拒绝 |

## 一个例子：规则、触发与执行分别看

08:00 创建 Run A，A 等批准；09:00 同 Schedule 触发因重叠跳过。此时 status=ACTIVE、last_run_at=09:00、last_outcome=SKIPPED_OVERLAP、last_run_id=A、run_count=1 可以同时成立。

last_run_at 是最近回写的计划时刻，不是 A 开始时间；last_run_id 保留最近成功关联，跳过不清空；run_count 不是成功数。last_* 是会被覆盖/滞后的摘要，不是完整触发账本。

## 生命周期与发火

```text
保存规则（仅 ACTIVE 参与 tick）
  → TX A：认领 PENDING occurrence
       冻结原配置 / lease
       推进 next_run_at
  → 当前授权 / 查询原键
       无原 Run 才检查重叠和前置条件
  → TX B：锁内复核当前授权与 claim
       普通 Run 创建 / 原请求确认
       同时关联、结算、更新摘要与计数
已知无 Run：独立短事务结算
```

认领与 Run 创建仍是两个事务；A 后崩溃从原 PENDING 恢复。Run 的初始快照、事件/Outbox 与 occurrence 关联同事务，不能把创建返回当成已提交。基础设施异常或提交响应未知保留原记录，由后续原键确认，不伪造失败或退款。

支持 ONCE/CRON、必填 IANA timezone、分钟粒度；定义/预览/发火共用求值。ONCE 一次、CRON 通常展示至少三次，end_at/max_runs 限制不足时不虚构。状态 ACTIVE/PAUSED/COMPLETED/ERROR/ARCHIVED；修改保留状态，ERROR 修正后显式恢复，从当前时刻重算，不追赶暂停历史。COMPLETED 可仍有关联非终态 Run。

CRON 跳过 DST 不存在时刻、重复时刻取第一次；ONCE 规范到分钟，持久身份使用 UTC occurrence，不用浏览器文字生成键。代码见 [cron](../../PJM/backend/src/projectmind/schedules/cron.py)。

## 保存和执行边界

| 项目 | 规则 |
| --- | --- |
| 任务/输入 | 保存精确 SkillVersion/task_key、input/sources 规则，不跟 latest；发火才冻结本次资源 |
| 身份 | 发火前重新检查创建者有效性/Project 权限 |
| 幂等 | Schedule ID + UTC occurrence；先查原 Run，再判断新建 |
| 重叠 | 查同 Schedule 的全部 occurrence 关联 Run，非终态含等待均跳过 |
| 失效 | FAILED_PRECONDITION / ERROR，修正后显式恢复，不换来源 |
| 结束 | ONCE 一次机会；CRON 的 max_runs 是累计创建额度，跳过不消耗额度 |

调度不绕过批准、资源再验证或审计。文档全集每个新 Run 固定当时成员；单份/集合保持原 ID，同 occurrence 重放原快照。

### 管理写入的授权事务

创建、编辑与状态修改保留 ProjectWriteActor 入口检查，并在保存事务复用[原会话校验](authentication.md#认证与业务提交不是同一个事务)。只传原请求的 cookie/CSRF 与服务器 request UUID，不接受另选会话；UserAccess 是内部参数，不改变 HTTP 正文。Skill/输入解析在锁外，解析期间撤权仍须在写入前拒绝。

```text
Organization UPDATE → 当前 User SHARE → 原 AuthSession UPDATE
  → Project SHARE → USER 的 ProjectMember SHARE
  → 既有 Schedule UPDATE → 写入 / flush → 最终认证 → commit
```

创建不取既有 Schedule 锁；ADMIN 仍限本组织，但无需 membership。取得 Project/成员、Schedule 锁后以及最终 flush 后，以新时间复核原 token/CSRF、登录角色、撤销和期限，再检查成员与归档：失效会话 401、CSRF 403、无权/不存在 404，只有已授权的归档项目返回 409。判断使用锁内当前身份，不沿用入口 actor 的旧角色。

User 只读共享锁阻止停用/角色变更，同时兼容认领事务插入 occurrence 所需的 User 外键锁；不能升级成账户写入，否则可能与 Schedule 反向等待。锁保持到事务结束，原 CAS、终态与已用/占用额度仍在 Schedule 锁内检查。最终复核失败则整笔回滚；commit 响应未知仍是未知，不自动重发。最终认证点不等于保证物理 commit 瞬间未过期。

该边界只覆盖三类管理写入，不证明读取期间撤权立即生效或在途 Run 已停止；Worker 发火继续按当前创建者资格和持久 claim 授权，不依赖浏览器会话存活。

创建和编辑在同一门禁内重新校验锁外已验证的文档选择，只读取元数据，不持锁调用 blob/Skill/Provider。原 ID 已消失则拒绝保存，不切换为同路径新文档；请求 input/sources 在首次 await 前复制，既有调度先验锁内版本。文档删除与当前规则、所有保留 occurrence 的引用关系见[删除事务](document-lifecycle.md#删除事务与引用判定)。

### 重叠检查到底看谁

[ScheduleService](../../PJM/backend/src/projectmind/schedules/service.py)先确认原 occurrence，只有不存在原 Run 才查同 Schedule 的其他关联。数据库限定每个 Schedule 最多一个 PENDING；它未结算时不认领下一候选，不新增历史补跑队列。

不查同 Task 的手动/其他 Schedule，不扩成 Project/Task 全局执行锁。身份和关联校验只占用数据库事务，不持锁解析 Skill、物化文件或调用模型；新建事务仍须再次 fencing 与重叠检查。

## 保存表单与触发预览

TasksPage/ScheduleDialog 与即时执行共用 TaskLaunchFields/taskDraft，发送实际 input/sources/精确任务，必需文档未选或集合不完整不能保存。预览只验证时间，不授权资源、不创建 Run，也不保证未来发火成功。

保存需要当前 definition 的成功非空服务端预览及人工确认。修改后清确认，A→B→A 也不能复用旧请求的晚到结果。预览只发送 definition，不包含任务正文。

关闭创建弹窗丢草稿；abort/超时不证明 Schedule 未保存，不能当作安全重发。可从独立管理列表查询当前事实，但它没有 Run 创建的原键恢复保证；同名、同配置或页面不可见均不是原请求成败证明。

### 保存后的管理入口

`#/schedules?project=<id>` 独立于模块筛选和 TaskCatalog，默认包含全部状态，保留服务端 total/limit/offset；q 按名称或 task_key 作不区分大小写的字面子串查询，status 指定一个状态。按 created_at、ID 降序稳定排列，tick 更新不改变页序。当前 count/items 分次读取，不承诺原子或跨页快照；并发变化造成页元数据矛盾时要求刷新。任务卡片汇总也读取后续页，遇到总数变化/重复不冒充完整结果。

选中后读取精确 Schedule，再匹配原 SkillVersion/task；任务不再可用仍显示原配置与摘要，编辑只读并说明原因，不换 latest。目录读取失败与任务失效分开，均不抹掉调度列表。归档 Project 只读，调度的 last_* 不解释成完整触发历史。

创建/编辑共用输入字段和时间表单。编辑载入原 name/input/sources/definition，旧来源不静默换候选，未修改的绝对时刻保留精度；不归档重建、不清 run_count。PUT 与状态操作都发送用户看到的 expected_row_version，repository 锁内检查版本、状态和已用/占用额度。

冲突保留草稿，原值、已发送内容与 GET 当前值分开显示；人工采用当前版本后，编辑须重新预览确认，再明确保存。超时/丢响应保留原意图，不自动重发；GET 只核对当前值，不证明原写成功。管理页关闭再打开编辑仍保留未决意图，同目标其他写入暂禁；切换会话/Project/离页不承诺恢复，不写浏览器持久层。

列表/详情/目录/在途读取的 401/403/404 关闭写入资格，旧成功不能复权；共享请求边界处理同步防重、期限与晚到响应。原状态请求的旧客户端若缺版本返回 422，需刷新 Web；不是幂等成功，也不由服务端补最新版本。公开白名单见 [Schedule 契约](../../PJM/contracts/task-schedule/v1.schema.json)，HTTP 声明见 [OpenAPI](../../PJM/contracts/openapi/projectmind-api.v1.json)。

### 在途只读核对

管理详情独立读取 `GET /projects/{project_id}/schedules/{schedule_id}/activity`，按当前读者的 Project 权限授权；不因创建者失效、任务下架或调度暂停/归档而隐藏待结算记录。父配置版本、单个 PENDING 和数据库 `checked_at` 来自同一条 SELECT，不加业务锁、不调用发火或修复。

| 读取结果 | 含义 |
| --- | --- |
| TRACKED + pending | 显示原 UTC occurrence、原/当前配置版本、认领次数/自动上限和租约截止；不是 Run 执行状态 |
| TRACKED + null | 本次读取未见 PENDING；不证明没有 Run、原触发成功或可以重发 |
| LEGACY_UNAVAILABLE | 旧协议无法提供可靠在途追踪，不补空历史、不迁移旧数据 |
| 读取失败 / 409 schedule_activity_unavailable | 保持未核实；损坏关联、快照或多 PENDING 不以第一条/空值掩盖 |

租约是否到期按返回的 `checked_at` 比较，保留微秒；未到期不证明 Worker 存活，到期/次数耗尽不证明执行已停或额度已释放。接管可能只更新 occurrence，不增加 Schedule.row_version，因此每次核对都重新 GET，不能按父版本缓存。刷新不恢复写入资格，也不清除编辑的未知意图。

公开投影不含原请求正文、幂等键、snapshot/hash、worker 或 token；校验原快照与身份后才投影。它不是完整触发历史、原写入回执或人工重试入口；API 不支持此新增查询时显示读取失败，既有详情仍独立保留。

### 时间输入与展示的边界

| 边界 | 规则 |
| --- | --- |
| CRON | 按 definition.timezone 求值，未必是浏览器本地时间 |
| ONCE/end_at 输入 | datetime-local 按明确标注的浏览器时区转绝对时刻；改规则 timezone 不改变输入时区 |
| 预览/摘要 | 按规则时区显示 occurrence 与 UTC offset，不给本地文字拼上另一时区标签 |
| API datetime | run_at/end_at 拒绝无 offset；旧已存绝对时刻不重新解释 |

浏览器 UTC、规则 Asia/Tokyo 时，CRON 09:00 是 UTC 00:00，而 ONCE 输入 09:00 是东京 18:00。非法日期/DST 缺失时刻拒绝，歧义时刻显式选择 offset，不依赖 Date 隐式纠正。改为规则时区输入需另定兼容协议。

## 可靠性保证的限度

### 停机恢复的实际行为

dispatch 开关不停止 Schedule tick，停写须覆盖实际 job/cron/实例，见[部署](../operations/deployment.md)。每小时规则 next_run_at=03:00、09:30 恢复：当前尝试 03:00 一次，04:00–09:00 六次计 missed，下次 10:00；ONCE 也无最大迟到拒绝。

_count_missed 单次最多 1000，长期停机可能截顶。迟到容忍/截止须明确 ONCE/CRON 产品策略再同步实现和 UI，不能临时补跑或改 occurrence。

## 持久认领与结算

采用持久在途触发 → 普通幂等创建 → 幂等结算，不设计第二套 Run 插入/hash，也不批量补发未认领历史。

### 认领记录与恢复权限

认领事务原子校验状态、候选、配置版本，保存原 actor/任务/时间规则/input/sources/UTC occurrence/key，再推进候选。逻辑唯一身份仍 Schedule+UTC occurrence，编辑/接管不换键。

持久 claim 有 worker/token hash/generation/到期时刻，非 RunAttempt；旧持有者不能晚到关联/结算。锁顺序是当前身份/Project → Schedule → occurrence → 普通 Run；Skill 解析在外，创建事务内的资源快照及关联共享短期锁。

恢复顺序：当前授权 → 查原键，有 Run 即关联 → 无 Run 才查本 Schedule 其他在途和非终态关联 → 允许时经普通创建。创建事务也须验证认领仍可新建，不能仅在调用前检查。

普通入口不能用保留的 `schedule:` 前缀新建 Run；已有合法原请求仍可确认，规则见[幂等键作用域](run-creation.md#幂等键的作用域)。否则手动插入可绕开 occurrence 的事务锁、关联与计数。

### 配置并发与暂停

编辑的版本、状态与写入须同原子边界，条件 UPDATE/锁协议核验影响行数；状态按锁内最新事实判定。区分整行 row_version 与语义配置版本，tick 统计不等于改输入。

认领提交是暂停/归档边界：阻止未来认领，不撤回在途；旧 occurrence 按原配置及当前授权完成，不覆盖新定义/PAUSED/ARCHIVED。改公开版本请求时同步 DTO/OpenAPI/Web 冲突与旧客户端兼容。

### 结算、计数与未知结果

同 occurrence 关联/结算幂等，每个不同已关联 Run 只计一次、不等 SUCCEEDED；晚结果可补自身审计/数量，不回退较新摘要。

max_runs 是累计创建额度：认领原子留名额；确认无 Run 的跳过/失败释放，已有 Run 只结一次，创建未知保留并查原键。编辑不清计数，上限不得低于已关联与占用之和。ONCE 一次机会与 CRON 额度耗尽分开。

基础设施错误不是 FAILED_PRECONDITION。当前内部默认 lease 为 60 秒、最多 3 次认领（含首次）；到期才可原键接管，耗尽仍保留 PENDING 和额度，不换键/清计数。自动认领上限与公开投影共用同一策略常量。人工处理和配置化策略仍待接齐，没有自动释放或人工强制重跑入口。

### 历史兼容与实施顺序

0036 新增 occurrence 表、configuration_version 和 occurrence_protocol。旧行/旧 writer 默认 protocol=0，保留 last_*/run_count，不补造历史；新创建显式 protocol=1。新 tick 遇到到期旧行标 ERROR，旧行不能靠改配置或恢复按钮变成新协议。

历史核对/迁移工具仍待实现；不能直接改 protocol、清零旧数或归档重建来宣称已恢复原调度。旧 Run/key/快照保留，历史只读与可继续发火分开。

新旧 API/Worker 不得混跑；发布前停所有 Run/Schedule writer，不能以 dispatch=false 代替。0036 回退先锁表，有任何 occurrence 或启用新协议的 Schedule 时拒绝降级，不删审计；不证明更早迁移链全部可安全回退。

## 实现与验收

入口：[service](../../PJM/backend/src/projectmind/schedules/service.py)、[repository](../../PJM/backend/src/projectmind/schedules/repository.py)、[API](../../PJM/backend/src/projectmind/api/routes/schedules.py)、[ScheduleDialog](../../PJM/web/src/components/ScheduleDialog.tsx)、[管理页](../../PJM/web/src/pages/SchedulesPage.tsx)。

验收 ONCE/CRON/DST/跨时区同一绝对时刻、当前预览确认、编辑冲突保草稿、超过 100 条/任务失效可管理、A/B/C 间崩溃、同版竞争、暂停与旧回写、原键恢复/自身重叠、接管未知提交、跳过不耗创建额度及历史/混合版本。

时间单测、mock 创建和健康 tick 不证明真实事务/多 Worker/发火成功；专用 Project 另验实际 occurrence、停机迟到与 ERROR 恢复。
