# Skillmind 运维与故障排查

按症状分诊；起动、发布、恢复分别见[Quickstart](quickstart.md)、[发布](deployment.md)、[恢复](backup-recovery.md)。缺口见[计划](../planning/roadmap.md)，设计不代表已有修复 CLI。

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

按症状选择对应一节，优先已有响应/脱敏日志/获准只读查询，不把整页当日常检查清单。PostgreSQL 是正本，不改状态、stamp、删审计或换键消错；未知保全原身份，不自动重放。仅请求排障时不擅自停机、调模型、外部写入或恢复；已授权的部署按[发布流程](deployment.md)执行，不逐命令重复确认。

命令在服务器四文件部署目录使用已确认的 ENV_FILE/project，配置见[统一入口](deployment.md#环境文件与配置边界)，不切默认 context 误操作。

对外只留 UTC、image/revision、对象 ID、公开错误码和范围；不附 `.env`、Cookie/CSRF、密码/KEK、内部地址、正文或完整 workspace/HAR。

## 认证故障的只读分诊

记录原 path（无参数）、status/code/request_id 和版本，只查 header 存在性、cookie 属性和刷新顺序。

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

GET 不轮换 v2 CSRF 但可更新 idle，login-context 占配额/建 challenge，均非纯只读探针。旧 API 混跑按[会话切换](deployment.md#会话协议切换检查)处理；重登先保全未确认 ID/key，403 不证明未提交。无通用账户排障 CLI，不用 SQL/bootstrap 代替。

## 登录防护的排查与恢复

429 记录 Retry-After 并停止快速重试，查共享出口/challenge/可信代理；正常登录通常消耗两次来源请求。不连续探测、轮询、换来源或删配额解锁，等待不保证成功。

503 区分 `login_protection_unavailable` 与代理错误，查指定 Redis 的权限、容量/驱逐、TTL/损坏和超时；PING 不证明 EVAL/SET/GETDEL 可用。

修配置/重启/切 Redis/主动登录须获批环境、专用账号和影响评审；不 FLUSHDB/删 key。修复后验来源不可伪造、正常登录、429 等待及 503 拒绝；不据此放行旧会话/Worker/Effect。

Web 的 1–300 秒只作静态提示；通信失败/离页按[提交未知](../design/login-protection.md#提交离页与结果未知)处理，未跳页不证明登录未成功。

## 项目与归档的只读分诊

核对原 project_id/调用者/服务端状态；失效链接保留原目标，导航选中不代替详情授权。

| 症状 | 核对与停止条件 |
| --- | --- |
| 移出成员后仍可访问 | 系统 ADMIN 不依赖 membership，成员移除不是账户降权 |
| 归档后仍有 Run/Effect | 归档不是取消或全局停写，按原对象停止协议处理 |
| 无 Run 但 DELETE 失败 | TaskSchedule、成员审计、文档/上传意图/清理记录仍会阻止删除；原版本冲突须核对，不拆外键、删历史或循环 DELETE |
| 204 后仍有附件/备份 | 只证明所列元数据删除，不证明字节清理；不递归删目录 |

修复边界见[项目生命周期](../design/project-lifecycle.md)。

## 文档保存与删除的只读分诊

保留原 Project/document ID、upload key、时点/code/版本；无文档 ID 用原 key 查，文件受控保全，正文/storage key 不入报告。

| 症状 | 核对与停止条件 |
| --- | --- |
| 同名 409 | 当前目录/PENDING 占用在 PUT 前拒绝；旧 writer/孤立对象另查，不重复上传验证 |
| 409 document_upload_pending / document_upload_key_conflict | 原 key 待确认 / 绑定另一输入；人工 GET 原 key，不换键重传 |
| 原 key GET PENDING / 404 | 已预约未确认 / 当前未查到；均不证明旧 POST 不继续，不释放占用 |
| 原 key GET PUBLISHED，目录无文档 | 原发布成立，元数据可能后来删除；按原 ID 核对，不补建 |
| 413 document_upload_too_large | 实际文件/multipart 超限，本次未进存储；缩小请求，不伪报 Content-Length |
| 422 invalid_document_upload | 查单 file、可选 UTF-8 folder、重复字段/参数和完整结束边界 |
| 422 invalid_document_upload_key | 须单个非 nil UUID header；核对 API/Web 版本，不换键重试未知 |
| 目录部分完成 | 分开已发布/拒绝/未知/未发送；未知暂停后续，查原回执后人工继续 |
| DELETE 报错后 ID 消失 | 可能已提交 metadata；404 不恢复清理，不循环删或删同路径新 ID |
| 删除 409 | document_in_use 为原引用，document_references_unavailable 为历史无法核实；查 Run/调度/occurrence，不删审计、改 hash 或换 ID |
| 204 后对象仍在 | 不证明旧版本/在途 PUT/全部字节已清理，不清 Project 前缀 |
| 下载 409 document_content_missing / document_content_invalid | 原 blob 缺失 / 内容不符；按原 ID/hash 与恢复点核对，不改 hash/同名覆盖 |
| 503 document_storage_unavailable | 查 namespace/归属、存储可达性/权限；上传/删除可能已写，不推导回滚或清理成功 |
| 503 document_upload_unavailable | 原意图/回执/占用无法核实，保留身份，不删记录补成功 |
| 删除后配额未减 | 新意图 size 保留，旧占用转清理记录；精确结算未接，不凭 204/对象查无扣减 |
| 切存储后旧附件不可读 | 旧行不自动绑定，按[归属](../design/document-lifecycle.md#存储归属与配置切换)核原 ID/key/hash，不改摘要/复用 UUID |
| 预览超限/资源消失 | 实际 byte 超限停读，HTML 仅静态结构；原文件可下载，但打开安全性另判 |

无通用孤立清理/配额修复 CLI；按[文档生命周期](../design/document-lifecycle.md)处理，不同名重传修复冻结输入。

## 准备故障的只读分诊

只查 Run/Attempt、日志与输入回执，不启动模型/重做物化；回执无公开管理 API。

| 观察 | 核对与停止条件 |
| --- | --- |
| RUNNING 但无 Session | 运行态与 prepared event 可在 ContextBuilder 前产生，先查准备错误/回执/Brief |
| preparation_timeout | 查准备配置、资源耗时和回执提交；模型 wall timeout 不是替代 |
| context_build_failed | 查脱敏异常、冻结来源、binding 和消费者，不一律归因网络 |
| 首次世代创建被拒绝 | 旧 .skillmind-inputs、其他 UUID 或 symlink 都可阻止；保留现场，不删除或补 READY |
| 执行权异常、不断接管 | 查 lease/token 与 Attempt 时间线，旧 Worker 不得强写终态 |
| 已接受取消但未结束 | 查取消意图/lease/Session；按钮或 CANCELLED 不证明进程退出 |
| READY 但 Run 失败 | 输入就绪不保证 Brief/模型成功，不删完成回执解锁 |

计时器见[执行限额](../design/run-budgets.md#现有计时器的覆盖范围)。未知保留原世代、输入不可用，不补 READY/换世代；停机试验见[接管](#worker-喪失と接管)。

## 资源快照排障

核部署版本、Run sources/manifest、回执世代和实际 revision，不用今日目录/HEAD/同名路径代替。缺 ID/hash 不符/混入文件保留现场，不重传、删缓存或改快照；输入改变建新 Run。

丢响应先确认[原创建请求](../design/run-creation.md)，已有 Run 按[准备中断规则](../design/resource-snapshots.md#准备中断与再次使用)处理；hash/目录完整/迁移成功不授权放行，无补签 CLI。

## WAITING 状态

WAITING_FOR_INPUT/APPROVAL 非终态；查 Interaction/Proposal、Brief/checkpoint、Session/事件。等待释放主 lease，已批准 Effect 可独立执行，不重复批准。答案追加 Segment，技术重试才追加 Attempt。

普通超期追加 INTERACTION_TIMEOUT，不代答；批准超期失效。410 可能已提交续行，先读状态，不换键补答；悬空 EFFECT_APPROVAL 不用普通答复/手写审批修复。

resume 须 transcript/workspace 与 engine/model 兼容；fork 保留 parent/checkpoint，replace 从可信 checkpoint 建会话，均不改冻结版本/权限/资源/预算。仅 PRIMARY 限一个 ACTIVE，不把子会话算重；见[交互](../design/user-interactions.md)、[Runtime](../design/agent-runtime.md)。

## 结果与评价的只读分诊

查授权 detail/历史，保留原 ID/响应，不复制正文。SUCCEEDED 可与 PARTIAL/BLOCKED 并存；模型 APPLIED 不替代平台 read-back，引用计数不证明可读。

评价 pointer 相对 detail.result.data；400 查原值根，409 查 Result，401/403 回认证分诊。无评价非无 Result，同文/时间非原提交证明；未知不自动 POST、删重复或改原值，见[结果评价](../design/results-evaluation.md)。

## Schedule 与 Recovery 的监视

tick 计数、Schedule last_* 与业务结果分开。FAILED_PRECONDITION 回写 ERROR，基础设施异常未必留状态；不补触发或改 DB 计数。

| 异常 | 实际边界 |
| --- | --- |
| 迟到/漏跑 | 当前可能执行最早迟到 occurrence，再跳过后续过期时刻；missed_count 单次最多 1000 |
| last_* 不一致 | 跳过/失败可保留旧 Run ID，不能拼成同一次执行事实 |
| next_run_at 已进但无 Run | 刷新“在途核对”，查原 occurrence/版本/认领/租约；可能处理、未知或恢复耗尽，不清记录/额度或换键 |
| 在途为空/租约到期 | 不证明 Run 不存在、已停止或可重发；旧协议/读取失败另列，不补空值 |
| 编辑/停止同时触发 | 认领提交后沿原配置完成，暂停不撤回在途；旧结算不覆盖新配置/暂停状态 |
| 同 Task 多 Run | 重叠检查覆盖本 Schedule 的全部关联非终态 Run，不覆盖手动或其他 Schedule |
| 页面找不到 Schedule | 在“定时安排”清筛选/翻页；失效 task/归档仍可读，不重建 |
| 冲突/保存未知 | 保留草稿/原请求，独立读取并人工比较采用版本；GET 不证明原提交，不自动重发 |
| 旧状态按钮返回 422 | 新协议要求 expected_row_version；刷新 Web，不让 API 自动补当前版 |
| 历史 Schedule 不能恢复 | protocol=0 须独立历史核对/迁移，工具仍待实现；不改 protocol、清计数或归档重建 |

保留 ID、带 offset 的 occurrence、原键/日志，核规则与浏览器时区；摘要不足保持未知。Run/Effect lease 与交互/批准超期分别观察，Effect 未知先对账，见[调度](../design/task-scheduling.md)。

## Repository 与 CAS 接入

按 [repository source](../../SKM/backend/src/skillmind/agent/repository_source.py)核验：Git 支持 http/https/file，SVN 另支持 svn，禁 SSH/URI 密码；Secret 首冒号分 username:secret，无冒号用 x-access-token。Git 凭据经环境、SVN 经 stdin，不入 argv；image 需 git/subversion，超时用 SKILLMIND_REPOSITORY_COMMAND_TIMEOUT_SECONDS。

paths 是硬范围，记录实际 commit/revision；普通不可读可 skipped，冻结来源/hash 不符必须失败，原 stderr 不进 Agent/Evidence。

Redmine 须 CAS adapter 声明 issue.update/v1、atomic_precondition=revision、idempotency=key，经 well-known 校验与 pre-read/apply/read-back，不回退普通更新；配置/Secret 变化不改原 binding。

repository.write/v1 始终人工批准：默认 direct、Git fast-forward/不 force、具体 default branch；branch 用 skillmind/。同名同内容不证明原执行；SVN 未固定批准基线，仍可能包含变化 revision。

PR/MR 仅 Git branch 且完整 forge_kind/forge_api_base_url/forge_project；全无则只保留 branch/commit，部分配置拒绝。PR 失败不撤销 commit；target_stale 重新观察/提案，见[受控写入](../design/repository-effects.md)。

## Incident 与 recovery

保留 ID、Provider/capability 版、Proposal checksum、请求指纹/键 hash；批准、远端写入、read-back、PR、本地 finalize 分阶段核对，不合成 FAILED。

| 原执行的外部事实 | 处理 |
| --- | --- |
| 已应用且回读一致 | 沿原身份确认既有结果，不再写 |
| 明确未应用 | 仍复查批准/scope/期限/revision，只走原受控恢复路径 |
| 未知或部分成功 | 保持隔离对账，不换键、强推、改 SQL 或盲目重试 |

重试可能重跑整个 Provider，无“只补 PR”/通用补账 CLI。lease 到期不证明旧请求停止，恢复 DB 不证明远端未写；不靠重启 Worker 促恢复。人工修正须 incident 及适用 Evaluation，不覆盖原 Effect/Result。

## MANAGED Secret の KEK 運用

keyring 格式与加密边界见[Secret 正本](../design/secret-storage.md)；经 Secret 管理注入，DB 备份补不了丢失 KEK。

按[切换/恢复](../design/secret-storage.md#切换与恢复的顺序)核备份、维护窗口、全部进程及旧备份解封。API/Worker 启动加载 cipher，改 CLI 环境不更新实例；新标签必须配新 key，不能同标签换 bytes。

获批后在目标环境执行 `docker compose --env-file "${ENV_FILE:-.env}" --file compose.yml exec api python -m skillmind.ops.rotate_secrets`，会锁定/改写材料，不改 Run/binding。核退出/数量及必要解封；skipped 不验证解密，全 skipped 不证明成功。无全量只读解密 CLI，不输出明文替验。

旧备份所需 KEK 继续独立保管；版本不明/缺 key/认证失败拒绝，不降明文。不能防同时掌控 host/process 与 DB 的攻击者，KMS/HSM 未实现。

## 验收与故障注入

### 通常 smoke

这里是业务执行验收，不是普通部署或页面检查的默认步骤。smoke 调模型、创建 Run/Evaluation、测试取消，会计费/持久写入。须获准专用环境/Project/published task，并满足[Worker 放行](deployment.md#启动与放行)、dispatch/模型凭据；单独 Project 不隔离旧 job/cron。

明确设置测试 UUID 为 SMOKE_PROJECT_ID 后执行，未指定会拒绝：

```bash
docker compose --env-file "${ENV_FILE:-.env}" --file compose.yml exec \
  -e SKILLMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" \
  api python -m skillmind.ops.smoke
```

密码经 TTY。SKILLMIND_SMOKE_ 前缀的 SKILL_VERSION_ID/TASK_KEY 固定任务，TASK_INPUT_JSON/TASK_SOURCES_JSON 提供输入；正文/凭据不入共享 history。通过仅覆盖认证、创建重放、结果/证据、评价、SSE/取消，不证明全部 Provider/页面/质量。

### Worker 喪失と接管

仅可停机专用环境，分别选准备中/模型执行中 Run，保留原 ID、快照/lease；RUNNING 不证明模型已启动。

```bash
LEASE_SECONDS="$(docker compose --env-file "${ENV_FILE:-.env}" --file compose.yml exec -T api python -c \
  'from skillmind.core.settings import get_settings; print(get_settings().run_lease_seconds)')"
docker compose --env-file "${ENV_FILE:-.env}" --file compose.yml stop worker
```

确认 LEASE_SECONDS 为有效秒数，加 recovery cron 观察窗口后再启动；20 秒余量不保证恢复完成：

```bash
sleep "$((LEASE_SECONDS + 20))"
docker compose --env-file "${ENV_FILE:-.env}" --file compose.yml start worker
docker compose --env-file "${ENV_FILE:-.env}" --file compose.yml logs --no-log-prefix --tail=200 worker
```

观察实际 tick：旧 Attempt 为 LEASE_EXPIRED，经 RETRY_PENDING 在原 Segment 追加 Attempt，快照不变；超限 retry_exhausted。新 Attempt 不补签旧 PREPARING。部署版本/真实持久恢复分别验，mock 或仅模型阶段成功不能替代。

## Log 確認と incident 記録

受控终端按服务器 request/trace 或 Run/Attempt ID 查日志并关联 Session/Effect；客户端 X-Request-ID 非服务器审计值，request ID 非重放键。

```bash
docker compose --env-file "${ENV_FILE:-.env}" --file compose.yml logs --no-log-prefix --tail=200 api worker
```

原日志可能敏感，只转录[运用原则](#运用原则)的脱敏事实，不附 Tool 正文/Evidence/配置。
