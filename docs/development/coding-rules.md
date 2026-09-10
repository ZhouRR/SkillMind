# 按改动类型查阅的实现细则

按 [AGENTS](../../SKM/AGENTS.md)只读本次涉及的章节；领域规则见[变更指南](change-guide.md)，运行命令见[本地开发](local-development.md)。
Backend 路径相对 SKM/backend/src/skillmind/，Web 路径相对 SKM/web/。

## Backend

route 只做认证/入出参，业务在 domain/service，查询/锁/持久化在 repository。公开 route 在 api/routes/ 资源模块单层实现，不增加未认证并行入口、转发 wrapper 或 ARQ job 内业务分支。

同义处理必须复用唯一入口：

| 处理 | 入口 / 限制 |
| --- | --- |
| JSON / SHA-256 | core/hashing.py：canonical_json / sha256_hex；源包索引按 skills/importer.py 的格式计算，不混用 JSON 与原字节摘要 |
| 敏感字段 | core/redaction.py：find_sensitive_key；Evidence/Tool response 共用 |
| Lease token | runs/domain.py：lease_token_hash |
| RunEvent / Outbox | runs/repository_base.py 的 RunRepository：_snapshot_event、_event_outbox、_dispatch_outbox、_next_sequence |
| 认证 Problem | api/auth_dependencies.py 共享 factory；Run 用 routes/runs.py 的 run_not_found_problem / authorized_run |
| binding / Secret | agent/run_binding.py：load_bound_run_resource / resolve_binding_secret；检查 checksum、Integration 状态、provider/revision/capability |
| 外部 repository | agent/repository_client.py / repository_source.py；物化/read/write 共用，不另写 subprocess/凭据解析 |
| 可 apply effect | effects/catalog.py：EFFECT_CAPABILITIES，创建/执行/审批共用 |
| Binary 转文字 | agent/binary_text.py：render_text，不在 Provider 复制 |

documents/__init__.py 只导出 domain 类型，service/repository/source 显式导入，避免 DB → Run → 文档循环。

repository.write/v1 不可预授权。direct 仅默认 branch fast-forward；branch 仅新建 skillmind/ 命名空间 branch；禁止 force、覆盖既有 branch 或任意 branch 写入。协议见[受控写入](../design/repository-effects.md)。

## Run lifecycle

修改执行、认领、续行或取消时，查 [Runtime](../design/agent-runtime.md)和[监督](../design/run-supervision.md)；仅改页面展示先查 [Workspace](../design/workspace.md)，不因此补齐全部执行链路。

- 转换只经 plan_run_transition / ALLOWED_RUN_TRANSITIONS；锁序 Run → Segment → Attempt。旧 migration 路径仍为 Run → Attempt，不反转 _lock_claimed_execution。
- 终态 RUN_SNAPSHOT 是最后一个 RunEvent。sequence 在 Run 内单调；TEXT_DELTA 消耗 sequence 但不持久化，允许缺号、不改子层编号。遵守 RunEvent Schema。
- 重新 dispatch 也受 SKILLMIND_RUN_MAX_ATTEMPTS 限制，耗尽以 retry_exhausted 关闭为 FAILED。
- wall_timeout_seconds 只中断 event 等待，不中断终态 transaction；ARQ job_timeout 必须更长，不能替代执行 deadline。
- 子能力统一经 resolve_subagent_capabilities，分配与消费不混同；权限、返回和 Session 审计见[子分析](../design/subagents.md)。
- 预算执行接线须满足[上线门禁](../design/run-budgets.md#上线门禁与接线顺序)，内部账本不等于实际执行受控。
- 准备遵循[资源快照](../design/resource-snapshots.md)，Result 遵循[结果与评价](../design/results-evaluation.md)，不在 route/job 复制校验。
- TaskSchedule 复用 RunService.create_task_run；版本、失效与迟发行为见[调度](../design/task-scheduling.md)。

## Web

- 资源 client 放 src/api/，画面经 index.ts barrel；HTTP 只用 requestApiJson / requestApiEmpty。mutation 传 csrfToken / X-CSRF-Token，响应在 client 边界验证，纯逻辑放 src/lib/。
- TasksPage 选任务，WorkspacePage 管理 Run；不重造 SSE/取消/终态 lifecycle。即时/调度共用 taskDraft.ts / TaskLaunchFields.tsx。
- 不在前端重算服务端 ID（如 derive_task_id）。筛选/分页在服务端，不能过滤第一页冒充全量。
- 三语统一 src/lib/i18n/{zh,ja,en}.ts，同步 UiMessages / MESSAGES；画面用 useMessages()。
- 实现生成 FrontendModule 时先完成威胁模型与 CSP/iframe 隔离回归，且不继承应用权限，见[生成界面](../design/generated-modules.md)；普通 Web 页面不套用 builder/Host 门禁。

## 同步点

只更新受影响项；公开协议同时遵守[契约 workflow](contract-workflow.md)。

| 改动 | 同步范围 |
| --- | --- |
| Settings | core/settings.py 的 Field 范围 → .env.example → API lifespan / Worker startup 注入 |
| AgentEventType | agent/domain.py → engine.py mapper → Web RUN_EVENT_NAMES → RunEvent Schema |
| Blueprint / Brief / OutcomeEnvelope | contract → DTO/validator/projector → frozen checksum → Worker/Web → example/test；不加业务专属字段 |
| Segment / Interaction / Proposal | transition → DB/repository → event/outbox/SSE → 授权/幂等 → Web → recovery/Compose |
| Tool capability | versioned request/response/error → Provider → context_builder.py registry → allowed_capabilities → 回归 |
| endpoint | 资源 route/actor alias → response allowlist/Problem → Schema/example/OpenAPI → Web validator/barrel → 回归 |
| DB model | db/models.py → migration → repository DTO/projection → 回归；无 relationship 的 FK 父子同事务新增时，父行后显式 flush() |
| Schema / example | 同时注册 scripts/validate_contracts.py 和 backend/tests/contracts/test_contracts.py；保持 Problem 兼容 |
| OpenAPI | endpoint 修改后运行 scripts/export_openapi.py 并验一致性；只读审查不重导出 |
| 结构化 log | 注册 core/logging.py 的 _CONTEXT_FIELDS allowlist |
| system Interpreter Skill | SemVer/content hash、example identity、prompt checksum 与测试同步；model 执行规则需 prompt 回归 |

tests/ 镜像模块职责，测试名说明验证的行为。API 测试按资源拆分，共用 tests/api/fakes.py / conftest.py，不复制认证 fixture；fake SQL 与[真实 DB](local-development.md#実-postgresql-の前提)分开举证。

## Ingress

- 复用共享 Traefik，不新增其 service/静态配置、证书 resolver、dashboard 或 Docker socket mount。
- 不发布 host port；只有 web/api 进入外部 edge network，其余 service 仅内部网络。
- 外部 URL 在 SKILLMIND_CONTEXT_PATH 下：{contextPath}/api 归 API，其余归 Web。Traefik 去掉 context path，内部 API 保持 /api、Web 保持 /。
- 配置与停写/迁移/放行见[发布手册](../operations/deployment.md)，功能开关不能代替全局停写证明。
