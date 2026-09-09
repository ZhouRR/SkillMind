# TaskSchedule 设计

Schedule 只决定何时、以谁和哪份配置调用普通 [Run 创建](run-creation.md)，不成为第二引擎。下文分开现行行为与待实现可靠性，状态见[计划 R09](../planning/roadmap.md#r09-调度)。

## 一页速览

| 概念 | 边界 |
| --- | --- |
| Schedule / Run | 前者存规则，每次成功触发创建独立 Run |
| occurrence / 实际开始 | occurrence 是计划时刻，tick 与 Run 都可能晚到 |
| 暂停 / 取消 | 暂停阻止后续认领，不撤回在途；已有 Run 单独取消 |
| 幂等 / 不漏发 | 稳定键抑制重复，不能修复认领后崩溃丢失 |
| 不批量补跑 / 不迟到 | 当前处理一个到期 occurrence，其余计 missed，无最大迟到拒绝 |

## 一个例子：规则、触发与执行分别看

08:00 创建 Run A，A 等批准；09:00 同 Schedule 触发因重叠跳过。此时 status=ACTIVE、last_run_at=09:00、last_outcome=SKIPPED_OVERLAP、last_run_id=A、run_count=1 可以同时成立。

last_run_at 是最近回写的计划时刻，不是 A 开始时间；last_run_id 保留最近成功关联，跳过不清空；run_count 不是成功数。last_* 是会被覆盖/滞后的摘要，不是完整触发账本。

## 生命周期与发火

```text
保存规则 / 候选，仅 ACTIVE 参与 tick
  → TX A：认领并推进 next_run_at（还没有 Run）
  → 当前授权 / 查询原键 / 重叠与前置检查
  → TX B：普通创建 Run / Segment / Outbox
  → TX C：回写触发摘要 / 计数
```

三个事务不原子，已有原 Run/重叠/前置不符时不执行 B，基础设施失败可能中断于 C 前。

支持 ONCE/CRON、必填 IANA timezone、分钟粒度；定义/预览/发火共用求值。ONCE 一次、CRON 通常展示至少三次，end_at/max_runs 限制不足时不虚构。状态 ACTIVE/PAUSED/COMPLETED/ERROR/ARCHIVED；修改保留状态，ERROR 修正后显式恢复，从当前时刻重算，不追赶暂停历史。COMPLETED 可仍有关联非终态 Run。

CRON 跳过 DST 不存在时刻、重复时刻取第一次；ONCE 规范到分钟，持久身份使用 UTC occurrence，不用浏览器文字生成键。代码见 [cron](../../PJM/backend/src/projectmind/schedules/cron.py)。

## 保存和执行边界

| 项目 | 规则 |
| --- | --- |
| 任务/输入 | 保存精确 SkillVersion/task_key、input/sources 规则，不跟 latest；发火才冻结本次资源 |
| 身份 | 发火前重新检查创建者有效性/Project 权限 |
| 幂等 | Schedule ID + UTC occurrence；先查原 Run，再判断新建 |
| 重叠 | 当前仅查该 Schedule 的 last_run_id，非终态含等待均跳过 |
| 失效 | FAILED_PRECONDITION / ERROR，修正后显式恢复，不换来源 |
| 结束 | ONCE 消化后停止；CRON 按候选/end_at/max_runs，计数缺口见下文 |

调度不绕过批准、资源再验证或审计。文档全集每个新 Run 固定当时成员；单份/集合保持原 ID，同 occurrence 重放原快照。

### 重叠检查到底看谁

当前 [ScheduleService](../../PJM/backend/src/projectmind/schedules/service.py)只看本 Schedule 上次关联 Run，不查同 Task 的手动/其他 Schedule；自身创建后漏回写也可能漏判。原 occurrence 重放先于重叠检查。

修正范围仍是同 Schedule 不重叠，使用可追溯关联而非摘要指针；不悄悄扩成 Project/Task 全局锁或排队。全局串行若另立项须覆盖全部普通创建入口。

## 保存表单与触发预览

TasksPage/ScheduleDialog 与即时执行共用 TaskLaunchFields/taskDraft，发送实际 input/sources/精确任务，必需文档未选或集合不完整不能保存。预览只验证时间，不授权资源、不创建 Run，也不保证未来发火成功。

当前预览可选，只发送 definition，改时间后清旧预览并拒绝晚到旧结果；保存尚未要求确认当前 definition 的服务端候选，需补齐。关闭创建弹窗丢草稿，abort 不证明 Schedule 未保存；它没有 Run 创建的原键恢复保证。

### 保存后的管理入口

当前 PUT/client 已有 expected_row_version、不允许换任务，但页面无编辑表单；版本比较非原子。Web 只取 Project 前 100 条且丢分页，再依赖 TaskCatalog 卡片、隐藏 ARCHIVED，失效任务和后续页可能无法管理。

目标：编辑载入原配置/共享输入，冲突保留草稿，不归档重建；Project 级独立分页/筛选保留 total，失效版本只读标原因、不换 latest；归档可显式查询。页面不可见不代表保存失败。

### 时间输入与展示的边界

| 边界 | 当前问题 / 修正 |
| --- | --- |
| CRON | 按 definition.timezone 求值，未必是浏览器本地时间 |
| ONCE/end_at 输入 | datetime-local 按浏览器时区转绝对时刻；改规则 timezone 不改变转换，须显式标输入时区 |
| 预览/列表 | 预览显示本地时间；ONCE 摘要会拼另一规则时区，可能误标。目标按规则时区显示 occurrence/offset，必要时另列本地对照 |
| API datetime | 当前可接受无 offset，可能依赖进程时区；目标拒绝无 offset，不重新解释旧已存绝对时刻 |

浏览器 UTC、规则 Asia/Tokyo 时，CRON 09:00 是 UTC 00:00，而当前 ONCE 输入 09:00 是东京 18:00。跨日、DST 缺失/歧义须明确拒绝或确认 offset，不能依赖 Date 隐式纠正。改为规则时区输入需另定兼容协议。

## 可靠性保证的限度

### 停机恢复的实际行为

dispatch 开关不停止 Schedule tick，停写须覆盖实际 job/cron/实例，见[部署](../operations/deployment.md)。每小时规则 next_run_at=03:00、09:30 恢复：当前尝试 03:00 一次，04:00–09:00 六次计 missed，下次 10:00；ONCE 也无最大迟到拒绝。

_count_missed 单次最多 1000，长期停机可能截顶。迟到容忍/截止须明确 ONCE/CRON 产品策略再同步实现和 UI，不能临时补跑或改 occurrence。

### 事务、并发与统计

| 边界 | 当前风险 |
| --- | --- |
| A 后 B 前崩溃 | 候选已推进，无 Run/持久在途 |
| B 后 C 前崩溃 | Run 已在，摘要/last_run_id 未更新 |
| claim 只比候选/ACTIVE | 同时间编辑可被旧输入认领，未比配置版本 |
| 编辑先读后 Python 比版本 | 两请求可同版通过，未条件 UPDATE/锁内 CAS |
| 状态与结果回写 | 先读后判、按 ID 回写，旧结果可能覆盖暂停/归档/新定义 |
| 内存 ClaimedSchedule | 重放入口存在，崩溃后仍可能失去原配置 |

row_version 随编辑、状态、claim、回写都增长，不是语义配置版本。run_count 在非空 run_id 回写时增加，不是 Run 成功数；重复/漏回写会失真。plan_occurrence 用 run_count+1 预判 max_runs，最后机会若跳过也可提前 COMPLETED；ONCE 的唯一机会与 CRON 额度另判。

## 可靠性修正要求（待实现）

采用持久在途触发 → 普通幂等创建 → 幂等结算，不设计第二套 Run 插入/hash，也不批量补发未认领历史。

### 认领记录与恢复权限

认领事务原子校验状态、候选、配置版本，保存原 actor/任务/时间规则/input/sources/UTC occurrence/key，再推进候选。逻辑唯一身份仍 Schedule+UTC occurrence，编辑/接管不换键。

持久 claim 有有界执行权/fencing，非 RunAttempt；旧持有者不能晚到关联/结算。锁只覆盖认领/校验/结算，不长期跨资源和普通创建调用。

恢复顺序：当前授权 → 查原键，有 Run 即关联 → 无 Run 才查本 Schedule 其他在途和非终态关联 → 允许时经普通创建。创建事务也须验证认领仍可新建，不能仅在调用前检查。

### 配置并发与暂停

编辑的版本、状态与写入须同原子边界，条件 UPDATE/锁协议核验影响行数；状态按锁内最新事实判定。区分整行 row_version 与语义配置版本，tick 统计不等于改输入。

认领提交是暂停/归档边界：阻止未来认领，不撤回在途；旧 occurrence 按原配置及当前授权完成，不覆盖新定义/PAUSED/ARCHIVED。改公开版本请求时同步 DTO/OpenAPI/Web 冲突与旧客户端兼容。

### 结算、计数与未知结果

同 occurrence 关联/结算幂等，每个不同已关联 Run 只计一次、不等 SUCCEEDED；晚结果可补自身审计/数量，不回退较新摘要。

max_runs 是累计创建额度：认领原子留名额；确认无 Run 的跳过/失败释放，已有 Run 只结一次，创建未知保留并查原键。编辑不清计数，上限不得低于已关联与占用之和。ONCE 一次机会与 CRON 额度耗尽分开。

基础设施错误不是 FAILED_PRECONDITION；有界重试耗尽仍保留待核对记录，不换键/改计数。迟到准入、missed 截顶与在途投影随协议交付，不先造公开字段。

### 历史兼容与实施顺序

先持久协议/普通创建接线/原子编辑认领结算/故障测试，再 API 投影、独立分页编辑与三语。旧 last_* 不能补造历史 occurrence、配置或精确计数；保留原 Run/key/快照，可证明关联与未知分开。

新旧 tick 不混跑绕过认领门禁，回退保留 Run 与触发审计。仍不加入全局 task mutex、跨 Project 调度、条件监控或任意补跑。

## 实现与验收

入口：[service](../../PJM/backend/src/projectmind/schedules/service.py)、[repository](../../PJM/backend/src/projectmind/schedules/repository.py)、[API](../../PJM/backend/src/projectmind/api/routes/schedules.py)、[ScheduleDialog](../../PJM/web/src/components/ScheduleDialog.tsx)。

验收 ONCE/CRON/DST/跨时区同一绝对时刻、当前预览确认、编辑冲突保草稿、超过 100 条/任务失效可管理、A/B/C 间崩溃、同版竞争、暂停与旧回写、原键恢复/自身重叠、接管未知提交、跳过不耗创建额度及历史/混合版本。

时间单测、mock 创建和健康 tick 不证明真实事务/多 Worker/发火成功；专用 Project 另验实际 occurrence、停机迟到与 ERROR 恢复。
