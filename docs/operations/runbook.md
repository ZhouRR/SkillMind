# Skillmind 运维与故障排查

从症状定位服务和原请求，再决定处理方式。启动见[Quickstart](quickstart.md)、升级见[发布](deployment.md)、数据恢复见[恢复](backup-recovery.md)，运行边界见[运行与资源指南](../development/runtime-guide.md)。

## 运用原则

优先查看原响应、Run/Attempt/Effect ID 和脱敏日志。PostgreSQL 是正本；不改状态、删审计、清队列或换键消除错误。只分享 UTC、版本、对象 ID、公开错误码和影响范围，不上传 `.env`、凭据或业务正文。

命令在已确认 ENV_FILE/project 的部署目录执行。已授权操作按其范围继续；排障本身不授权真实模型、停机、恢复或外部写入。

## 服务与队列

```bash
make status
make runtime-check
docker compose --env-file "${ENV_FILE:-.env}" logs --no-log-prefix --tail=200 api worker maintenance
```

| 症状 | 核对 |
| --- | --- |
| API 不健康或版本不一致 | 部署阶段、migration head、DB/Redis 和 runtime-check；按[发布](deployment.md#linux-核验与部署)定位原错误 |
| 新任务不执行、批准后不续行 | dispatch 开关、`worker` 与 `maintenance` 健康状态、原 Outbox/job；两队列都要运行，不重建 Run 催执行 |
| 执行慢、页面更新慢 | 按[耗时观测](run-performance.md)区分准备、模型/工具等待、收尾与展示 |
| 模型认证或网络失败 | Worker 设备登录状态、出口代理和认证/模型服务连通性；配置 fingerprint 不验证登录 |

## 认证故障的只读分诊

记录 path（无参数）、status/code/request_id，只核 header 存在性、cookie 属性和 Origin，不记录值。

| 响应 | 处理 |
| --- | --- |
| 401 invalid_credentials / authentication_required | 核环境、会话期限和权限；bootstrap 只用于首个 ADMIN，不能重置账户 |
| 403 csrf_rejected | 核 Origin scheme/host/port、CSRF header 与当前会话；保全未确认请求，不直接重发 |
| 403 administrator_required / 404 | 区分系统 ADMIN 与 ProjectMember；404 也可能隐藏越权 |
| 409 project_archived / 版本冲突 | 读取原对象最新状态，保留原请求；归档不会取消在途 Run/Effect |
| 422 | 按公开契约修正输入，不分享包含密码的原值 |

会话读取可能更新 idle，login-context 会建立 challenge 并消耗配额，不能反复请求作为健康探针。修改密码被拒绝不代表当前会话失效。

## 登录防护的排查与恢复

429 按 `Retry-After` 停止快速重试，核共享出口和可信代理。503 `login_protection_unavailable` 核 Redis 权限、容量、TTL 与超时；PING 不能证明登录所需命令可用。不 FLUSHDB、删配额或轮换来源绕过限制。

## 文档保存与删除的只读分诊

保留 Project/document ID、原 upload key 和响应。超限或 MIME 拒绝应修正文件/配置；存储异常先核 namespace、连接和权限。

| 症状 | 处理 |
| --- | --- |
| 上传未知、PENDING 或原 key 查无 | 继续按原 key 核回执，不换键重传或释放占用；查无不证明旧请求未提交 |
| 已 PUBLISHED 但列表无文件 | 按原 document ID 核后续移动/删除，不同名补建 |
| 删除 409 | 查 Run、调度及历史引用；引用无法确认时保留，不拆外键或删审计 |
| 下载缺失/hash 不符、切存储后不可读 | 按原 ID/key/hash 和恢复点核对，不改 hash、复用 namespace 或覆盖同名文件 |
| 删除后对象/配额仍在 | 元数据删除不证明存储清理和配额结算完成；无通用孤立对象清理 CLI，不递归删前缀 |

项目删除还受调度、成员审计及文档等引用保护；没有 Run 不代表可以删除项目。

## 准备故障的只读分诊

`RUNNING` 且无 Session 时先查 Context 准备、输入回执和 Brief。`preparation_timeout` 核准备超时配置与资源耗时；`context_build_failed` 核冻结来源和绑定。保留原输入世代及 workspace，不删目录、补 READY 或用今日 HEAD/同名文件替代冻结输入。

反复接管时核 lease、Attempt 时间线和错误；租约到期不证明旧调用已停止。已接受取消也不证明 SDK/远端进程已退出。恢复能力须按实际 engine/model、输入和会话材料验证。

## WAITING 状态

`WAITING_FOR_INPUT/APPROVAL` 是非终态。查看原 Interaction/Proposal 和期限后答复或批准；已批准 Effect 可独立执行，不重复批准。请求返回超时、410 或断线时先读原状态；自动批准范围以原 Run 启动同意为准，开关或 Project 权限不能补造同意。

## 结果与评价的只读分诊

Run 成功不等于业务全部完成；同时查看结果的 PARTIAL/BLOCKED、证据与平台写入回执。模型称已保存不替代 read-back。评价针对原 Result，pointer 相对 `detail.result.data`；保留提交原键，未知不重复 POST，也不覆盖原结果。

## Schedule 与 Recovery 的监视

核 `SCHEDULING_ENABLED`、dispatch、维护 Worker、规则时区和原 occurrence。暂停/修改不撤回已认领触发，last_* 摘要和业务结果须分别读取。`next_run_at` 前进而没有 Run 时查“在途核对”，不清计数或重建调度补跑；租约到期与查询为空都不是可重发证明。

## Incident 与 recovery

沿原 Proposal/Effect、请求键、批准与远端回执核事实。执行详情提供只读核对时，CONFIRMED 只证明原操作被观察到，不保证 Run 已续行；NOT_OBSERVED/CONFLICT 不授权重推。明确未应用仍需复查授权/范围/期限/revision；未知或部分成功保持原记录，不换键、强推或以重启 Worker 促恢复。

外部 Git commit、数据库事务、文档保存和 MCP 调用均可能先于本地收尾完成。数据库恢复也不能撤销它们；没有通用补账或“只补 PR” CLI。修正保留原结果和适用 Evaluation/incident 记录。

## MANAGED Secret の KEK 運用

`SKILLMIND_MANAGED_SECRET_KEK` 使用 `version:base64key` 的逗号分隔 keyring，每个 key 为 32 字节，首项用于新加密，旧项用于解密。轮换先保全所需旧 key，让全部持钥进程加载“新 active + 旧 key”，再执行：

```bash
docker compose --env-file "${ENV_FILE:-.env}" exec api python -m skillmind.ops.rotate_secrets
```

命令会重封装数据库密文；检查退出状态及 rotated/skipped，skipped 不验证解密。cipher 在进程启动时加载，只改 CLI 环境不会更新服务。不同 key 使用不同版本名，旧备份所需 key 独立保留；DB 备份无法补回丢失 KEK。

## 通常 smoke

仅在获准测试环境执行；会调用模型、创建 Run/Evaluation 并测试取消。指定已有 Project 和已发布任务，不能把新 Project 当作旧队列的隔离措施：

```bash
docker compose --env-file "${ENV_FILE:-.env}" exec \
  -e SKILLMIND_SMOKE_PROJECT_ID="${SMOKE_PROJECT_ID:?Specify the test project UUID}" \
  api python -m skillmind.ops.smoke
```

密码通过 TTY。可用 `SKILLMIND_SMOKE_SKILL_VERSION_ID`、`TASK_KEY`、`TASK_INPUT_JSON`、`TASK_SOURCES_JSON`（后三者也加 `SKILLMIND_SMOKE_` 前缀）指定任务。通过只覆盖脚本所验链路，不代表全部 Provider、质量或灾难恢复通过。Worker 停机/接管试验另在允许中断的隔离环境执行。
