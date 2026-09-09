# TaskSchedule 设计

> 定位：当前调度行为与可靠性修正要求。调度只决定何时、以谁的身份、使用哪份配置调用普通 Run 创建服务；不成为第二个执行引擎。进度见[计划 R09](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)，创建身份见[Run 创建与幂等](run-creation.md)。

## 一页速览

| 要区分的事情 | 边界 |
| --- | --- |
| Schedule 与 Run | Schedule 保存未来的触发规则；每次成功触发创建独立 Run |
| occurrence 与实际开始时间 | occurrence 是规则算出的计划时刻，Worker 可能晚到；实际 Run 开始更晚 |
| 暂停调度与取消执行 | 暂停阻止后续认领；已认领的在途触发不保证被撤销，已有 Run 需单独取消 |
| 不重复创建与不漏发 | 稳定幂等键约束重复；不能补上认领后崩溃造成的丢失 |
| 不批量补跑与绝不迟到执行 | 当前恢复会处理一个已到期 occurrence，其余错过只计数；不具备最大迟到拒绝策略 |

下面“当前”描述的是 2026-09-08 工作副本；修正要求单列，不把尚不存在的 occurrence 持久记录或事务保证写成现行能力。

## 一个例子：规则、触发与执行分别看

假设同一 Schedule 首次在 08:00 发火，之后每小时一次，未设截止或次数上限，各次回写都成功；以下时刻均为 UTC：

```text
08:00 的触发 → 创建 Run A
                  ↓
Run A 等待用户批准，尚未结束
                  ↓
09:00 的触发 → 发现 Run A，跳过
                  ↓
下次候选 10:00；Run A 仍在等待
```

此时 Schedule 的几个字段不是“一次 Run 的完整状态”：

| 读到的值 | 它实际说明什么 |
| --- | --- |
| status = ACTIVE | 规则仍接受后续认领；不表示 Run A 正在消耗模型时间 |
| last_run_at = 09:00 | 最近回写的 **occurrence 计划时刻**，不是 Run A 的创建、开始或结束时刻 |
| last_outcome = SKIPPED_OVERLAP | 09:00 这次处理跳过；不是 Run A 的执行结果 |
| last_run_id = Run A | 最近一次成功关联的 Run；跳过/失败回写不会清空它，因此可能属于更早的 occurrence |
| run_count = 1 | 已关联 Run 的计数摘要，不是成功数；当前重复回写或漏回写还会影响准确性 |

