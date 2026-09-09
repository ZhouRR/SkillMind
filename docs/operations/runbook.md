# ProjectMind 运维与故障排查

本页按症状分诊；首次起动、发布、数据恢复分别见[Quickstart](quickstart.md)、[发布手册](deployment.md)、[恢复手册](backup-recovery.md)。现有实现与待补范围见[计划](../planning/roadmap.md)，设计目标不代表已有修复 CLI。

## 按问题找入口

| 症状 | 入口 |
| --- | --- |
| 登录、CSRF、权限异常 | [认证](#认证故障的只读分诊) / [登录防护](#登录防护的排查与恢复) |
| 项目消失、归档或删除异常 | [项目](#项目与归档的只读分诊) |
| 上传部分成功、列表与附件不一致 | [文档保存与删除](#文档保存与删除的只读分诊) |
| Run 无会话、准备超时、不断接管 | [准备](#准备故障的只读分诊) / [冻结输入](#资源快照排障) |
| 等待回答/批准、结果或评价可疑 | [WAITING](#waiting-状态) / [结果](#结果与评价的只读分诊) |
| 调度漏跑或计数不符 | [Schedule](#schedule-与-recovery-的监视) |
| 外部写入未知、PR 失败 | [Effect 对账](#incident-与-recovery) |
| 凭据无法解封或准备轮换 | [KEK](#managed-secret-の-kek-運用) |
| 验收或 Worker 故障注入 | [smoke](#通常-smoke) / [接管](#worker-喪失と接管) |

## 运用原则

先使用已有响应、脱敏日志及获准的只读查询。PostgreSQL 是业务/审计正本；不改 Run 状态、stamp migration、删审计或换幂等键消除错误。启动、停机、模型、外部写入与恢复须重新确认目标、副作用和授权。命令在目标环境的 `PJM/` 执行，通过[统一配置入口](deployment.md#环境文件与配置边界)使用已确认的 ENV_FILE 和 COMPOSE_PROJECT_NAME；不要直接切默认 Docker context 操作另一目标。

对外交接只保留 UTC 时点、image/revision、必要对象 ID、公开错误码和检查范围，不附 `.env`、Cookie/CSRF、密码、KEK、内部地址、业务正文或完整 workspace。结果未知先保全原请求身份，不自动重放。

## 认证故障的只读分诊

记录已有响应的 path（无业务参数）、status/code/request_id 与版本。只查 header 是否存在、cookie 属性及刷新顺序，不导出完整 HAR 或凭据表单。

| 原响应 | 核对与停止条件 |
| --- | --- |
| 401 invalid_credentials | 确认环境，不探测账号存在性或重跑 bootstrap |
| 400 current_password_rejected | 改密操作拒绝，不是登录失效；保留仍有效 session |
| 401 authentication_required | 查 cookie、期限、用户/角色/失效及 API 协议；不公开内部原因 |
| 403 csrf_rejected | 查 Origin scheme/host/port、header 类型和是否换会话；不关闭 CSRF 或放宽 cookie |
| 403 administrator_required / 404 | 系统 ADMIN 与 ProjectMember 不同；404 也可能隐藏越权，不枚举或换账号探测 |
| 409 project_archived / 用户冲突 | 按[项目](../design/project-lifecycle.md)或[账户](../design/user-lifecycle.md)规则核对，不擅自恢复项目、换版重发 |
| 429 / 503 | 转[登录防护](#登录防护的排查与恢复)，不当作密码错误 |
| 422 | 按契约修正缺失 header/结构/输入，不显示含密码的原值 |

v2 同 session 的 GET 不轮换 CSRF，但可能更新 idle；login-context 占配额并建 challenge，均非纯只读探针。混合旧 API 按[会话切换](deployment.md#会话协议切换检查)处理。需重登先保全未确认动作的 ID/key；403 不证明此前请求未提交，也不授权自动重发。账户管理没有通用排障 CLI，不用 SQL 或 bootstrap 代替。

## 登录防护的排查与恢复

不连续请求登录接口制造样本。429 记录 Retry-After 后停止快速重试，查共享出口、重复 challenge 与可信代理；一次正常登录通常消耗两次来源请求。等待不保证下次成功，不轮询、换来源或删除配额“解锁”。

503 先区分应用 `login_protection_unavailable` 与代理错误，再查指定 Redis 的可用性、命令权限、容量/驱逐、TTL/状态损坏与超时。PING 不证明 EVAL/SET/GETDEL 可用；前端通用文案也不直接说明根因。

修配置、重启、切 Redis 或主动登录验证已超出只读分诊，需批准的环境、专用账号及影响评审；不提供 FLUSHDB/删 key 命令。修复后分别验来源不可伪造、正常登录、429 等待和 503 不放行验证。登录防护恢复不代表旧会话、Worker 或外部效果恢复。

当前 Web 的 1–300 秒合法值只作静态提示，并非倒计时或强制按钮锁定；通信失败/离页按[提交未知](../design/login-protection.md#提交离页与结果未知)处理，不从未跳页断言没有登录成功。

## 项目与归档的只读分诊

先核对原 project_id、调用者和服务端状态；失效深链接保持原目标，页面须经精确详情授权，导航选中项不能代替该检查。

| 症状 | 核对与停止条件 |
| --- | --- |
| 移出成员后仍可访问 | 系统 ADMIN 不依赖 membership，成员移除不是账户降权 |
| 归档后仍有 Run/Effect | 归档不是取消或全局停写，按原对象停止协议处理 |
| 无 Run 但 DELETE 失败 | TaskSchedule 或成员审计仍会阻止删除；原版本冲突须核对，不拆外键、删历史或循环 DELETE |
| 204 后仍有附件/备份 | 只证明所列元数据删除，不证明字节清理；不递归删目录 |

写入修复先确认权限、目标和可恢复性；正本见[项目生命周期](../design/project-lifecycle.md)。

## 文档保存与删除的只读分诊

保留原 Project/document ID、时点、status/code 与版本；未返回 ID 的上传保留受控本地路径，不把正文/内部 key 放入报告。

| 症状 | 核对与停止条件 |
| --- | --- |
| 同名 409 且占用增加 | blob 先于 metadata 约束写入，可能有孤立对象；不重复上传验证 |
| 目录仅部分完成 | 逐文件分清确认与未知；done 不是成功数，不重传整批 |
| DELETE 报错后 ID 消失 | metadata 可能已提交；404 不恢复清理，不循环删或改删同路径新 ID |
| 删除 409 引用冲突 | document_in_use 保留原引用；document_references_unavailable 表示历史无法核实。核对原 Run/调度/保留 occurrence，不删审计、改 hash 或换同名 ID 绕过 |
| 204 后对象仍在 | S3 适配器可能吞权限等错误，授权存储负责人核对原对象，不清 Project 前缀 |
| 下载 409 document_content_missing / document_content_invalid | 分别表示原 blob 缺失或内容不符；保留原 ID/hash，按获准恢复点核对，不改 checksum 或用同名文件掩盖损坏 |
| 下载 503 document_storage_unavailable | 核对获准存储的可达性与权限，不当作文件缺失或删除成功；不导出内部 key/SDK 正文 |
| 预览超限或样式/图片消失 | 实际 byte 超限会停止读取；HTML 只保留静态结构，主动内容/资源不展示。可下载原文件，但下载后打开的安全性另行判断 |

没有通用孤立对象清理/配额修复 CLI；[文档生命周期](../design/document-lifecycle.md)定义修正边界。冻结输入不能靠同名重传修复。

## 准备故障的只读分诊

查已有 Run/Attempt、日志和输入回执，不启动模型或重做物化。内部回执没有公开管理 API。

| 观察 | 核对与停止条件 |
| --- | --- |
| RUNNING 但无 Session | 运行态与 prepared event 可在 ContextBuilder 前产生，先查准备错误/回执/Brief |
| preparation_timeout | 查准备配置、资源耗时和回执提交；模型 wall timeout 不是替代 |
| context_build_failed | 查脱敏异常、冻结来源、binding 和消费者，不一律归因网络 |
| 首次世代创建被拒绝 | 旧 .projectmind-inputs、其他 UUID 或 symlink 都可阻止；保留现场，不删除或补 READY |
| 执行权异常、不断接管 | 查 lease/token 与 Attempt 时间线，旧 Worker 不得强写终态 |
| 已接受取消但未结束 | 查取消意图/lease/Session；按钮或 CANCELLED 不证明进程退出 |
| READY 但 Run 失败 | 输入就绪不保证 Brief/模型成功，不删完成回执解锁 |

计时器见[执行限额](../design/run-budgets.md#现有计时器的覆盖范围)。提交未知时保持输入不可用、保留原世代，不填 READY 或用新世代覆盖。实际停机测试另见[接管](#worker-喪失と接管)。

## 资源快照排障

核对部署版本、Run sources、冻结 manifest、回执所指世代和实际 revision，不以今日文档列表、HEAD 或同名物理目录代替。缺 ID、hash 不符或混入文件时保留现场，不重传同名文件、删缓存或改 snapshot；可信输入必须改变时创建新 Run，旧事实保留。

响应丢失先按[原创建请求](../design/run-creation.md)确认，已有 Run 的 PREPARING/READY/缺回执则按[输入中断规则](../design/resource-snapshots.md#准备中断与再次使用)处理。hash、目录完整和 migration 成功均不是放行证据；无补签/修复 CLI。

## WAITING 状态

WAITING_FOR_INPUT/APPROVAL 不是终态或 Worker 卡死。查 Interaction、Proposal、Brief/checkpoint、Session 与事件：人工等待释放主执行 lease，但已批准 Effect 可持有独立 lease；不要重复批准。正常答案追加 Segment，技术重试才在原 Segment 追加 Attempt。

普通超期以 INTERACTION_TIMEOUT Segment 续行，不代选答案；批准超期则失效且不写外部。410 可能发生在过期续行提交后，先读保存状态，不换 key 补答案。只有 EFFECT_APPROVAL Interaction 而无 Proposal 时保留现场，不能用普通答复或手写审批修复。

Session resume 要求 transcript/workspace 与 engine/model 兼容；fork 保留 parent/checkpoint；replace 从可信 checkpoint 建新会话。它们不是任意切换 CLI，均不得改变冻结版本、权限、资源或预算。PRIMARY 最多一个 ACTIVE，不以含子会话的 ACTIVE 总数判重。详见[普通交互](../design/user-interactions.md)与[Runtime](../design/agent-runtime.md)。

## 结果与评价的只读分诊

查授权 detail/评价历史，保留 Run/Result/Evaluation ID 与原响应，不复制正文。SUCCEEDED 可与业务 PARTIAL/BLOCKED 并存；模型说 APPLIED 不替代平台 Effect/read-back，引用/Artifact 计数不证明内容可读。

评价 pointer 相对 detail.result.data；400 查原值根，409 查 Result 存在性，401/403 回认证分诊。无评价不等于无 Result。响应丢失不能靠同文/时间确认原提交，也不自动 POST、删除重复项或改原值。见[结果与评价](../design/results-evaluation.md)。

## Schedule 与 Recovery 的监视

tick 的 created/skipped/failed、Schedule 的 last_*、计数与业务结果分开。FAILED_PRECONDITION 回写后进入 ERROR，但基础设施异常未必留下状态；不补造触发或改 DB 计数。

| 异常 | 实际边界 |
| --- | --- |
| 迟到/漏跑 | 当前可能执行最早迟到 occurrence，再跳过后续过期时刻；missed_count 单次最多 1000 |
| last_* 不一致 | 跳过/失败可保留旧 Run ID，不能拼成同一次执行事实 |
| next_run_at 已进但无 Run | 在管理详情刷新“在途核对”，查原 occurrence、配置版本、认领次数和租约截止；可能仍在处理、提交未知或恢复耗尽，不清记录/额度或换键 |
| 在途为空/租约到期 | 仅说明该次查询未见 PENDING 或租约已过期，不证明 Run 不存在、执行停止或可以重发；旧协议/读取失败另列，不补空值 |
| 编辑/停止同时触发 | 认领提交前后的权限不同；已认领项按原配置完成，暂停不撤回在途，旧结算不覆盖新配置/暂停状态 |
| 同 Task 多 Run | 重叠检查覆盖本 Schedule 的全部关联非终态 Run，不覆盖手动或其他 Schedule |
| 页面找不到 Schedule | 打开项目“定时安排”，清除服务端筛选并翻页；失效 task/归档仍可读，不因未找到而重建 |
| 修改冲突/保存响应未知 | 保留草稿与原请求；管理页独立读取、人工比较采用当前版，不自动重发；GET 不是原请求成功证明 |
| 旧状态按钮返回 422 | 新协议要求 expected_row_version；刷新 Web，不让 API 自动补当前版 |
| 历史 Schedule 不能恢复 | protocol=0 须独立历史核对/迁移，工具仍待实现；不改 protocol、清计数或归档重建 |

保留 Schedule ID、带 offset 的 occurrence、原键与日志；时间同时核对规则和浏览器时区。摘要不足就保持未知，不伪造结算。Run/Effect lease、Interaction/Proposal 超期的 recovery 计数分别观察；Effect 技术重试保持原身份，未知远端仍先对账。正本见[调度](../design/task-scheduling.md)。

## Repository 与 CAS 接入

读取先查 [repository source](../../PJM/backend/src/projectmind/agent/repository_source.py)：Git 支持 http/https/file，SVN 另支持 svn，不接受 SSH 或 URI 密码。Secret 格式 username:secret（首冒号切分），无冒号用 x-access-token 用户名；Git 认证经环境、SVN 密码经 stdin，不进 argv。image 需 git/subversion，超时由 PROJECTMIND_REPOSITORY_COMMAND_TIMEOUT_SECONDS 控制。

paths 是硬范围，冻结选择后记录实际 commit/SVN revision。普通不可读文件可 skipped，冻结来源/hash 不符必须失败；原始 stderr 不进入 Agent/Evidence。

Redmine 写入必须经声明 issue.update/v1、atomic_precondition=revision、idempotency=key 的 CAS adapter，先检 well-known Provider 协议，再 pre-read/apply/read-back；不回退普通更新接口。Integration 配置与 SecretReference 变化不得改写原 Run binding。

repository.write/v1 始终人工批准：direct 是缺省，Git fast-forward、不 force，default_revision 必须是具体 branch；branch 使用 projectmind/ 命名空间。同名同内容不证明本次执行。SVN 当前未固定批准基线，不能保证排除已变化的 revision。

自动 PR/MR 仅 Git branch + 完整 forge_kind/forge_api_base_url/forge_project；未配置只保留 branch/commit，部分配置拒绝。PR 失败不撤销 commit，也不替代执行前批准。target_stale 重新观察/提案，见[受控写入](../design/repository-effects.md)。

## Incident 与 recovery

保留关联 ID、Provider/capability 版、Proposal checksum、请求指纹、幂等键 hash、批准与 apply/read-back 状态。分别查批准保存、远端写入、回读、PR 与本地 finalize，不能合成一个 FAILED。

| 原执行的外部事实 | 处理 |
| --- | --- |
| 已应用且回读一致 | 沿原身份确认既有结果，不再写 |
| 明确未应用 | 仍复查批准/scope/期限/revision，只走原受控恢复路径 |
| 未知或部分成功 | 保持隔离对账，不换键、强推、改 SQL 或盲目重试 |

当前重试可能重新运行整个 Provider，无“只补 PR”或通用补账 CLI。Effect lease 过期不证明旧请求停止；恢复 DB 不证明远端未执行。不要仅重启 Worker 促使自动恢复。人工外部修正需受控 incident 记录及适用 Evaluation，不覆盖原 Effect/Result。

## MANAGED Secret の KEK 運用

MANAGED 直接用部署主密钥 AES-256-GCM，不是 DEK/KEK 两层信封。keyring 为逗号分隔 version:base64key，每个 key 为 32 字节；首项新加密，其余解旧密文。通过环境 Secret 管理注入，不展示 key。丢失 KEK 无法由 DB 备份补救。

按[切换与恢复顺序](../design/secret-storage.md#切换与恢复的顺序)确认备份、维护窗口、全部读写进程和旧备份解密条件。API/Worker 启动加载 cipher，仅改 CLI 环境不更新现有实例；新标签必须配新 key，不能同标签换 bytes。

获批窗口在已配置目标环境执行 `python -m projectmind.ops.rotate_secrets`。它锁定并改写材料，不是只读操作，也不改 Run/binding 身份。检查退出/数量与所需解封；active 行 skipped 不验证解密，全部 skipped 不证明成功。没有全量只读解密 CLI，不输出明文替代验证。

旧备份仍需旧 KEK 时继续独立保管。版本不明、密钥缺失或认证失败一律拒绝，不降明文。MANAGED 不防护同时掌控 host/process 与 DB 的攻击者，外部 KMS/HSM 尚未实现。

## 验收与故障注入

### 通常 smoke

smoke 会调用模型、创建 Run/Evaluation 并测试取消，有费用和持久写入。使用获准专用环境、Project 与 published task；先满足[Worker 放行](deployment.md#启动与放行)、dispatch 和模型凭据前置。单独 Project 不隔离旧 job/cron。

明确设置测试 UUID 为 SMOKE_PROJECT_ID 后执行，未指定会拒绝：

```bash
python3 scripts/compose.py -- exec \
  -e PROJECTMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" \
  api python -m projectmind.ops.smoke
```

密码经 TTY。可用 PROJECTMIND_SMOKE_SKILL_VERSION_ID / TASK_KEY 固定任务、TASK_INPUT_JSON / TASK_SOURCES_JSON 提供合法输入（后面三项同样带 PROJECTMIND_SMOKE_ 前缀）。不把真实正文/凭据放共享 shell history。通过覆盖认证、创建重放、结果/证据、追加评价、SSE 与取消，不代表全部 Provider、页面或业务质量。

### Worker 喪失と接管

仅在可停机专用环境注入。分别选择准备中、模型执行中的测试 Run，保留 Run/Segment/Attempt、快照和 lease；RUNNING 不证明模型已启动。

```bash
LEASE_SECONDS="$(python3 scripts/compose.py -- exec -T api python -c \
  'from projectmind.core.settings import get_settings; print(get_settings().run_lease_seconds)')"
python3 scripts/compose.py -- stop worker
```

确认 LEASE_SECONDS 是有效秒数，等待该时长加 recovery cron 观察窗口后才启动 Worker。20 秒只是余量，不保证完成恢复：

```bash
sleep "$((LEASE_SECONDS + 20))"
python3 scripts/compose.py -- start worker
python3 scripts/compose.py -- logs --no-log-prefix --tail=200 worker
```

观察实际 recovery tick：旧 Attempt 为 LEASE_EXPIRED，Run 经 RETRY_PENDING 在原 Segment 追加 Attempt，快照不变；超限以 retry_exhausted 失败。准备中断不允许新 Attempt 补签旧 PREPARING 为 READY。部署版本和真实持久恢复分别验，不用 mock test 或仅模型阶段成功代替。

## Log 確認と incident 記録

在受控终端按服务器响应的 request/trace ID 或 Run/Attempt ID 缩小 JSON 日志，再关联 Session/Effect；客户端自带 X-Request-ID 不是当前服务器审计值，request ID 也不是重放键。

```bash
python3 scripts/compose.py -- logs --no-log-prefix api worker \
  | jq -c 'select(.run_id == "<RUN_ID>" or .trace_id == "<TRACE_ID>")'
```

原日志可能包含敏感上下文，对外只转录[运用原则](#运用原则)所列脱敏事实，不附 Tool 参数/结果、Evidence 或全量配置。
