# TaskSchedule 设计

Schedule 决定何时、以谁、哪份配置调用普通 [Run 创建](run-creation.md)，不是第二引擎。实现与缺口集中在[计划 R09](../planning/roadmap.md#r09-调度)。

## 一页速览

| 概念 | 边界 |
| --- | --- |
| Schedule / Run | 前者存规则，每次成功触发创建独立 Run |
| occurrence / 实际开始 | occurrence 是计划时刻，tick 与 Run 都可能晚到 |
| 暂停 / 取消 | 暂停阻止后续认领，不撤回在途；已有 Run 单独取消 |
| 幂等 / 恢复 | 原键抑制重复，持久 occurrence 保存原配置；有界恢复不承诺无限重试 |
| 不批量补跑 / 不迟到 | 当前处理一个到期 occurrence，其余计 missed，无最大迟到拒绝 |

## 一个例子：规则、触发与执行分别看

08:00 创建 A，A 等批准；09:00 同调度因重叠跳过。ACTIVE、last_run_at=09:00、last_outcome=SKIPPED_OVERLAP、last_run_id=A、run_count=1 可同时成立。

last_run_at 是回写的计划时刻，last_run_id 是最近成功关联，跳过不清空；run_count 计创建不计成功。last_* 可滞后/覆盖，不是触发账本。

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

认领/创建分事务，A 后崩溃恢复原 PENDING；初始快照、事件/Outbox、关联同事务，commit 确认前不宣称成功。基础设施/提交未知保留记录，以原键确认，不造失败或退款。

ONCE/CRON 必填 IANA timezone、分钟粒度，共用[求值器](../../SKM/backend/src/skillmind/schedules/cron.py)。ONCE 一次；CRON 预览通常至少三次，受 end_at/max_runs 限制时不虚构。CRON 跳过 DST 缺失时刻、重复取首次；ONCE 规范到分钟，键用 UTC occurrence。

状态为 ACTIVE/PAUSED/COMPLETED/ERROR/ARCHIVED；编辑保留状态，ERROR 修正后显式恢复，从当前时刻重算，不补暂停历史。COMPLETED 可有关联非终态 Run。

## 保存和执行边界

| 项目 | 规则 |
| --- | --- |
| 任务/输入 | 保存精确 SkillVersion/task_key、input/sources 规则，不跟 latest；发火才冻结本次资源 |
| 身份 | 发火前重新检查创建者有效性/Project 权限 |
| 幂等 | Schedule ID + UTC occurrence；先查原 Run，再判断新建 |
| 重叠 | 查同 Schedule 的全部 occurrence 关联 Run，非终态含等待均跳过 |
| 失效 | FAILED_PRECONDITION / ERROR，修正后显式恢复，不换来源 |
| 结束 | ONCE 一次机会；CRON 的 max_runs 是累计创建额度，跳过不消耗额度 |

调度不绕批准/资源/审计；新 occurrence 冻结全集当时成员，单份/集合保持原 ID，重放不改快照。

### 管理写入的授权事务

创建/编辑/状态写入经 ProjectWriteActor，并在事务复用[原会话校验](authentication.md#认证与业务提交不是同一个事务)。内部 UserAccess 保留原 cookie/CSRF/request UUID，不换会话、不改 HTTP。锁外解析 Skill/输入，写前重新授权。

```text
Organization UPDATE → 当前 User SHARE → 原 AuthSession UPDATE
  → Project SHARE → USER 的 ProjectMember SHARE
  → 既有 Schedule UPDATE → 写入 / flush → 最终认证 → commit
```

创建不取既有 Schedule 锁；ADMIN 免 membership、不免组织/归档。取得业务锁及最终 flush 后用新时间验原 token/CSRF、角色、撤销、期限与当前成员：会话失效 401、CSRF 403、无权/不存在 404，已授权归档 409。

User SHARE 阻止停用/改角色并兼容 occurrence 外键锁，勿升级 UPDATE 造成与 Schedule 反向等待。CAS、状态、已用/占用额度在 Schedule 锁内检查；最终拒绝整体回滚，commit 未知不自动重发。此门禁不证明物理 commit 瞬间未过期或在途 Run 停止；发火用当前创建者/持久 claim，不依赖浏览器会话。

创建/编辑/恢复 ACTIVE 在 CAS/状态检查后，[可用性门禁](skill-interpretation.md#resourcebinding-与-readiness)依次持有 SkillVersion/ProjectSkillVersion SHARE，验精确 PUBLISHED/未停用；不跟 latest，暂停/归档免可用性。SHARE 兼容 occurrence KEY SHARE。

input/sources 在首次 await 前复制，既有调度先验锁内版本；创建/编辑锁内重验文档元数据，不重新解释或读 blob。原 ID 缺失拒绝，不换同路径新文档；[删除门禁](document-lifecycle.md#删除事务与引用判定)保护当前规则及保留 occurrence。

### 重叠检查到底看谁

先确认原 occurrence，无原 Run 才查同 Schedule 其他关联。每 Schedule 最多一个 PENDING，未结算不认领下一项，不补历史队列；不检查手动/其他 Schedule，不是全局锁。新建事务再次 fencing/验重叠，解析、物化、模型均在锁外。

## 保存表单与触发预览

ScheduleDialog 与即时执行共用输入组件，发送精确任务/input/sources，必需文档缺失或集合不完整拒绝保存。

保存须当前 definition 的成功非空预览和人工确认；修改即清确认，A→B→A 也不收旧响应。预览仅发 definition，只验时间、不授权资源或创建 Run。

关闭创建弹窗丢草稿；abort/超时非未保存证明。列表可核对当前事实，但无创建原键保证；同名/同配置/不可见均不证明原请求成败。

### 保存后的管理入口

`#/schedules?project=<id>` 独立于模块/TaskCatalog，默认全部状态。服务端 total/limit/offset 分页；q 对 name/task_key 做不区分大小写的字面子串查询，status 单选，created_at/ID 降序。count/items 非原子快照，矛盾须刷新；任务卡汇总读完后续页，遇总数变更/重复不宣称完整。

详情匹配精确 SkillVersion/task；失效仍展示原配置，只读编辑并说明，不跟 latest。目录错误不冒充任务失效、不清列表；归档 Project 只读。编辑保留原来源/未改绝对时刻精度，不归档重建或清计数。

PUT/状态操作携带用户看到的 expected_row_version。冲突并列原值、已发送内容、GET 当前值并保留草稿；人工采用新版本后重新预览确认再保存。未知不自动重发，GET 不证明原写成功；重开编辑保留未决、暂禁同目标其他写入，身份/Project/离页不承诺恢复，不持久存储正文。

任一列表/详情/目录/在途读取的 401/403/404 关闭写入资格，旧成功不复权；共用防重/期限/晚到隔离。旧客户端缺版本为 422，须更新 Web，不补最新版本。字段见[契约](../../SKM/contracts/task-schedule/v1.schema.json)和 [OpenAPI](../../SKM/contracts/openapi/skillmind-api.v1.json)。

### 在途只读核对

GET /projects/{project_id}/schedules/{schedule_id}/activity 按当前 Project 读权限查询，不因创建者/任务失效或暂停/归档隐藏待结算。父配置版本、单 PENDING、DB checked_at 来自同条 SELECT，不锁业务或触发修复。

| 读取结果 | 含义 |
| --- | --- |
| TRACKED + pending | 显示原 UTC occurrence、原/当前配置版本、认领次数/自动上限和租约截止；不是 Run 执行状态 |
| TRACKED + null | 本次读取未见 PENDING；不证明没有 Run、原触发成功或可以重发 |
| LEGACY_UNAVAILABLE | 旧协议无法提供可靠在途追踪，不补空历史、不迁移旧数据 |
| 读取失败 / 409 schedule_activity_unavailable | 保持未核实；损坏关联、快照或多 PENDING 不以第一条/空值掩盖 |

按 checked_at 精确到微秒判到期；未到期非存活证明，到期/耗尽非停止/释放证明。接管不一定改父 row_version，须重新 GET，不按父版本缓存；刷新不复权或清未知意图。

校验原快照/身份后才投影，不含正文/key/snapshot/hash/worker/token；非历史账本、原写回执或人工重试入口。旧 API 不支持则显示读取失败，保留独立详情。

### 时间输入与展示的边界

| 边界 | 规则 |
| --- | --- |
| CRON | 按 definition.timezone 求值，未必是浏览器本地时间 |
| ONCE/end_at 输入 | datetime-local 按明确标注的浏览器时区转绝对时刻；改规则 timezone 不改变输入时区 |
| 预览/摘要 | 按规则时区显示 occurrence 与 UTC offset，不给本地文字拼上另一时区标签 |
| API datetime | run_at/end_at 拒绝无 offset；旧已存绝对时刻不重新解释 |

浏览器 UTC/规则东京：CRON 09:00 是 UTC 00:00，ONCE 输入 09:00 是东京 18:00。非法日期/DST 缺失拒绝，歧义须显式选 offset，不靠 Date 纠正；改输入时区须另定兼容。

## 可靠性保证的限度

### 停机恢复的实际行为

dispatch 不停止 tick，须按[部署](../operations/deployment.md)覆盖所有 job/cron/实例。每小时规则积压 03:00 至 09:30：尝试 03:00 一次，04:00–09:00 计 missed，下次 10:00；ONCE 也无最大迟到拒绝。

_count_missed 单次最多 1000。迟到容忍/截止尚待 ONCE/CRON 产品策略，不临时补跑或改 occurrence。

## 持久认领与结算

持久触发 → 普通幂等创建 → 幂等结算；不另建 Run/hash 或批量补未认领历史。

### 认领记录与恢复权限

认领原子验状态/候选/配置，保存原 actor/任务/规则/input/sources/UTC occurrence/key，再推进候选；唯一身份为 Schedule+UTC occurrence，编辑/接管不换键。

claim 保存 worker/token hash/generation/到期，非 RunAttempt；旧持有者不得晚到关联/结算。锁序为当前身份/Project → Schedule → occurrence → 普通 Run，Skill 解析锁外，快照/关联同事务。

恢复先授权/查原键，有 Run 即关联，无才验本调度其他在途/非终态再创建；创建事务重验 claim 和版本可用性。停用/废弃先提交则 FAILED_PRECONDITION，但须仍有 claim 才能结算；有原 Run 不重验今日可用性。普通入口不得用[保留前缀](run-creation.md#幂等键的作用域)绕过关联/计数新建。

### 配置并发与暂停

编辑版本/状态/写入同一原子边界，核对条件 UPDATE 影响行数；row_version 不同于语义配置版本，tick 不算改输入。

暂停/归档阻止后续认领，不撤回已提交认领；旧 occurrence 用原配置/当前授权完成，不覆盖新定义/状态。公开版本修改须兼容旧客户端并同步消费者。

### 结算、计数与未知结果

同 occurrence 幂等关联/结算，每 Run 只计一次、不等成功；晚结果补自身审计/数量，不回退新摘要。max_runs 原子留名额：确认无 Run 才释放，未知保留/查原键；编辑不清计数，上限不低于已用+占用。ONCE 一次机会不同于 CRON 额度耗尽。

基础设施错误不作 FAILED_PRECONDITION。默认 lease 60 秒、含首次最多 3 次认领，投影共用策略常量；到期原键接管，耗尽保留 PENDING/额度。人工处理/可配置策略未接，不自动释放、换键或强制重跑。

### 历史兼容与实施顺序

0036 增 occurrence/configuration_version/occurrence_protocol。旧行/writer 默认 0，保留 last_*/run_count 不造历史；新创建显式 1。新 tick 遇到期旧行标 ERROR，编辑/恢复不能转换协议。

历史核对/迁移工具未实现；不得改 protocol、清数或归档重建冒充恢复。保留旧 Run/key/快照，历史可读不代表可发火。

发布前停全部 Run/Schedule writer、不混新旧 API/Worker；dispatch=false 不够。0036 排他锁下遇任一 occurrence/新协议 Schedule 拒绝降级，不删审计，也不证明更早迁移安全。

## 实现与验收

入口见[代码根 README](../../SKM/README.md)。

验收 ONCE/CRON/DST/跨时区同一绝对时刻、当前预览确认、编辑冲突保草稿、超过 100 条/任务失效可管理、A/B/C 间崩溃、同版竞争、暂停与旧回写、原键恢复/自身重叠、接管未知提交、跳过不耗创建额度及历史/混合版本。

时间单测、mock 创建和健康 tick 不证明真实事务/多 Worker/发火成功；专用 Project 另验实际 occurrence、停机迟到与 ERROR 恢复。
