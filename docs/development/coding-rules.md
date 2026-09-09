# 按改动类型查阅的实现细则

本页由 [AGENTS.md](../../PJM/AGENTS.md)强制引用，只需阅读本次改动涉及的章节。它保留实现级约束，不重复领域设计、运行命令或当前进度；设计入口见[变更指南](change-guide.md)。

下文 Backend 路径以 `PJM/backend/src/projectmind/` 为基准，Web 路径以 `PJM/web/` 为基准。

## Backend

route 只负责认证和入出参，业务判断在 domain/service，查询、锁和持久化在 repository；不得在 route 或 ARQ job 中嵌入业务判断。公开 route 只在 `api/routes/` 的资源模块单层实现，不增加未认证的并行实现或转发 wrapper。

必须复用下列单一实现；新增语义在原入口扩展，不复制一份分支：

| 处理 | 唯一入口与限制 |
| --- | --- |
| JSON 与 SHA-256 | `core/hashing.py` 的 `canonical_json` / `sha256_hex`；`skills/importer.py`、`agent/fixture_providers.py` 已持久化的历史 hash 格式不得顺手改变 |
| 敏感字段 | `core/redaction.py` 的 `find_sensitive_key`，Evidence 与 Tool response 共用词表 |
| Lease token | `runs/domain.py` 的 `lease_token_hash` |
| RunEvent / Outbox | `RunRepository` 的 `_snapshot_event`、`_event_outbox`、`_dispatch_outbox` 与 `_next_sequence`，定义在 `runs/repository_base.py` |
| 认证/授权 Problem | `api/auth_dependencies.py` 的共享 factory；Run 使用 `api/routes/runs.py` 的 `run_not_found_problem` / `authorized_run` |
| 凍结 binding 与 Secret | `agent/run_binding.py` 的 `load_bound_run_resource` / `resolve_binding_secret`，执行前核对 checksum、Integration 状态、provider、revision、capability |
| 外部 repository | `agent/repository_client.py` / `agent/repository_source.py`，物化、read Provider 和 write effect 均走此处，不另写 subprocess 或凭据解析 |
| 可 apply 的 effect | `effects/catalog.py` 的 `EFFECT_CAPABILITIES`，创建、校验、执行与审批重新校验使用同一表 |
| Binary 转文字 | `agent/binary_text.py` 的 `render_text`，新增格式不在各 Provider 单独转换 |

`documents/__init__.py` 只导出 domain 类型；service/repository/source 显式从各模块导入，避免 DB model → Run domain → 文档规则形成循环 import。

`repository.write/v1` 的 `preauthorizable=False` 不得放宽。Integration 的 `write_mode` 决定目标：`direct` 仅默认 branch 的 fast-forward，`branch` 仅新建 `projectmind/` 预留命名空间 branch；均不可 force、覆盖既有 branch 或直接写任意 branch。完整协议见[受控写入](../design/repository-effects.md)。

## Run lifecycle

修改 Run、Worker、Session、调度或子 Agent 时，先读 [Runtime](../design/agent-runtime.md)、[监督与停止](../design/run-supervision.md)及本次涉及的领域设计，再核对以下实现约束。