例如“09:00 跳过”与“仍能打开 Run A”可以同时成立。`last_*` 是可被覆盖的摘要，不是持久触发历史；回写乱序或缺失时还可能滞后。后续恢复要看[认领记录](#认领记录与恢复权限)，不能从这几个字段拼出不存在的执行账本。另一条 Run 是否会阻止发火，先核对[重叠范围](#重叠检查到底看谁)。

## 生命周期与发火

```text
保存规则、计算下次时间
  ↓ 仅 ACTIVE 参与 Worker tick
事务 A：认领并推进下次时间
  ↓ 提交（此处尚无 Run）
检查身份、原键与上次 Run
  ↓ 无原 Run 才按条件新建
事务 B：普通 Run 创建服务
  ↓ 提交 Run / Segment / Outbox
事务 C：回写触发摘要与计数
```

支持 `ONCE` 和 `CRON`，必填 IANA timezone。定义、预览和触发复用同一时间求值逻辑；ONCE 展示一次，CRON 通常展示至少三次，受 end_at/max_runs 限制时可以不足三次，不能虚构不存在的候选。状态为 `ACTIVE / PAUSED / COMPLETED / ERROR / ARCHIVED`，实际转换见 [domain](../../PJM/backend/src/projectmind/schedules/domain.py)。修改定义不意味着自动从 ERROR 恢复，应通过显式恢复操作。

图中三个事务属于当前实现，不是一个跨步骤原子提交。首次创建进入 ACTIVE；修改保留原状态；显式恢复会从当前时间重算候选，不重放暂停期间的历史时刻。Schedule 的 COMPLETED 表示规则不再发火，可能仍有关联 Run 未结束。

已有原 Run、重叠或前置不符时，不执行新建事务 B，仍回写本次结果；数据库等基础设施异常则可能在 C 之前中断。流程图不能作为每次认领必定留下完整结果的保证。

时间按分钟计算。当前 CRON 在夏令时跳过不存在的本地时刻，重复时刻只取第一次；ONCE 使用带时区的具体时刻并规范到分钟。持久身份以 UTC occurrence 为准，不能用浏览器显示文本生成幂等键。规则时区与页面显示时区的差别见[时间边界](#时间输入与展示的边界)。实现与 DST 用例见 [cron](../../PJM/backend/src/projectmind/schedules/cron.py)和[时间测试](../../PJM/backend/tests/schedules/test_cron.py)。

## 保存和执行边界

| 规则 | 行为 |
| --- | --- |
| 版本 | 保存精确 SkillVersion 与 task_key，不自动跟随 latest |
| 输入和资源选择 | 保存 input/sources 选择规则；发火时经普通 Run 创建校验并生成本次资源快照，不把保存 Schedule 当成冻结全部外部内容 |
| 创建者 | 发火前重新检查用户有效性和 Project access |
| 幂等 | 使用 schedule ID 与 occurrence 生成创建 Run 的稳定键；当前入口已先查询原 Run，再判断是否需要新建 |
| 重叠 | 没有原 occurrence 的 Run 时，只检查本 Schedule 的 last_run_id；该 Run 非终态则跳过，包含等待输入/批准，详见[检查范围](#重叠检查到底看谁) |
| 停机错过 | 处理已保存的一个到期 occurrence，其间其余计划时刻只计 missed，不批量补跑；详见[停机恢复](#停机恢复的实际行为) |
| 配置失效 | 记录 FAILED_PRECONDITION 并进入 ERROR，修正后显式恢复 |
| 结束条件 | ONCE 消化后停止；CRON 根据下一候选、end_at 和 max_runs 判断结束，计数存在下述限制 |

调度不会绕过审批、预授权、资源再验证、Event/Outbox 或 Result 不可变规则。UI 的暂停操作控制 Schedule 后续触发，不能当作暂停正在运行的 Agent。

document 的显式“全集”在每个新 Run 创建时固定成员；单份/集合继续指向原 ID，不跟随同路径重新上传的内容。同一 occurrence 重放复用首次 Run/快照，见[资源快照](resource-snapshots.md#创建重放与调度)。表单与公开清单已接入，不代表 tick 崩溃后的持久恢复已经完成。

### 重叠检查到底看谁

当前 [ScheduleService._overlapping_run](../../PJM/backend/src/projectmind/schedules/service.py) 先读取该 Schedule，再按其 last_run_id 查一条 Run。它不按 task_id 查询全部执行：

- 本 Schedule 上次已关联 Run 仍在执行或等待：可观察并跳过；自己的原键重放先于这项检查。
- 另一个 Schedule 或手动启动了同 Task：当前检查不会因此跳过，即使对方已经提交，并非只有并发瞬间才有窗口。
- 本 Schedule 的 Run 已创建但结果回写丢失：last_run_id 可能仍为空或指向更早的 Run，下一次触发也可能漏判。

本设计保持“同 Schedule 不重叠”为修正范围，以可追溯关联代替摘要指针，接线见[认领记录与执行权](#认领记录与恢复权限)；不悄悄扩大为 Project/Task 全局互斥，也不引入排队。全局串行若另行立项，必须覆盖手动与所有 Schedule 的普通创建入口，不能只给调度加锁。

## 保存表单与触发预览

时间预览只解释“何时触发”，不证明输入有效、资源已授权或未来发火一定成功。保存时校验配置，发火时仍经普通 Run 创建再次校验；预览结果不作为冻结清单或资源锁。

当前 TasksPage 先选定任务，再挂载 ScheduleDialog；与即时执行共用 TaskLaunchFields/taskDraft，允许编辑实际输入、确认文档范围并解释失效原因。必需文档未选或集合不完整不能保存；业务输入的最终校验仍由服务端负责。选项语义只在[资源选择规范](resource-snapshots.md#公开选择与读取投影的实施契约)维护。

当前预览是可选操作，只提交时间 definition；改动时间定义后清除旧预览，晚到的旧结果不得覆盖新配置。保存发送精确任务、实际 input/sources 与时间定义，不创建 Run。关闭创建弹窗丢弃未保存草稿；中止本地等待不保证服务器没有保存 Schedule，不能把 Run 创建的幂等恢复保证套到 Schedule 创建。

保存前应有可确认的服务端候选时刻，而不只显示 CRON 表达式。当前保存按钮不要求已有预览，这项确认仍需补齐：候选必须属于当前 definition，修改时间后重新确认，不使用旧预览证明新规则；候选数量遵守结束条件。预览不因此变成资源授权或未来执行承诺。

验收时分别确认“合法时间但输入无效不能保存”“可选文档不选不授权”“时间预览不触发 Run”和“保存后资源失效在发火时明确 ERROR”。完整触发恢复仍需下面的持久在途设计，不能靠完善表单消除事务故障窗口。

### 保存后的管理入口

页面看不到规则，不能直接推断保存失败或引导用户重新创建。当前边界是：

- 编辑：PUT API/client 已有，要求 expected_row_version、不允许更换任务；Task Center 只有创建和状态操作，没有编辑表单。
- 并发：repository 先读版本再在 Python 比较，尚不能保证原子冲突检测，见[配置并发要求](#配置并发与暂停)。
- 可见性：[loadProjectSchedules](../../PJM/web/src/api/schedules.ts) 只取 Project 前 100 条、丢弃分页信息；[TasksPage.buildRows](../../PJM/web/src/pages/TasksPage.tsx) 再挂到当前 TaskCatalog 卡片，并隐藏 ARCHIVED。超过首批或任务已失效的规则，可能连需要处理的 ERROR 也没有入口。

后续交付分别满足三项：

1. 编辑复用共享输入组件，载入完整原配置，冲突时保留草稿；不以归档重建代替更新，不继承另一规则的计数。
2. 管理入口以 Project 为主体，不依赖任务仍可启动；服务端筛选/分页，页面保留 total 和分页状态，不靠增大 100 的常量解决。
3. 失效精确版本只读展示身份与原因，不切换 latest；归档可明确筛选。暂停/恢复/归档仍走授权 API，不改数据库补记录。

公开分页和目标字段同步见[契约入口](../../PJM/contracts/README.md#schedule-の公開契約を読む)。

### 时间输入与展示的边界

下面来自 [ScheduleDialog](../../PJM/web/src/components/ScheduleDialog.tsx) 和[时间格式化](../../PJM/web/src/lib/presentation.ts)的代码核对，不是浏览器跨时区验收结论：

| 位置 | 当前依据 | 容易误读的地方 |
| --- | --- | --- |
| CRON 求值 | 服务端使用 definition.timezone | 规则的 09:00 未必是使用者当地的 09:00 |
| ONCE 的 run_at、截止 end_at 输入 | datetime-local 经浏览器本地时区转换成带 offset 的绝对时刻 | 改变 timezone 输入框不会改变该转换的时区 |
| 预览列表 | 服务端返回时刻，Web 按浏览器本地时间显示，文案已说明“本地时间” | 尚未并列显示规则时区的对应时刻 |
| ONCE 列表摘要 | 本地格式化的时刻后拼接 Schedule.timezone | 两个时区不同时，标签与时刻可能不匹配 |
| API 时间输入 | 当前 request model 使用普通 datetime，未限定必须带时区；service 调用 astimezone | 无 offset 的值可能依赖 API 进程时区解释，不能认作跨环境稳定的时刻 |

修正时保留现有 API 的绝对时刻语义，不重新解释已保存数据。输入栏应显式标出实际使用的浏览器 IANA 时区；规则 timezone 用于 CRON 求值。API 对 run_at/end_at 须明确拒绝无 offset 的输入，不能借服务器默认时区补全。预览和列表应以 Schedule.timezone 格式化实际 occurrence，同时显示时区/offset，必要时另列本地对照。不能给本地时刻直接贴上另一个时区标签。

例如浏览器为 UTC、规则为 Asia/Tokyo：CRON 09:00 对应 UTC 00:00；而当前 ONCE 输入 09:00 得到的是 UTC 09:00，即东京 18:00。这不是同一次执行时刻。需用不同浏览器/规则时区、跨日以及 DST 缺失/重复时刻验收；本地时间输入遇到不存在或无法唯一表达的时刻应明确拒绝或让用户确认具体 offset，不能依赖 Date 的隐式纠正。

以上是待补齐的显示/输入验证要求。若未来改为“按所选规则时区输入 ONCE”，须另行确定歧义解析协议和兼容方式，不只改 label。

## 可靠性保证的限度

### 停机恢复的实际行为

这里的停机是调度执行者实际停止，不是设置 `PROJECTMIND_WORKER_DISPATCH_ENABLED=false`。当前 Schedule tick 不检查该开关，仍可能创建 Run 和更新配置摘要；停写/恢复须按[Worker 入口边界](../operations/deployment.md#一个例子关闭-dispatch-后仍有工作)确认所有执行者。

例如每小时一次的规则，保存的 next_run_at 为 03:00，Worker 到 09:30 才恢复：当前会认领 03:00 并尝试创建一次 Run，把 04:00–09:00 六次记为 missed，下一候选设为 10:00。ONCE 也没有最大迟到拒绝逻辑。因此“停止中不追赶”的约束不能被理解为代码已保证“任何过期时刻都不执行”。

[_count_missed / plan_occurrence](../../PJM/backend/src/projectmind/schedules/service.py)单次最多计 1000 个错过时刻，长期停机后的数值可能是截顶值，不是精确漏发账本。若产品要求过期全部跳过，应先确定 ONCE/CRON 的迟到容忍与截止规则，再同步实现、预览、UI 和验收；不能在运维恢复中临时补跑或临时改写 occurrence。

### 事务、并发与统计

| 代码边界 | 风险与保证的限度 |
| --- | --- |
| A 提交后、B 之前崩溃 | next_run_at 已推进，却没有 Run 或持久在途记录；认领不等于创建成功 |
| B 提交后、C 之前崩溃 | Run 已存在，Schedule 摘要可能没更新；不能仅依赖 last_run_id 判断没有执行 |
| claim 只比较 next_run_at 与 ACTIVE | 候选时间不变的编辑可能仍被旧输入认领；没有比较配置版本 |
| 编辑先读版本、再更新 ORM 对象 | 两个请求可能同时读到同版并通过检查；没有带 expected_row_version 的条件 UPDATE 或行锁保护，存在覆盖风险 |
| 状态先读后判定、结果只按 ID 回写 | 暂停/归档与恢复/旧触发回写可能互相覆盖；状态 request 也没有 expected_row_version |
| 重叠依赖本 Schedule 的 last_run_id | 不检查手动/其他 Schedule；自身已创建但漏回写时也可能漏判，详见[实际范围](#重叠检查到底看谁) |
| 重放函数依赖内存 ClaimedSchedule | 同一参数可查原 Run，但 tick 崩溃后未必能恢复原配置；稳定键不等于完整恢复 |

这些是根据当前 [service](../../PJM/backend/src/projectmind/schedules/service.py) / [repository](../../PJM/backend/src/projectmind/schedules/repository.py)推导出的故障与竞争风险，不是已完成的并发故障注入结论。幂等键抑制重复创建，不能把三个事务提升为“精确一次投递”。

row_version 在编辑、状态变更、认领和回写时都会增长，是整行变化的标记，不是“第几版执行配置”。当前 API 回归中的 409 来自 fake service 抛出的冲突，只证明错误投影；不能用它代替两个真实 transaction 的同版竞争验收。

`run_count` 目前在记录非空 run_id 时递增，不是 Run 成功次数；重叠跳过不递增。然而 `plan_occurrence` 预先按 `run_count + 1` 判断 max_runs，所以最后一次机会若被跳过，也可能提前 COMPLETED。ONCE 本身只有一次触发机会，跳过后结束与 CRON 的计数问题须分开理解。目标应明确为“成功关联的不同 Run 数量”，恢复同一 Run 不重复计数，只有实际创建后才消耗该额度。

## 可靠性修正要求（待实现）

后续实现采用“持久在途触发 + 普通幂等创建 + 幂等结算”的责任划分；不是引入历史批量补发。occurrence 是逻辑对象，具体载体/Schema/migration 在实施时与当前模型一起评审。

普通创建的请求身份与重放入口已经存在，后续复用它；下面尚缺的是可恢复的认领记录、配置版本隔离和幂等结算，不应重新设计第二套请求 hash 或调度专用 Run 插入逻辑。

### 认领记录与恢复权限

认领事务同时确认当前状态、候选和配置版本，保存原创建者、精确任务、时间规则、input/sources 选择、UTC occurrence 与原幂等键，再推进候选。逻辑唯一身份仍是 Schedule ID 与 UTC occurrence；配置版本用于解释和隔离输入，不因接管或编辑而换键重建同次 Run。

持久认领还要有有界执行权和接管规则：同一 occurrence 只允许当前持有者提交关联或结算，超期旧持有者的晚到结果不能覆盖新事实。它不是 RunAttempt，不能借用尚未创建的 Run lease。数据库锁只覆盖认领/校验/结算，不跨资源校验或普通创建调用长期持有。

恢复只扫描已认领、未结算的记录，顺序固定：

1. 检查当前身份与 Project 权限，无权不继续创建或重放。
2. 查询原请求是否已有 Run；存在则关联原 Run，不重新冻结资源，不把自己判成重叠。
3. 无原 Run 才检查本 Schedule 其他在途创建和已关联非终态 Run；不能只看 last_run_id。
4. 仍允许新建时，经同一 `RunService.create_task_run` 创建，不直接插 Run。

创建事务必须校验这份认领仍允许新建，不能只在调用前读一次执行权。资源、批准和 Outbox 规则仍按[创建设计](run-creation.md)，不另开调度专用通道。

### 配置并发与暂停

编辑的版本比较、可编辑状态检查和写入必须在同一个数据库原子边界完成；状态转移也要按锁内最新状态判定，不能从旧读取结果授权。可使用条件 UPDATE 或统一行锁协议，但必须验证影响行数/冲突结果。读取后在 Python 比较、只给 request 加字段、或仅维护一个递增整数都不足以实现 CAS。

区分“整行并发标记”和“执行配置版本”：tick/统计变化不应被解释为输入变更。认领固定语义配置，编辑只影响尚未认领的触发。若调整公开 expected_row_version 或状态请求，须同步 DTO/OpenAPI、Web client、冲突提示和旧客户端兼容；不能在目标示例里先加尚未定义的字段。

以认领提交为暂停/归档边界：它们阻止之后的认领，不撤回已认领工作；旧 occurrence 按原配置完成或失败，仍需当前授权。结果属于原 occurrence，不能让旧回写把新定义、PAUSED 或 ARCHIVED 覆盖成旧状态。Run 的取消继续走普通取消协议，UI 明示两种操作的区别。

### 结算、计数与未知结果

Run 关联与结算按 occurrence 幂等：成功关联的不同 Run 只累计一次，不等待 Run SUCCEEDED，也不因同一次恢复再次加一。旧结果可以补齐自己的审计与应计数量，但不得把较新 occurrence 的摘要退回旧时间。

max_runs 是该 Schedule 的累计创建额度，不是触发机会或业务成功额度。认领时原子保留名额；确认无 Run 的跳过/失败可释放，已有 Run 则只结算一次。创建提交结果不明时先保留名额、用原请求查询，不能当作零消耗再发一次。编辑不清零历史计数，也不能把上限改到已关联数量与仍占用名额的总和之下。ONCE 的唯一机会消化后结束，与 CRON 的额度耗尽分别判断。

数据库连接中断等基础设施错误不等于 FAILED_PRECONDITION。结果不明必须留下可恢复记录；到达有界重试上限后停止自动重试并保留待核对事实，不换键、不直接改计数。错过但未认领的历史时刻不由恢复器批量补发；迟到准入、missed 截顶提示与在途可见面随调度修正一起交付，它们不是现行公开字段。

### 历史兼容与实施顺序

先同步领域/持久记录、普通 Run 创建接线、原子编辑/认领/结算和故障测试，再接入 API 投影、分页管理、编辑表单与三语显示。数据库迁移存在不等于恢复器已经可启用。

已有 Schedule 仅保有当前配置和摘要，不能从 last_* 或当前 input 伪造全部历史 occurrence、旧配置或精确累计数。迁移保留原 Run、键与快照；可验证的关联和无法证明的历史分别标识，不为补账自动创建 Run。旧 tick 不写新在途记录，不能与要求新协议的 Worker 混跑；上线按[停写与恢复审查](../operations/backup-recovery.md#恢复前的停止条件)隔离旧执行者，另行验证混合版本拒绝和回退的数据保留。

仍不自动加入全局 task mutex、条件监控、跨 Project 调度或任意补跑。

## 实现与验收

[service](../../PJM/backend/src/projectmind/schedules/service.py) · [repository](../../PJM/backend/src/projectmind/schedules/repository.py) · [cron](../../PJM/backend/src/projectmind/schedules/cron.py) · [API](../../PJM/backend/src/projectmind/api/routes/schedules.py) · [Web](../../PJM/web/src/components/ScheduleDialog.tsx)

| 验收 | 必须观察到的结果 |
| --- | --- |
| 时间与预览 | ONCE/CRON、timezone/DST、分钟粒度、end_at/max_runs 截止一致；不足三次真实显示 |
| 时间输入与展示 | 浏览器与规则时区不同、跨日与 DST 时，输入、预览和列表标注同一个绝对时刻；不误贴时区 |
| 编辑与提交边界 | 编辑载入原配置并带 expected_row_version，冲突保留草稿；旧预览不覆盖新定义；Schedule 保存与 Run 创建的未知结果分开处理 |
| 管理可见性 | Project 超过 100 条、精确任务失效、归档与分页切换时仍能定位相应规则；无卡片不能被解释为未保存 |
| 故障窗口 | 分别在三个事务之间中断；原 Run 不重复创建，在途触发有可追溯去向 |
| 配置竞争 | 两个同版编辑最多一个成功；候选时间不变的编辑与认领、暂停/归档与回写并发时，不混用输入或覆盖较新决策 |
| 幂等与重叠 | 自身恢复不跳过自己；同 Schedule 漏回写不漏判；手动/其他 Schedule 不被误说成已纳入互斥 |
| 接管与未知提交 | 旧持有者不能晚到结算；Run 提交响应丢失时复用原键确认，不重复创建或提前释放名额 |
| 计数与上限 | 创建、重放、跳过、失败分别核对；只对不同的已关联 Run 计数，不把 Run 终态成功混进来 |
| 失效与恢复 | 创建者/版本/资源失效可解释地 ERROR；修正后显式恢复，不替换资源或自动追赶 |
| 历史与上线 | 旧摘要不被伪造为完整账本；旧 Worker 不绕过新认领协议；回退保留已创建 Run 与触发审计 |

本地纯时间测试不能覆盖真实事务、多个 Worker 或部署时钟。部署验收还需专用 Project 观测实际 occurrence、停机迟到、重叠和 ERROR 恢复；当前未完成事项继续保留在 R09。

现有[重放回归](../../PJM/backend/tests/schedules/test_schedule_replay.py)与表单浏览器检查的覆盖范围统一见[计划](../planning/roadmap.md#13-当前执行状态)。原键重放和 mock 保存通过不覆盖持久在途、配置竞争、计数、跨时区页面或真实数据库崩溃恢复。

Worker tick 健康只证明调度循环在运行，不证明业务 Run 已成功创建。
