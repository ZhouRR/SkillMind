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

## 生命周期与发火

```text
创建/修改规则 → 计算下次时间 → ACTIVE
                                  ↓ Worker tick
事务 A：CAS 认领并推进 next_run_at
                                  ↓
检查创建者 → 查询原创建键 → 无原 Run 才检查重叠与当前任务
                                  ↓
事务 B：RunService.create_task_run → Run / Segment / Outbox
                                  ↓
事务 C：记录本次触发结果、计数与 Schedule 状态
```

支持 `ONCE` 和 `CRON`，必填 IANA timezone。定义、预览和触发复用同一时间求值逻辑；ONCE 展示一次，CRON 通常展示至少三次，受 end_at/max_runs 限制时可以不足三次，不能虚构不存在的候选。状态为 `ACTIVE / PAUSED / COMPLETED / ERROR / ARCHIVED`，实际转换见 [domain](../../PJM/backend/src/projectmind/schedules/domain.py)。修改定义不意味着自动从 ERROR 恢复，应通过显式恢复操作。

时间按分钟计算。当前 CRON 在夏令时跳过不存在的本地时刻，重复时刻只取第一次；ONCE 使用带时区的具体时刻并规范到分钟。持久身份以 UTC occurrence 为准，不能用浏览器显示文本生成幂等键。规则时区与页面显示时区的差别见[时间边界](#时间输入与展示的边界)。实现与 DST 用例见 [cron](../../PJM/backend/src/projectmind/schedules/cron.py)和[时间测试](../../PJM/backend/tests/schedules/test_cron.py)。

## 保存和执行边界

| 规则 | 行为 |
| --- | --- |
| 版本 | 保存精确 SkillVersion 与 task_key，不自动跟随 latest |
| 输入和资源选择 | 保存 input/sources 选择规则；发火时经普通 Run 创建校验并生成本次资源快照，不把保存 Schedule 当成冻结全部外部内容 |
| 创建者 | 发火前重新检查用户有效性和 Project access |
| 幂等 | 使用 schedule ID 与 occurrence 生成创建 Run 的稳定键；当前入口已先查询原 Run，再判断是否需要新建 |
| 重叠 | 没有原 occurrence 的 Run 时，发现同 Task 非终态 Run 则跳过，包含等待输入/批准；观察到重叠后再查一次原键 |
| 停机错过 | 处理已保存的一个到期 occurrence，其间其余计划时刻只计 missed，不批量补跑；详见下节 |
| 配置失效 | 记录 FAILED_PRECONDITION 并进入 ERROR，修正后显式恢复 |
| 结束条件 | ONCE 消化后停止；CRON 根据下一候选、end_at 和 max_runs 判断结束，计数存在下述限制 |

调度不会绕过审批、预授权、资源再验证、Event/Outbox 或 Result 不可变规则。UI 的暂停操作控制 Schedule 后续触发，不能当作暂停正在运行的 Agent。

document 的显式“全集”在每个新 Run 创建时固定成员；单份/集合继续指向原 ID，不跟随同路径重新上传的内容。同一 occurrence 重放复用首次 Run/快照，见[资源快照](resource-snapshots.md#创建重放与调度)。表单与公开清单已接入，不代表 tick 崩溃后的持久恢复已经完成。

## 保存表单与触发预览

时间预览只解释“何时触发”，不证明输入有效、资源已授权或未来发火一定成功。保存时校验配置，发火时仍经普通 Run 创建再次校验；预览结果不作为冻结清单或资源锁。

当前 TasksPage 先选定任务，再挂载 ScheduleDialog；与即时执行共用 TaskLaunchFields/taskDraft，允许编辑实际输入、确认文档范围并解释失效原因。必需文档未选或集合不完整不能保存；业务输入的最终校验仍由服务端负责。选项语义只在[资源选择规范](resource-snapshots.md#公开选择与读取投影的实施契约)维护。

当前预览是可选操作，只提交时间 definition；改动时间定义后清除旧预览，晚到的旧结果不得覆盖新配置。保存发送精确任务、实际 input/sources 与时间定义，不创建 Run。关闭创建弹窗丢弃未保存草稿；中止本地等待不保证服务器没有保存 Schedule，不能把 Run 创建的幂等恢复保证套到 Schedule 创建。

保存前应有可确认的服务端候选时刻，而不只显示 CRON 表达式。当前保存按钮不要求已有预览，这项确认仍需补齐：候选必须属于当前 definition，修改时间后重新确认，不使用旧预览证明新规则；候选数量遵守结束条件。预览不因此变成资源授权或未来执行承诺。

修改现有配置的 PUT API 和 Web client 已存在，要求 expected_row_version，且不允许换成另一任务；当前 Task Center 只有创建和状态操作，没有编辑入口。后续编辑应复用共享输入组件、载入完整原配置并解释版本冲突，不以“归档后重建”静默替代更新，也不继承另一个 Schedule 的计数。

验收时分别确认“合法时间但输入无效不能保存”“可选文档不选不授权”“时间预览不触发 Run”和“保存后资源失效在发火时明确 ERROR”。完整触发恢复仍需下面的持久在途设计，不能靠完善表单消除事务故障窗口。

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

例如每小时一次的规则，保存的 next_run_at 为 03:00，Worker 到 09:30 才恢复：当前会认领 03:00 并尝试创建一次 Run，把 04:00–09:00 六次记为 missed，下一候选设为 10:00。ONCE 也没有最大迟到拒绝逻辑。因此“停止中不追赶”的约束不能被理解为代码已保证“任何过期时刻都不执行”。

[_count_missed / plan_occurrence](../../PJM/backend/src/projectmind/schedules/service.py)单次最多计 1000 个错过时刻，长期停机后的数值可能是截顶值，不是精确漏发账本。若产品要求过期全部跳过，应先确定 ONCE/CRON 的迟到容忍与截止规则，再同步实现、预览、UI 和验收；不能在运维恢复中临时补跑或临时改写 occurrence。

### 事务、并发与统计

| 代码边界 | 可能观察到的结果 | 不能声称的保证 |
| --- | --- | --- |
| 事务 A 提交后、B 之前崩溃 | next_run_at 已推进，但没有 Run；当前无持久在途记录可恢复 | 认领就一定创建成功 |
| 事务 B 提交后、C 之前崩溃 | Run 已存在，但 last_run_id / run_count / last_outcome 可能没更新 | Schedule 摘要等于完整执行历史 |
| claim 只比较 next_run_at 与 ACTIVE，不比较配置版本 | 定义被改动但候选时间没变时，可能仍使用先前读取的输入 | 触发一定使用最新编辑结果 |
| record_outcome 仅按 schedule ID 更新 | 旧触发的结束/失败可能覆盖较新的状态或下次时间 | 暂停/编辑后绝不被旧回调覆盖 |
| 创建前先查询同 Task 非终态 Run | 能跳过已看见的重叠；并发手动启动或多个 Schedule 仍可相撞 | 同 Task 全局互斥 |
| 已有原键查询与冲突后复查，但没有持久在途 occurrence | 给定同一 ClaimedSchedule 时可走重放；tick 崩溃后未必能再次取得这份精确配置 | 有稳定键和重放函数就已经实现完整恢复 |

这些是根据当前 [service](../../PJM/backend/src/projectmind/schedules/service.py) / [repository](../../PJM/backend/src/projectmind/schedules/repository.py)推导出的故障与竞争风险，不是已完成的并发故障注入结论。幂等键抑制重复创建，不能把三个事务提升为“精确一次投递”。

`run_count` 目前在记录非空 run_id 时递增，不是 Run 成功次数；重叠跳过不递增。然而 `plan_occurrence` 预先按 `run_count + 1` 判断 max_runs，所以最后一次机会若被跳过，也可能提前 COMPLETED。ONCE 本身只有一次触发机会，跳过后结束与 CRON 的计数问题须分开理解。目标应明确为“成功关联的不同 Run 数量”，恢复同一 Run 不重复计数，只有实际创建后才消耗该额度。

## 可靠性修正要求（待实现）

后续实现采用“持久在途触发 + 普通幂等创建 + 幂等结算”的责任划分；不是引入历史批量补发。occurrence 是逻辑对象，具体载体/Schema/migration 在实施时与当前模型一起评审。

普通创建的请求身份与重放入口已经存在，后续复用它；下面尚缺的是可恢复的认领记录、配置版本隔离和幂等结算，不应重新设计第二套请求 hash 或调度专用 Run 插入逻辑。

1. 认领与保存该次 occurrence 的精确配置版本、输入/选择、创建者和稳定键同事务完成。比较配置版本，避免读取旧定义后只凭相同 next_run_at 认领。
2. Worker 仅恢复已经持久认领、尚未结算的触发。每次恢复仍检查当前身份/Project 权限，并先确认原键是否已有 Run；不能把自己当成另一条重叠执行。
3. 未创建 Run 时继续调用同一个 `RunService.create_task_run`，不直接插 Run，不放宽资源、批准或 Outbox 规则。资源冻结与重放必须先满足 [R01 创建设计](run-creation.md)。
4. Run 关联与 occurrence 结算必须幂等；计数只增加一次。max_runs 检查与名额占用需原子处理，不能先假定会创建成功再结束规则。
5. 以认领提交为界：暂停/归档阻止之后的认领，已认领的触发按原配置完成或失败，仍需当前授权；编辑不重写它的输入。关联 Run 的取消走普通取消协议。旧结果只归自己的 occurrence，不覆盖新定义、暂停或归档决定，UI 必须说明暂停不会撤回在途触发。
6. 错过但未认领的历史时刻不由恢复器批量补发。迟到准入策略、missed 截顶提示和在途状态的用户可见面作为同一调度变更交付；它们不是现行公开字段。

仍不自动加入全局 task mutex、条件监控、跨 Project 调度或任意补跑。若业务确需强制同 Task 串行，应让手动与调度创建共同经过数据库级互斥；只锁调度路径无效。

## 实现与验收

[service](../../PJM/backend/src/projectmind/schedules/service.py) · [repository](../../PJM/backend/src/projectmind/schedules/repository.py) · [cron](../../PJM/backend/src/projectmind/schedules/cron.py) · [API](../../PJM/backend/src/projectmind/api/routes/schedules.py) · [Web](../../PJM/web/src/components/ScheduleDialog.tsx)

| 验收 | 必须观察到的结果 |
| --- | --- |
| 时间与预览 | ONCE/CRON、timezone/DST、分钟粒度、end_at/max_runs 截止一致；不足三次真实显示 |
| 时间输入与展示 | 浏览器与规则时区不同、跨日与 DST 时，输入、预览和列表标注同一个绝对时刻；不误贴时区 |
| 编辑与提交边界 | 编辑载入原配置并带 expected_row_version，冲突保留草稿；旧预览不覆盖新定义；Schedule 保存与 Run 创建的未知结果分开处理 |
| 故障窗口 | 分别在三个事务之间中断；原 Run 不重复创建，在途触发有可追溯去向 |
| 配置竞争 | 编辑后候选时间不变、暂停/归档与回写并发时，不使用未确认的新旧混合输入，不覆盖较新决策 |
| 幂等与重叠 | 自身 occurrence 恢复不跳过自己；另一 Run 的真实重叠按声明策略处理 |
| 计数与上限 | 创建、重放、跳过、失败分别核对；只对不同的已关联 Run 计数，不把 Run 终态成功混进来 |
| 失效与恢复 | 创建者/版本/资源失效可解释地 ERROR；修正后显式恢复，不替换资源或自动追赶 |

本地纯时间测试不能覆盖真实事务、多个 Worker 或部署时钟。部署验收还需专用 Project 观测实际 occurrence、停机迟到、重叠和 ERROR 恢复；当前未完成事项继续保留在 R09。

现有[重放回归](../../PJM/backend/tests/schedules/test_schedule_replay.py)与表单浏览器检查的覆盖范围统一见[计划](../planning/roadmap.md#13-当前执行状态)。原键重放和 mock 保存通过不覆盖持久在途、配置竞争、计数、跨时区页面或真实数据库崩溃恢复。

Worker tick 健康只证明调度循环在运行，不证明业务 Run 已成功创建。