- 状态转换只经 `plan_run_transition` / `ALLOWED_RUN_TRANSITIONS`。row lock 固定 Run → Segment → Attempt；旧 migration 前路径维持 Run → Attempt，不反转 `_lock_claimed_execution` 的取锁顺序。
- 用户答复/批准追加 Segment，worker retry/recovery 在同 Segment 追加 Attempt；等待期间不持有 worker lease 或 active wall timeout。原请求重发、新运行和 Attempt 恢复分别遵守[创建协议](../design/run-creation.md)，不得省略 actor/Project/精确版校验。
- 终态的 `RUN_SNAPSHOT` 是该 Run 最后一个 event；sequence 在 Run 内严格单调，`TEXT_DELTA` 消耗 sequence 但不持久化，缺号正常。遵守 `contracts/events/run-event/v1.schema.json`，不改成子层级编号。
- 任何重新 dispatch 都经过 claim 的 `PROJECTMIND_RUN_MAX_ATTEMPTS` 上限；耗尽时以 `retry_exhausted` 关闭为 FAILED。
- Executor 强制 `wall_timeout_seconds` deadline，只中断 event 等待，不中断终态 transaction；ARQ `job_timeout` 保持更长，不能替代执行 deadline。
- PRIMARY Session 顺序执行，不允许同时多个 ACTIVE。仅设计许可的只读 `SUBAGENT` / `BRANCH` 可并行；resume/fork/replace 审计 parent 与 checkpoint checksum。
- 子能力只经 `resolve_subagent_capabilities`：禁止集合与按名称判断 write 双重拦截，禁止请求显式拒绝，不静默删减。`split_budget` 整除分配，不逐 branch 重发总预算；子 Session 不写 RunEvent，归入主 Session 的一个 ToolCall + Evidence。
- 预算内部账本不等于实际执行已受约束；主子调用、预约、消费与未知量必须满足 [Run 预算上线门禁](../design/run-budgets.md#上线门禁与接线顺序)。
- 资源只在凍结 scope 内物化为只读 `input/`；超限失败、不截断，不可读文件记 `skipped`，凍结 ID/hash 不符则失败。准备和缓存遵守[资源快照](../design/resource-snapshots.md)。
- TaskSchedule 必须调用 `RunService.create_task_run`，不另建调度专用创建路径；精确 SkillVersion/输入/资源失效时停止为 ERROR，不自动换来源。不追赶停机期间全部发火；当前迟发差距与恢复规则以[调度设计](../design/task-scheduling.md)为准。
- terminal Result 至少满足通用 OutcomeEnvelope；task-specific output Schema 可选，不得仅因未声明它就报 `structured_output_missing`。Result 与人工 Evaluation 的边界见[结果设计](../design/results-evaluation.md)。

## Web

- 资源 API client 放 `src/api/`，画面只从 `index.ts` barrel 导入；HTTP 和 Problem 转换只经 `api/http.ts` 的 `requestApiJson` / `requestApiEmpty`，mutation 传 `csrfToken` 与 `X-CSRF-Token`。响应在 client 边界校验，纯逻辑放 `src/lib/`。
- `TasksPage` 选择要运行的任务，`WorkspacePage` 管理当前 Run；任务中心不再实现一套 SSE/取消/终态 lifecycle。即刻和定时运行共用 `lib/taskDraft.ts` / `components/TaskLaunchFields.tsx`。
- 服务端返回的确定性 ID 不在前端重算，例如 `task_id` 只由 `runs/domain.py` 的 `derive_task_id` 定义。列表过滤在服务端进行，不能过滤第一页后冒充全量结果。
- 三语文案在 `src/lib/i18n/{zh,ja,en}.ts`，同时更新 `messages.ts` 的 `UiMessages` / `MESSAGES`；画面经 `useMessages()` 读取，不硬编码用户文案。
- 生成 FrontendModule 不继承通常 Web application 权限。bundle CSP 与 iframe 隔离必须有回归，M3 之后先完成威胁模型；公开业务 module 与生成模块分开，见[生成界面](../design/generated-modules.md)。

## 同步点

仅更新本次实际受影响的项；不因设计出现新对象就机械增加 table 或公开字段。涉及公开协议时同时遵守[契约变更与联调](contract-workflow.md)。

| 改动 | 必须同步 |
| --- | --- |
| Settings | `core/settings.py` 的 Field 范围 → `.env.example` → API lifespan / Worker startup 的注入 |
| AgentEventType | `agent/domain.py` → `agent/engine.py` mapper → Web `RUN_EVENT_NAMES` → RunEvent Schema |
| Blueprint / Brief / OutcomeEnvelope | 通用 contract → DTO/validator/projector → frozen checksum → Worker → Web validator/view → example/test；不加入业务专属 field |
| Segment / Interaction / Proposal | transition → DB/migration/repository → event/outbox/SSE → 授权/幂等 → Web → recovery/Compose 回归 |
| Tool capability | versioned request/response/error Schema → Provider → `agent/context_builder.py` registry → permission snapshot `allowed_capabilities` → 回归 |
| 公开 endpoint | 资源 route 与 actor alias → response field allowlist / `ProblemException` → Schema/example/OpenAPI → Web client validator/barrel → 前后端回归 |
| DB model | `db/models.py` → Alembic migration → repository DTO/projection → 回归；无 relationship 的 FK 父子同 transaction 新增时，父行后显式 `flush()`，不依赖 mapper INSERT 顺序 |
| Schema / example | 同时注册 `scripts/validate_contracts.py` 与 `backend/tests/contracts/test_contracts.py` 两份 example 表，保持 Problem 契约兼容 |
| OpenAPI | endpoint 改动后运行 `scripts/export_openapi.py` 并保留 snapshot 一致性回归；只读审查不重导出掩盖缺口 |
| 结构化 log | 在 `core/logging.py` 的 `_CONTEXT_FIELDS` allowlist 注册，否则字段会被丢弃 |
| system Interpreter Skill | SKILL.md/references 变更同步 SemVer、content hash、request/response example identity、prompt checksum 与测试；由 model 执行的规则须有 prompt 内容回归 |

测试置于 `tests/` 并镜像模块职责。API contract test 按资源分文件，共用 `tests/api/fakes.py` 和 `tests/api/conftest.py`，不复制认证 fixture。真实 DB 与 fake SQL 的证明范围分开，运行前读[验证与副作用](local-development.md#変更に応じた検証)。

## Ingress

- 复用已存在的共享 Traefik；不新增 Traefik service、静态配置、证书 resolver、dashboard 或 Docker socket mount。
- ProjectMind container 不发布 host port；只有 `web` / `api` 进入外部 edge network，PostgreSQL、Redis、MinIO、Worker、Sandbox 只进内部网络。
- 外部 URL 都在 `PROJECTMIND_CONTEXT_PATH` 下，`{contextPath}/api` 归 API，其余归 Web。Traefik 去掉 context path，内部 FastAPI route 保持 `/api`、Web 静态 route 保持 `/`。
- 设置来源与停写、迁移、放行遵守[发布与迁移](../operations/deployment.md)；功能开关不能冒充全局停止证明。
