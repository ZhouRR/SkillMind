# ProjectMind 交付历史

> 这里保留各日期的交付证据和当时边界，不代表所有内容仍适用。旧 seed、能力限制、测试数量和部署结果应按记录日期理解；当前状态见[实施计划 §13](../planning/roadmap.md#13-当前执行状态)，现行规则见[设计目录](../README.md)。新记录追加到文末，不覆盖历史结论。

归档日期：2026-07-07（§9 于 2026-07-11、§21–25 于 2026-07-18、§26–28 于 2026-07-19、§30–§39 于 2026-07-25 追加；其中 §30/§31 为按 `docs/01` 与 `docs/13` 回填的既往工作包记录）

本文合并原 `projectmind-mvp`、Identity/Project/认证计划、回归与运维计划、文档重整计划及其重复执行报告，并归档已完成的「文件与文档管理」工作包计划。这里只保留交付结论、关键产物和后续必须守护的边界；逐步骤状态文件与已完成的计划文件已删除。

## 1. 基础执行闭环总结

基础执行闭环已完成从产品契约到 `jaf.ticket.analyze` 的可部署纵向链路：

- 产品方向、领域模型、Skill compatibility、Runtime、Workspace 和 JAF acceptance profile 已形成正式规格。
- FastAPI/ARQ/React、PostgreSQL/Redis、transactional Outbox、RunAttempt lease/recovery 和 SSE replay 已落地。
- AgentEngine、Claude Agent SDK adapter、受控 ToolRegistry/MCP Gateway、EvidenceWriter 和 ResultValidator 已接通。
- Result 不可变，人工评价以 Evaluation 追加；RunSkillSnapshot 固定 published SkillVersion/checksum。
- Docker Compose 接入共享 Traefik，具备 migration、backup、restore、rollback 和 smoke 路径。
- JAF Native seed、CSV/Git fixture 和 contract 作为基础闭环回归资产保留，不再定义平台通用边界。

## 2. 产品规划与正式规格（2026-06-24 至 2026-06-30）

| 主题 | 完成内容 | 正本 |
| --- | --- | --- |
| 产品与初始范围 | 产品定位、MVP 范围、不做事项和边界 | `docs/planning/roadmap.md` |
| Domain 与权限 | Organization/User/Project、Skill、Run、Evidence、Result、Evaluation 不变条件 | `docs/design/domain-model.md` |
| Skill compatibility | Adapter、NormalizedSkillPackage、Interpreter、Manifest、发布门禁和版本规则 | `docs/design/skill-contract.md` |
| Runtime | AgentEngine、Session、Tool、permission、Evidence、恢复与资源边界 | `docs/design/agent-runtime.md` |
| Workspace | standard ViewSpec、执行交互、Result/Evaluation 和后续 generated UI 边界 | `docs/design/workspace.md` |
| JAF acceptance | 单 Ticket contract、fixture、benchmark 目标和迁移约束 | `docs/acceptance/jaf-quality.md` |

本轮规划的关键取舍是先完成单 Ticket execution spine，暂缓通用模型 Interpreter、调度、generated FrontendModule、外部 write 和 JAF 其余 5 类任务。

## 3. 基础执行闭环工程交付

### 3.1 Task execution

- Run 创建使用 Project/task/idempotency identity，冲突 fail closed。
- 状态转换统一经过 domain transition；Run → Attempt lock 顺序固定。
- Worker 使用 lease、heartbeat、attempt 上限、wall timeout 和过期接管。
- terminal RUN_SNAPSHOT 是 Run 最后持久事件；SSE 支持 Last-Event-ID replay。
- 取消 intent durable 保存，Worker interrupt 后进入 `CANCELLED`。

### 3.2 Agent、Tool 与审计

- Business 层依赖 `AgentEngine`，不直接依赖 SDK 类型。
- 内置 Read/Glob/Grep/Bash/Write/Edit/Web 默认不暴露。
- 仅注册 capability 和精确参数 contract 可执行；Evidence 先于 Result 保存。
- ToolCall、PermissionDecision、AgentSession、Evidence、Result 和 Evaluation 均可追溯。
- 结构化日志只允许关联 identity，不记录 password、Ticket 正文或 input payload。

### 3.3 Skill 发布基础

- 已实现 Directory/Generic adapter、安全扫描、NormalizedSkillPackage 和 Assisted draft。
- SkillSource/SkillInterpretation 追加式保存，相同冻结 identity 可复用。
- 已实现 Skill、SkillVersion DRAFT、RuntimeManifest checksum、hard gate 和 warning acceptance。
- 基础闭环的 Native PUBLISHED version 与每个 RunSkillSnapshot 精确绑定；Runtime 不读取“最新版本”。
- Assisted interpretation 不执行来源脚本、不继承外部 Tool 声明且不可发布。

## 4. Identity、Project 与认证（2026-07-04 至 2026-07-05）

原认证、Identity 与 Project 子计划合并结论如下：

1. **Project CRUD 与 membership**：实现 Project 创建、metadata 更新、归档、成员管理、组织/项目隔离和 ADMIN/USER 权限测试。
2. **Web auth 与 Project context**：接入 login/session/logout、授权 Project 列表、创建/归档、空状态和 USER 操作隐藏。
3. **业务 route 收口**：Skills、Run、Result、Evidence、Evaluation、SSE 全部使用共享 actor dependency；不存在与越权统一为 404，unsafe request 强制 Origin/CSRF。
4. **Project preference**：用户选择持久化到账号，hash URL 携带 Project context；无效或越权 Project 不暗中回退。
5. **审计 identity**：Run permission snapshot、Evaluation 和 Skill publish 使用真实 actor，不再使用旧固定用户。

关键实现保留在 `api/auth_dependencies.py`、资源别 route、identity/project service、Web `src/api/` 和 `docs/design/authentication.md`。

## 5. 回归与可运维化（2026-07-05 至 2026-07-07）

- smoke client 支持 login cookie、CSRF、幂等 replay、SSE cursor、Evaluation 不变性和 active Run 取消。
- 回归矩阵覆盖非法 structured Result、Tool hard deny、Worker1 lease 过期到 Worker2 Attempt 接管。
- API/Worker/Executor 使用 allowlist JSON logging，关联 trace/run/attempt/session。
- `docs/operations/runbook.md` 固化 backup、migration、恢复、接管和 image rollback。
- 本地全量门禁与部署环境任务执行完成；后续 Compose smoke 和接管演练作为每次发布的运维门禁。

## 6. 文档与方向收口（2026-07-07）

- 正式标记基础执行闭环完成。
- 新增 `docs/README.md` 文档索引和 `docs/design/skill-interpretation.md`。
- `docs/08` 从当前实施主线调整为 JAF migration/regression/benchmark profile。
- 下一组工作重点调整为 Skills Interpretation；i18n、真实 Provider、调度和 JAF 扩展后移。
- 下一组工作重点必须使用 Generic Native、Generic Adapted、Unsafe Assisted、JAF Regression 四类 fixture，不能只用 JAF 证明通用性。

## 7. 历史验证快照

基础执行闭环建设期间逐步达到并保持以下门禁：

- Backend Ruff、Mypy strict、Pytest。
- Web TypeScript strict、Vitest、production build。
- JSON Schema/example、OpenAPI 和 Compose 静态校验。
- Compose 部署中的 authenticated task execution。

具体测试数量会随实现增长，不作为历史契约；当前结果以每次运行的验证输出为准。

## 8. 必须延续的决策

- PostgreSQL 是 Run 与审计正本；Redis 只用于 queue、短期 lock 和通知。
- 已发布 SkillVersion、Result、Run snapshot 和历史 Interpretation 不原地修改。
- Interpreter 只能生成候选契约，不能授予权限或直接发布。
- JAF 固有内容只能存在于普通 Skill source、fixture、acceptance/evaluation 和历史 snapshot，不得进入通用 contract 或形成 generic service 分支。
- 任意 host Shell、未受控外部 write、generated FrontendModule、调度和 JAF 其余任务需要单独更新产品与安全计划。

后续实施顺序只从 `docs/planning/roadmap.md` 的当前 active section（现为 §15）恢复；历史状态文件不再作为规划正本。

### 8.1 通用 Skills Interpretation 交付摘要（2026-07-07 至 2026-07-13）

- 冻结 Interpreter request/response/report/diagnostic 契约、静态分析、capability catalog snapshot 与
  versioned system Skill；危险来源在 model 前 fail closed。
- 实现 SDK 无关 Interpreter、Claude structured completion transport、不可变 execution identity、
  append-only adjustment/lineage/diff、Draft/publish gate、Worker + SSE 执行与 Web Preview。
- 实现通用 TaskCatalog、`POST /task-runs`、Run snapshot inline Schema、task-agnostic context builder、
  Workspace task discovery 与通用 Result 基线；JAF 固定入口在迁移期保留。
- 完成真实 PostgreSQL 唯一约束、lineage FK、并发幂等和 Project isolation 验证；模型×3、Compose
  非 JAF E2E 与 JAF smoke 留作后续动态 Task Contract S6 的发布验收。
- 2026-07-13 稳定化 model structured output：完整 generation Schema、Schema/prompt checksum、默认
  identity 复用、显式 regeneration nonce、一次脱敏修复和 fail-closed 错误分类。

上述交付证明了 Interpretation/Execution 骨架，但业务 input/output 仍依赖预定义 Schema path；
`docs/01` §14 接续完成动态 TaskContractDraft、inline freeze、通用 UI 和旧业务资产删除。

## 9. 文件与文档管理（2026-07-09 至 2026-07-10）

原 `projectmind-file-management-plan.md` 的五层（F1/A1/B1/B2/B3）全部完成后归档至此。

### 9.1 已定决策

- Blob 存储后端为对象存储 MinIO/S3（复用 `object_storage_*` 设置）；离线测试用 `InMemoryFileStorage`。
- Skill 上传采用多文件/目录选择（浏览器 `webkitdirectory`，保留相对路径）。
- 文档管理器 = 存储 + 专用 UI，并同时接入为 `document.read/v1` 数据源（文件管理工作包的首个内部 Provider）。

### 9.2 分层交付

1. **F1 存储底座**：`storage/` 模块（`FileStorage` protocol、`S3FileStorage`(minio)、`InMemoryFileStorage`、`StoredBlob`、`UploadLimits`、`sanitize_object_key`）；settings 增 `document_max_bytes`/`project_document_quota_bytes`/`document_allowed_content_types`；上传面 size/type/配额/机密扫描 fail-closed（机密扫描复用 `core/redaction`）。
2. **A1 Skill 目录上传**：`POST /projects/{id}/skill-imports/upload`（multipart，ADMIN+CSRF）→ 临时目录（路径安全）→ 既有 `SkillPackageParser` → import 管线；binary bundle 落对象存储且 `storage_uri=s3://…`（内联 `database://` 源零改动）；SkillsPage 目录选择器。
3. **B1 文档存储**：`ProjectDocument` model + migration `0014`（uq project+folder+name）+ `DocumentRepository`/`DocumentService`；upload/list/content/delete 四个 endpoint（Project actor+CSRF，越权/不存在折叠 404）；契约 `documents/v1`。
4. **B2 文档管理器 UI**：独立「项目文档」页面（route `documents`；UX 复盘后从 WorkspacePage 迁出，Workspace 观测区改为 tab）；`api/documents.ts` + barrel；样式沿用暗色/青色语言。
5. **B3 文档即数据源**：capability catalog 增 `document.read/v1`（checksum 重算并同步固定断言）；`contracts/tools/document.read/v1` 契约组；`manifest_gate.REGISTERED_CAPABILITIES` 同步；`DocumentProvider` 经 `ProjectDocumentSource` port（本番 `DatabaseProjectDocumentSource`）；`context_builder` 抽出 fixture 定义并新增 `create_run_tool_registry`。

### 9.3 验证快照与残余

- 完成时门禁：backend 351 passed / ruff / mypy strict；web tsc 0 错误 / vitest 78 / build；contracts 45 schemas + 37 examples；compose 7 services；OpenAPI 回写（LF）。
- Compose 残余：真实 MinIO put/get round-trip 与配额、真实 DB 并发/隔离/唯一约束、上传→文档→task-run 读取文档的实机端到端。
- 延后决策：被 Run 引用文档的软删/保留策略（当前为硬删）；病毒扫描引擎接入（已声明超出该工作包范围）。

## 10. 动态 Task Contract 实施记录（2026-07-14）

`docs/01 §14` 的 Release A–D 代码迁移已完成到本地可验证边界：

- 新增受限 `TaskContractDraft` 元契约与确定性 compiler；相同 draft 稳定生成相同 Draft 2020-12
  Schema 和 canonical checksum，字段数、深度、enum、description 与 pattern 均有硬限制。
- Interpreter system Skill 升级到 2.1.0，model 只生成 contract draft/source trace；平台编译、绑定
  identity，并对 schema/checksum drift fail closed。信息不足的 source 保持无业务字段的 Assisted
  Preview；enum/required/compatibility/selection/workflow 使用通用确定规则，非法 JSON、空响应和截断
  候选只允许一次不回显正文的完整重生成。
- SkillVersion、TaskCatalog、Run snapshot、Worker 与 Result identity 全部使用 inline generated Schema
  和 checksum；新运行不再从 image contract root 回退读取业务 Schema。
- Web 标准表单和 Result renderer 按 generated Schema 工作；执行历史和 Agent 流摘要也不再猜测
  ticket/issue 等业务字段；删除 JAF 专用创建入口、result interpreter/renderer 和预定义
  JAF/repository-review 业务 Schema。
- JAF、repository-review、insufficient-contract、unsafe-shell 作为 `skills/examples/` 下普通输入；
  migration `0016` 将内置 JAF seed 标记为 `DEPRECATED`，历史审计快照不改写且不可重新执行。
- Smoke 改为从 TaskCatalog 选择 published task，并通过环境变量提供 Schema 合法输入与 source 选择。

真实模型门禁：JAF 与 repository-review 固定输入各三次均为结构有效率 3/3、contract signature 1、
runtime binding signature 1、总语义 signature 1；JAF 一次、repository-review 先前一次非法候选均由
受控重生成恢复，说明性 review item 数差异不改变可执行契约。

本地门禁：Ruff、Mypy strict、临时 Redis 支撑的完整非 PostgreSQL Backend/API/OpenAPI 测试、
41 schemas + 34 examples、Web 119 tests + production build、SDK probe 和 8-service Compose 静态检查通过。

部署验收残余：本环境没有 PostgreSQL server 与 Docker CLI，尚需在目标环境执行真实 PostgreSQL
`upgrade head`/0016 历史快照和并发不变量，以及 authenticated generic task smoke；两类无 Schema
Skill 的 import→interpret→adjust→draft→publish→run→result/evidence 以该部署 smoke 作为最终证据。

## 11. 能力蓝图与交互式 Runtime 方向收敛（2026-07-16）

在动态 Task Contract 完成本地迁移后，重新评审 `docs/` 与 `plans/`，确认下一组工作重点不再以业务 Schema
和固定 workflow 为 Skill 解释中心：

- Interpreter 的主产物调整为 CapabilityBlueprint，明确能力、目标、资源前提、Skill guidance、交付物、
  用户交互点和 observe/propose/apply 效果意图；task-specific Schema 降为可选派生产物。
- 领域 capability 与 Tool capability 分离；新业务能力无需预先登记到 Tool catalog，只有实际资源访问才
  要求版本化平台注册 handler。
- Agent 默认采用 SUPERVISED profile，在冻结资源和权限内自主规划；平台继续控制 Secret、Tool scope、
  workspace/sandbox、预算、审计和终态。
- Run 调整为持续目标线程：用户澄清、选择、Review 或批准追加 RunSegment，Worker 故障/接管追加
  RunAttempt；同一非终态 Run 可以顺序 resume/fork/replace 多个 AgentSession。
- 外部写入采用 ChangeProposal、默认批准、可选低风险预授权、幂等执行、optimistic concurrency 和
  read-back Evidence；对应 Provider 和门禁落地前生产路径仍禁止直接写入。
- `docs/01` 新增 §15 作为唯一 active implementation plan；`docs/04/05/06/07/08/10/11`、两张 HTML
  架构图和 AGENTS 按该方向同步。

原 `dynamic-task-contract-state.json` 的 S1–S5 完成内容和 S6 部署验收残余已经进入本文件 §10 与
`docs/01` §14，因此删除该 state。`plans/` 此后只保留本 delivery history，不再维护 active state。

## 12. CapabilityBlueprint 双读兼容（2026-07-16）

`docs/01` §15 S1 的第一个发布分段完成到本地可验证边界；旧 RuntimeManifest 执行路径未改动。

- 冻结通用 `contracts/capability-blueprint/v1.schema.json`（identity、capabilities、tasks、
  resource_requirements、guidance 四分类、interaction_points、effect_intents、execution_preferences、
  source_traces、assumptions/questions）与 example。契约不含 JAF/repository-review 业务字段，
  task-specific Schema 降为可选 `parameter_contract` / `result_contract`。
- `skills/capability_blueprint.py` 提供正规化 + 决定性校验 + canonical checksum：apply 意图必须绑定
  write 资源并保持 ask 承认、required rule 必须有 source trace、任意 contract draft 必须可编译；
  未知领域 capability 与缺少业务 Schema 都不构成 hard gate。
- `skills/blueprint_projection.py` 把旧 Manifest 投影为「一个能力集合、一个隐式资源集合」的兼容视图：
  data_sources/tools → read 资源要求，workflow → 建议步骤（不产生 required rule），permission 上限
  read-only → 不生成 apply/propose 意图，并以 assumption 标记自身为投影产物。
- 蓝图经 `ParseSkillResponse.capability_blueprint` 读取时投影公开（不落库、不改 DB/领域 DTO），
  Web Preview 新增能力/目标/资源/规则/交付物/效果面板，并声明「效果意图不是权限」。

本地门禁：ruff 全仓库全绿、mypy strict 104 files、backend 363 passed、web 128 tests + tsc + build、
42 schemas + 35 examples、8-service Compose、OpenAPI 回写（LF）。

顺带修复：`ops/dynamic_contract_acceptance.py` 中先于本工作包存在的一处 E501（把 subscript 内的
`_required_string` 提取为局部变量），行为不变，`_task_from_catalog` 的资源绑定结果经实测未变。

残余：依赖 PostgreSQL/Redis 的 67 个测试因本机只有 psql 客户端、缺 server 未执行（全部为 setup
ERROR，非回归）。

## 13. Interpreter 原生产出 CapabilityBlueprint（2026-07-16）

蓝图从「投影产物」升级为 Interpreter 的主产物；旧解释继续走 §12 的兼容投影，形成真正的双读。

- 按 `docs/05` §9 / `docs/04` §5.1，蓝图冻结在 RuntimeManifest 内（`capability_blueprint`），因此随
  既有 manifest JSON 列持久化，**不需要 migration**。公开 manifest 契约只把它约束为任意 object 以
  保持旧解释读取兼容；完整形状与决定性规则由 `capability-blueprint/v1` 与 validator 施加。
- system Skill 升级 2.1.0 → 2.2.0：蓝图列为首要产物，并写入 docs/11 §5.2 的确定性规则（领域
  capability 可新建、required rule 必须带 source trace、apply 必须绑定 write 资源且保持 ask、
  recommended_profile 无依据时不填）。identity/prompt checksum 随之更新，fixture 同步。
- 生成 Schema 对 model 必填蓝图，但剥离 `identity`/`compatibility`——两者由 platform 从 manifest
  权威值束缚（`_bind_blueprint_identity`），避免模型复刻出与 manifest 不一致的 source_hash/互换级别。
- publish gate 在蓝图存在时以专用 validator 检查（`capability_blueprint:*` finding），缺失不算违规；
  读取面 `_parse_response` 优先返回原生蓝图，无蓝图才投影。
- identity 在「model 解释」与「DRAFT 冻结」两处确定，因此两处都经 `bind_blueprint_identity`
  统一改写。此前只有 manifest 侧在冻结时被重新 stamp，蓝图会保留解释期的
  `interpretation_id` 而随已发布 version 固化成互相矛盾的审计值。

本地门禁：ruff 全绿、mypy strict 104 files、skills 142 tests、web 128 tests + tsc、42 schemas +
35 examples、OpenAPI 回写。

残余：真实模型是否稳定产出合规蓝图，需要按 `docs/11` §12 的 fixture 反复执行验收（本机无
ANTHROPIC 凭据与 PostgreSQL，无法执行）。

## 14. 资源要求匹配与就绪度（2026-07-16）

`docs/01` §15 S2 的判定核心已落地为纯确定性层，尚未接入 TaskCatalog/API/Web。

- `skills/resource_binding.py`：`evaluate_blueprint_readiness()` 从蓝图 resource_requirements +
  Project 候选资源 + 平台已注册 capability，算出逐项 `AVAILABLE/UNAVAILABLE/UNSUPPORTED`
  与整体 `GUIDANCE_ONLY / CONFIGURATION_REQUIRED / RUNNABLE / ACTIONABLE`。
- 关键规格取舍：`accepted_providers` 按 `docs/05` §4.2 只作**排序偏好**，不作过滤——满足 kind 与
  capability 的新 Provider 不被排除。就绪度按 `docs/05` §7.2 判定「资源与 Tool 是否可得」，不要求
  预先选定具体实例（实例选择留给 Task 配置/preflight/运行中 CHOICE）。
- 未注册 Tool capability 的必需资源 → `GUIDANCE_ONLY`（补配置也无法安全自动执行），区别于
  `CONFIGURATION_REQUIRED`（可绑定但尚未绑定）。有 apply 意图但无已注册 write Provider 时保持
  `RUNNABLE`（可分析、可生成提案，不可写入），符合 `docs/04` §3.3。
- `documents/resource_catalog.py`：`ProjectResourceCatalog` port 的首个实现，把 Project 文档列为
  `document` 候选。**当前仓库没有 Integration model**（`ToolCall.integration_id` 只是可空审计列），
  因此 repository/issue 候选要等 Integration 落地后才能参与绑定。

本地验证：10 个新测试 + ruff + mypy strict（106 files）。并以真实 `capability-blueprint.v1.json`
实测同一 SkillVersion 在无资源/有仓库/仓库+文档三个 Project 下分别得到 CONFIGURATION_REQUIRED /
RUNNABLE / RUNNABLE，且逐项给出不足原因——即 S2 判据。

### 14.1 接入 TaskCatalog / API / Web（同日）

- `resolve_capability_blueprint()` 收敛「原生蓝图优先、旧解释投影兜底」的解析，Preview 与
  TaskCatalog 共用同一实现。`PublishedTaskDescriptor` 携带解析后的蓝图与 `readiness` 槽位。
- 就绪度在 **service** 层计算（投影层没有 Project 资源），并复用 `ManifestValidator` 的已注册
  capability 集合作为单一正本——两处分别读取会造出「能发布却永远待配置」的矛盾。
- `GET /projects/{id}/tasks` 增加 `readiness{level, requirements[]}`，逐项返回 status、reason、
  capability_hints、selection_guidance 与候选资源（不含 Secret/连接信息）。catalog 未配线时返回
  `null`，不把「未判定」误报成「不可执行」。
- Web `TaskReadinessPanel` 在 Workspace 逐项展示状态与不足原因，并区分「补配置可解决」与
  「平台未支持该能力」。
- `api/main.py` 注入 `DocumentResourceCatalog`。

本地门禁：ruff 全绿、mypy strict 106 files、backend 385 passed、web 131 tests + tsc + build、
42 schemas + 35 examples、OpenAPI 回写。

### 14.2 修复：蓝图必须开示 manifest 实际读取的资源（同日）

把蓝图升为「利用者审阅的主产物」后出现一个新缺口：Run 实际读取的资源由 manifest 的
`data_sources`/`tools` 决定，而 Preview 展示的是蓝图。实测确认蓝图可以把 `resource_requirements`
清空、manifest 仍声明 `repository.read/v1`，publish gate 照样通过——即可以发行「读取未开示资源」
的版本，且就绪度显示为 RUNNABLE 而非 CONFIGURATION_REQUIRED。

- publish gate 新增 `capability_blueprint:resource_not_disclosed`：manifest 声明的每个
  capability 必须出现在某个资源要求的 `capability_hints` 中。
- 只做单向检查。反向（蓝图有、manifest 无）是 Tool 未对接的情形，由就绪度降级表达，不是 hard deny。
- 无蓝图的旧解释豁免：其蓝图由投影从 data_sources 派生，按构造即一致。
- system Skill 同步加入该规则（规则 8 与 output-contract），让模型在生成期就对齐而非发行期才被拒。

残余：Project 默认 / Task 配置 / Run preflight 三级绑定的**持久化**未开始（当前只算就绪度，不冻结
选中的 provider/revision/scope）；`ResourceBinding`（`docs/04` §5.2）与 Integration model 均未建立，
因此 repository/issue 要求在任何项目都显示 UNAVAILABLE。Integration 涉及 SecretReference 与
`docs/09` 的凭据决策，应作为独立工作包规划。

另注：`runs/service.py::_resolve_selected_sources` 仍把 manifest 的 `accepted_providers` 当作硬过滤
（基础闭环执行边界），而蓝图侧按 `docs/05` §4.2 只作排序提示。两者分属不同层，当前不冲突；待 Run 级
ResourceBinding 落地时需要一并对齐语义。

## 15. AgentTaskBrief 与 ExecutionProfile（2026-07-17）

`docs/01` §15 S3 的「Brief builder、checksum、ExecutionProfile 合并和 Worker 注入」已落地。选择先做
S3 而非补齐 S2 剩余，理由是 §14.2 已把三级绑定持久化判为被 Integration model 阻塞；而实测确认
`agent/context_builder.py` 的 prompt 只由 manifest 的 capability title + 已解析 tools + output schema
组成，蓝图的 objective / required_rules / quality_criteria / prohibited_actions **完全没有进入 Worker**
——这正是 §15 目标「把 Skill 中的能力完整传递给 Agent」的核心缺口，且不被 Integration 阻塞。

- 冻结通用 `contracts/agent-task-brief/v1.schema.json` 与 example：identity（run/segment/skill_version 与
  manifest/blueprint/result schema 三个 checksum）、objective、guidance 四分类、execution、resources、
  allowed_tools、effect_policy、deliverables、limits。契约不含 JAF/repository-review 业务字段。
- `agent/task_brief.py`：`build_agent_task_brief()` 从已冻结 snapshot 决定性组装 Brief 并算 canonical
  checksum；`render_task_brief_prompt()` 把 Brief 描画为 prompt。必须规则、禁止事项、品质基准、停止
  条件**不摘要、不省略**地全量下发（docs/11 §7.1）。
- `ExecutionProfilePolicy`：平台默认 SUPERVISED；Skill 推荐更保守（GUIDED）时采纳；推荐 DELEGATED 时
  按 `PLATFORM_MAXIMUM_PROFILE` 降级为 SUPERVISED 并把由来记为 `platform_maximum`。依据是 docs/11
  §7.2「DELEGATED 只在 ADMIN 预授权资源使用」，预授权属 S5，未实现前不能让来源文档自行抬高自律度。
- effect intent 与 permission 的分离在 Brief 里显式化：`declared_intents[].executable` 只有 observe 为
  true，prompt 明写「declaring an intent is not an approval」，要求以提案形式写入结果而非尝试写入。
- 蓝图与 manifest 的 task key 一致性**未被 publish gate 强制**，故 Brief 先按 key、再按 capability 回退
  匹配；两者都落空时退回 manifest 的 capability title 目标。guidance 位于蓝图顶层，任何情况下不丢失。
- Worker 增加 `run.execution.brief` 审计 log（仅 checksum + profile）。`core/logging.py` 的
  `_CONTEXT_FIELDS` 是**静默丢弃**式 allowlist，故同步登记两个字段并加测试固定，避免审计点无声消失。

本地门禁：ruff 全绿、mypy strict 107 files、391 passed（24 秒，见下方残余）、43 schemas + 36 examples。

设计取舍：Brief 未落库。docs/11 §8.1 把「AgentTaskBrief snapshot」排在 S4 的 migration 顺序里，且
docs/04 §5.2 的 `AgentInstructionSnapshot` 以 `run_segment_id` 为键——RunSegment 尚不存在。Brief 由
决定性函数从不可变 snapshot 导出（同 Run 同 Attempt 必得同一 checksum，已有测试固定），因此当前实现以
checksum 审计即可，持久化随 S4 的 segment 表一并落地，避免先写进错误的列再返工。

残余：
- S3 尚缺两项：ResultValidator 的通用 OutcomeEnvelope（需要先允许 Run 不带 task output schema，牵动
  Run 创建、基础闭环结果形状与 Web renderer，属独立工作包）；以及按能力开放 workspace read/search。
- `tests/db` 与 `tests/api` 共 67 个 setup ERROR（与 §12 同数，非回归）：本机只有 PostgreSQL 客户端，
  无 server 二进制，`redis-server` 未安装。**注意慢的不是 DB**（连接拒绝 2ms）：`api/main.py` lifespan 的
  arq `create_pool` 按 `conn_retries=5 × conn_retry_delay=1s` 死等，使每个用 `client` fixture 的 api 用例
  固定耗时 5.3 秒，全量约 5分47秒。本机验证请用 `--ignore=tests/db --ignore=tests/api`。
- 真实模型是否在 SUPERVISED 下既遵守 required rule 又不被过度约束，需按 docs/11 §12 反复执行验收
  （本机无 ANTHROPIC 凭据，无法执行）。

## 16. 示例 Skill 日文化与解释输出语言跟随源语言（2026-07-17）

`skills/examples/` 的 4 个示例 Skill 由英文改为日文后，暴露出解释结果的人类可读字段仍为英文。定位
结论：**与模型能力无关，是 prompt 引导缺失**——system Skill 与 `references/output-contract.md` 全文
不含任何语言指示（实测 grep 只命中 "natural-language intent" 与代码围栏语言标记），且 system Skill
本身通篇英文，把模型带向英文；而日文正文确实经 `normalized_package.instructions` 送达模型（实测
payload 内含日文）。

- 示例 Skill 日文化：4 个 `SKILL.md` 正文与 `description`、`repository-review/agents/openai.yaml` 转为
  日文。frontmatter `name:` 保持英文——`_skill_key()` 的 `_KEY_CLEANUP = [^a-z0-9.-]+` 会吃掉日文并
  退化成 `imported-<hash>`；capability ID、Schema key、enum 值、`Bash`/`Write` 同理保持英文。
- 安全检测未退化（逐个实测）：`unsafe-shell` 仍 blocked 且触发 `hard_denied_builtin_tool` +
  `shell_commands_require_manual_mapping`；`repository-review` 仍 `blocked=False`、`commands: []`
  （日文散文未误触发命令检测）；`insufficient-contract` 仍无 tools → assisted。
- 内容变更连带 checksum 同步：`repository-review` 的 `source_hash`/静态分析 `checksum` 被
  `skill-static-analysis`、`skill-interpreter-request`、`skill-interpreter-response` 三个 example 冻结
  引用（测试强制），已用生产代码重算并同步。
- system Skill 升级 2.2.0 → 2.3.0：新增 Required behavior 规则 9「人类可读字段使用 Skill 源自身的
  语言，标识符恒为小写 ASCII」，`output-contract.md` 同步一条字段规则。选择「跟随源语言」而非固定
  日文，因为写死 locale 等于把语境固化进通用 platform，与 §15「不预定义业务规则」冲突。identity
  `source_hash`/`prompt_checksum` 随之变化，已同步 request/response example 与两处测试断言。
- docs/11 §5.1 增加该语言规则，并明确标注它由模型执行：确定性校验只强制标识符 pattern，不校验自然
  语言字段的语种，保真度只能按 §12 反复验收观测。为此新增 prompt 守卫测试
  （`test_system_skill_instructs_output_language_to_follow_the_source`），并实测「删掉规则 9 即失败」，
  避免该指示被静默删除后无人察觉。

`contracts/examples/capability-blueprint.v1.json` 的 `interpreter_version` 保持 2.2.0：它记录「哪个
解释器产出了该示例」，本机无模型无法重新生成，改写即伪造溯源值。同目录 `generic-native-manifest` /
`generated-task-manifest` 至今仍为 2.1.0，属同一惯例。

顺带修复：`contracts/fixtures/jaf-m0` 已由用户有意删除（旧 JAF 描述无需保留），而
`tests/agent/test_runtime_context.py` 仍指向该路径——生产代码 `context_builder.py` 早已改用
`fixtures/generic-read-providers`，故将两处测试引用一并对齐到该 fixture。

本地门禁：ruff 全绿、mypy strict 107 files、392 passed、43 schemas + 36 examples，README 的 fixture
runner 对日文 Skill 实跑 exit=0。

残余：规则 9 的实际保真度（日文 Skill 是否稳定产出日文蓝图、`key` 是否仍恒为 ASCII）需在有
ANTHROPIC 凭据的环境按 docs/11 §12 反复执行确认；本机无法验证。

## 17. 移除旧解释兼容投影，收敛到蓝图唯一来源（2026-07-17）

正式发布前不存在需要兼容的历史解释，故提前收回 §12 建立的双读。蓝图改为**只由 Interpreter 产出**。

- 删除 `skills/blueprint_projection.py` 与 `tests/skills/test_blueprint_projection.py`。
  `bind_blueprint_identity()` 与 `resolve_capability_blueprint()` 并入 `skills/capability_blueprint.py`
  ——前者是 identity 绑定、与投影无关，必须保留；模块名 `blueprint_projection` 在投影消失后即失实。
- `resolve_capability_blueprint()` 不再兜底投影，无蓝图返回 `None`。publish gate 新增
  `capability_blueprint_missing` 硬拒；§14.2 的「无蓝图旧解释豁免开示检查」随之消失（该豁免本就是
  `blueprint is None → return` 的结构性副作用）。Worker 侧 `build_agent_task_brief()` 读不到蓝图即
  fail closed，不退化成空 guidance。
- **范围校正**：投影有两个消费者，只有一个是「旧解释」。`POST /skills/parse` 的导入期
  deterministic draft 天然无蓝图，此前靠投影临时编造（capability 为 `skill.<key>.assist`、objective
  为机械句式）。按 docs/11 §4「蓝图是 CapabilityInterpreter 的产物」，改为返回 `null` 表示「尚未
  解释」，不做机械补全。`ParseSkillResponse.capability_blueprint` 转为 nullable（公开契约变更，
  OpenAPI 已回写，实测行尾仍为纯 LF）。
- 确认发行路径不受影响：`create_version_draft` 只接受 PREVIEW_READY 的 interpretation，而 model 的
  生成 Schema 自 2.2.0 起强制 `capability_blueprint`，故确定性 draft 到不了发行；"assisted" 是模型
  给出的兼容级别，模型仍会同时产出蓝图。
- Web：`SkillParseResult` / `InterpretationPreview` 的 `capability_blueprint` 转 nullable。新增
  `parseOptionalBlueprint()` **区分「未解释的 null」与「壊れた蓝图」**——两者都畳成 null 会让
  Interpreter 的缺陷在 UI 上显得无害。对应测试改为两条：null 视为未解释、malformed 仍拒绝。
- fixture 全量对齐新不变量：`test_manifest_gate` / `test_task_catalog` / `test_skill_repository` /
  `test_runtime_context` 的 manifest 补蓝图；`generic-native-manifest` 与 `generated-task-manifest`
  两个 example 补蓝图并升到 2.3.0（它们代表「可发行的 manifest」，缺蓝图即自相矛盾）。

顺带修正 fixture 失真：`test_runtime_context` 的 `task_snapshot` 缺 `task_key`（生产
`runs/service.py` 必写），导致 Brief 按 key 匹配不到蓝图 task 而静默退回 manifest 标题。补上后
暴露一个建模事实：manifest 的 capability 若写成 `generic.repository.review/v1`，与蓝图 `key` 的
pattern（`^[a-z][a-z0-9_.-]*$`，禁 `/`）**永远匹配不上**；真实 manifest（如 `generic-native-manifest`）
用的是不带版本的领域 capability。fixture 已改为后者。

本地门禁：ruff 全绿、mypy strict 106 files、375 passed、43 schemas + 36 examples、
web tsc + 132 tests + vite build 全绿、OpenAPI 回写（LF 保持）。

残余：`contracts/examples/capability-blueprint.v1.json` 的 `interpreter_version` 保持 2.2.0（记录
产出该示例的解释器，无模型不可重新生成，改写即伪造溯源）。

## 18. §16 Skill 库作用域的领域与契约收敛（2026-07-17）

起因是使用中报「导入 Skills 后解析结果 Not Found」。定位为前端缺护栏（URL 出现
`projects//skill-imports/upload`，空 project_id 匹配不到 route，落到 FastAPI 默认 404 →
`code: http_error` + `detail: "Not Found"`，与领域 404 的 `skill_interpretation_not_found` 可由
`code` 区分）。`SkillsPage` 是唯一没有 `!projectId` 护栏的页面，已按 `ResourcesPage` 的写法补齐。

但用户追问「SkillsPage 不该归属某个项目」引出一处**既有的规格自相矛盾**，遂立为独立工作包 §16：

- `docs/05` §7.2、`docs/01` §15 S2、`docs/11` §13 S2 的门禁都要求「同一 SkillVersion 在不同项目得到
  不同就绪度」，但 `SkillSource.project_id` 使 SkillVersion 经 source 唯一归属一个 Project，该语义
  不可能成立。S2 交付时以纯函数单测（假 catalog）验证就绪度判据，未经 DB，故矛盾一直未暴露。
- 数据模型本就是混合作用域：`Skill`/`SkillVersion`/`RuntimeManifest` 无 project_id（已全局），
  `SkillComposition` 是 organization_id + `ProjectComposition` 启用关系（现成先例），项目隔离只靠
  `SkillSource.project_id` 一根锚，下游 12 处经回溯 join 继承。

T1（本次，只动规格，不动代码）：

- `docs/04`：§2 从隔离边界定义中移出 Skill，新增「Skill 资产归 Organization + 三条启用规则」；
  §3.1/§4.4/§5.1/§6 增加 `ProjectSkillVersion` 实体、关系、字段字典与权限行；`SkillSource` 字段
  字典改为 `organization_id` + `(organization_id, content_hash)` 唯一。
- `docs/05` §7.2：把就绪度语义与 Organization 作用域挂钩，明确未启用版本不参与判定。
- `docs/11` 新增 §6.0：固定「先可见、后就绪」的分界——`ProjectSkillVersion` 管资产可见性，
  `ResourceBinding` 管资源可得性，不合并为同一张表（否则「管理员未授予能力」与「能力已授予但资源
  没配」显示成同一状态，用户不知该配资源还是该申请启用）。
- `docs/01`：新增 §16（目标、已定决策、现状表、作用域工作包与门禁、非目标）；§13 状态表加一行。

用户已定：**启用粒度为 SkillVersion**（Run 冻结精确版本、平台不自动切换 PUBLISHED，启用到 Skill
会让新版本发布静默改变项目可执行内容）、**默认不启用**（扩大执行面必须是显式 ADMIN 操作）。

全文档扫描确认门禁达成：无任何正本再主张「SkillVersion 归单个 Project」（`docs/04:28` 与
`docs/01:674` 的命中是描述该矛盾本身，非主张）。两张对客 HTML 结构图**无需修改**——其隔离边界列的
是「成员/数据源/文档/任务/执行/结果」，Skill 本就不在其中，且已写明「项目执行永远锁定到精确版本」
「解释结果不直接进入项目」，与新设计一致。

本地门禁：43 schemas + 36 examples、8-service Compose、375 passed（未动代码，确认无副作用）。

残余：T2（migration，`skill_sources.project_id` → `organization_id` + `project_skill_versions`）、
T3（API 分层）、T4（Web）未开始。T2 的迁移门禁为「迁移前后各 Project 的 TaskCatalog 内容一致」，
需要真实 PostgreSQL 验证，本机无 server 无法执行。

## 19. §16 Skill 库作用域的数据模型扩张（2026-07-17）

### 19.1 先修正了自己写错的工作包顺序

原 §16.4 把「`project_id` → `organization_id` + 换唯一约束」放在 T2、「作用域改由
`project_skill_versions` 强制」放在 T3。实测发现二者不可分：`SkillSource.project_id` 除
`skills/repository.py` 的 12 处强制点外，还经 `skills/domain.py` 的 **5 个 DTO** 投影到公开 API
response 与 Web——删列必然同时波及强制点、DTO、API 与前端。

据此把 §16.4 改写为 expand/contract：**T2 纯增量、零行为变更**；T3 才切换强制点、重划 API 并收缩
旧列。这样把「本机无法验证的 DDL」压到最小：T2 的 migration 没有 drop、没有唯一约束变更。

### 19.2 本次交付

- `db/models.py`：`SkillSource` 增加 `organization_id`（FK→organizations、非空、索引），保留
  `project_id` 并注明其为移行期的旧 anchor；新增 `ProjectSkillVersion`（`project_id`,
  `skill_version_id`, `enabled_by`, `enabled_at`, `disabled_at`，unique(project_id, skill_version_id)，
  双 FK）。停用不删行而是立 `disabled_at`——「谁在何时扩大了执行面、又在何时收回」是监查事实。
- `migrations/0017_skill_library_organization_scope.py`（纯增量）：
  1. 加 `organization_id`（先 nullable）→ 从 `projects.organization_id` 回填 → 显式探测残留 NULL
     并 `RAISE EXCEPTION` → 才收紧 NOT NULL。既有行存在时无法直接加 NOT NULL 列，且回填不到的行
     必须响亮失败而不是让 NOT NULL 抛出难以定位的错误。
  2. 建 `project_skill_versions`。
  3. **播种**：为每个既有 PUBLISHED 版本，向其 source 当时所属的 Project 写入启用记录。这一步是
     「迁移前后可执行任务集合不变」门禁的实体——T3 切换强制点的瞬间若无播种，全 Project 的
     TaskCatalog 会变空。`enabled_by` 取 source 的 `imported_by`：捏造一个默认 ADMIN 等于把从未
     发生的授权行为写成监查事实。
- `skills/repository.py`：`save_preview` 建 source 时经新增的 `_organization_of()` 从所属 Project
  解析 organization，Project 不存在则 fail closed（否则会留下归属不明、T3 后任何 Organization 都
  看不见的行）。作用域强制点**未改动**，仍在 `project_id` 侧。

`gen_random_uuid()` 为本仓库首次使用（PostgreSQL 17.9 内置，无需 pgcrypto）。0010 的 seed 用固定
UUID 常量，但那是已知单行；此处要为数量未知的 (project, version) 对各生成一行，且该表的真实身份是
`(project_id, skill_version_id)` 唯一约束，代理主键随机即可。

### 19.3 测试替身的连带修正

三个 fake session 的 `scalars` 此前无条件返回「未注册」，新增的 Project 解析落进来后被判为
「Project 不存在」。按各自形态修正：`test_skill_upload` / `test_skill_service` 增加按
`column_descriptions` 判定 Project select 的分派与 `_ValueResult`；`test_skill_repository` 的
`side_effect` 序列插入 organization 解析（顺序为 source→organization→interpretation）。新增
`test_save_preview_rejects_a_source_whose_project_does_not_exist` 固定 fail closed。

本地门禁：ruff 全绿、mypy strict 106 files、376 passed、43 schemas + 36 examples、8-service Compose、
`alembic history` 修订链解析正确（0016 → 0017 head）、models 与 migration 的列/约束/FK 已离线互校。

**残余（本机不可验证，需你在 Compose 环境确认）**：migration 使用 PostgreSQL 专有语法
（`UPDATE...FROM`、`DO $$`、`gen_random_uuid()`、`ON CONFLICT ON CONSTRAINT`），SQLite 无法代跑；
本机只有 psql 客户端、无 server。真实 DDL、回填、播种结果与 downgrade 均未执行过。T3 的门禁
「迁移前后各 Project 的 TaskCatalog 内容一致」依赖本次播种的正确性。

## 20. §16 作用域切换、收缩迁移与 Web 管理闭环（2026-07-18）

### 20.1 强制点与 lifecycle

- SkillSource、Interpretation、Skill identity、Version lifecycle 全部切到 Organization；`Skill` 增加
  `organization_id`，唯一 identity 收紧为 `(organization_id, key)`，避免同 key 的跨组织耦合。
- TaskCatalog、Composition 绑定与新 Run 的精确版解算只接受 active、仍为 PUBLISHED 的
  `ProjectSkillVersion`。未启用、停用、废弃、跨组织与不存在在执行读取侧统一 fail closed；停用不改写
  已有 RunSkillSnapshot、Result 或审计。
- 导入、upload、interpret/adjust/SSE、Draft、version list/detail、publish/deprecate 均改为
  Organization endpoint；Project 只保留 enable/list/disable 与 task discovery。动态 Contract
  acceptance 也改为「发布 → PUT 精确版启用 → TaskCatalog → Run」，不再依赖隐式默认可见性。
- Interpreter execution key 加入 Organization scope salt，阻断跨组织同 content/model 请求共享
  Redis job ID 或 channel identity。

### 20.2 0018 contract migration

- 收缩前显式探测同 Organization/content 的重复 source、跨 Organization 的 Skill identity、以及
  未发布/cross-Organization enablement；任何冲突都 `RAISE EXCEPTION`，不静默合并被
  Interpretation/Version/Run 引用的 identity。
- 回填并收紧 `skills.organization_id`，删除旧 `skill_sources.project_id` 与旧唯一约束，建立
  `(organization_id, content_hash)`；Alembic revision ID 同时缩短到 varchar(32) 内并由测试固定。
- Downgrade 只有在每个 source 仍能唯一映射一个 Project 且 Skill key 无跨 Organization 重复时才允许；
  否则要求恢复配备前 backup。运维 Runbook 已记录这一不可逆边界。

### 20.3 Web 与公开契约

- SkillsPage 的 Organization library 在未选择 Project 时仍可导入、解释、创建 Draft、发布、列举和
  废弃；选择 Project 后显示精确版 active/disabled 状态并提供 enable/disable 操作。任务发现仍保持
  Project-scoped。
- `SkillVersion` 公开契约加入 `organization_id`；新增 Organization version list 与 Project
  enablement detail/list Schema/example，并同时注册到脚本和 Backend contract test。OpenAPI、Web
  validator/client 与 API fake 同步。

本地门禁：Ruff、Mypy strict 106 files、Skills/Worker/Alembic/contract/ops test 批次全绿；
46 schemas + 39 examples；Web typecheck、140 tests 全绿；OpenAPI 已从 FastAPI 重新导出。

环境残余：当前容器没有 PostgreSQL/Redis service，因此 0017→0018 的真实 DDL、播种前后
TaskCatalog 等价性、新增 enable/disable DB invariant 与完整 lifespan API suite 留待 Compose 验收；
代码侧已加入真实 PostgreSQL scratch database 回归，环境具备服务时会走全 migration 链执行。

## 21. §15 OutcomeEnvelope 与隔离 Workspace read/search（2026-07-18）

### 21.1 通用结果最低边界

- 新增 `projectmind.outcome-envelope/v1` contract/example，固定 summary、status、deliverables、findings、
  Evidence/Artifact/Proposal 引用、未决问题、限制、confidence 与 effect 摘要。Runtime 由平台合成
  Outcome Schema；Interpreter 2.4.0 只在来源明确给出稳定机器消费字段时生成可选 output draft，并将其
  约束到 `structured_data`，开放式报告不再被迫虚构业务 Schema。
- ResultValidator 分层验证包络、可选业务 Schema、敏感 field 与同 Run Evidence；0019 不改写历史
  `data_json`，把既有结果标为 `STRUCTURED_OUTPUT`，新结果保存为 `OUTCOME_ENVELOPE`，同时冻结引用和
  optional Schema identity。Run detail contract、OpenAPI、Web validator 与标准 Outcome renderer 同步。

### 21.2 受控 workspace 能力

- 新增注册能力 `workspace.read/v1` 与 `workspace.search/v1`，只访问当前 Run 的 `input/`、`workspace/`。
  Provider 拒绝绝对/上跳路径、symlink、非 UTF-8 与 root 逃逸，限制单 file 1 MiB、搜索 500 files / 10
  MiB / 100 results；0 件搜索也只保存 query hash 的 Evidence，不把查询正文复制进审计 metadata。
- Tool 仍经 Manifest、permission snapshot、ExecutionProfile、PreToolUse、request/response Schema 与
  Evidence transaction。`workspace.search/v1` 最低为 SUPERVISED；旧 snapshot 没有 profile 时不得后加
  workspace 权限。SDK 内置 Read/Glob/Grep/Bash/Write/Edit/Web 未开放。

本地门禁：53 schemas + 44 examples；Outcome/Interpreter/Manifest/Workspace/Tool/Run 单元批次、Ruff、
Mypy strict（108 files）与 OpenAPI 同步检查通过；Web typecheck、141 tests 与 production build 通过。
完整 Backend suite 在 93 tests 通过后进入 lifespan test，并因当前容器没有 Redis service 而停止；
因此 0019 真实 DDL、PostgreSQL invariant 与完整 lifespan API 留待 Compose 验收。

## 22. §15 RunSegment、UserInteraction 与顺序多 Session（2026-07-18）

### 22.1 持久化与状态机

- 0020 新增 `run_segments`、`agent_task_brief_snapshots`、`user_interactions`、
  `interaction_responses`，并把 Attempt/AgentSession 绑定到 Segment。历史 Run 不按猜测回填，detail
  只读投影 implicit Segment 1；Release G 后的新 Run 必须显式创建 Segment 1。
- Run 增加 WAITING_FOR_INPUT/WAITING_FOR_APPROVAL；用户等待时 Attempt 记为 DEFERRED、Session 记为
  IDLE、lease 清空，wall timeout 不再计时。回答完成旧 Segment、追加新 Segment 和 dispatch；技术恢复
  仍只在原 Segment 增加 Attempt。所有 Worker transaction 保持 Run → Segment → Attempt lock 顺序。
- Session 保存 parent、continuation mode、checkpoint checksum 与 engine options checksum；PostgreSQL
  partial unique index 阻断同一 Run 的多个 ACTIVE Session。恢复路径先关闭遗留 ACTIVE Session，再允许
  同 Segment 新 Attempt 接管。

### 22.2 Agent、API 与 Workspace

- 新增注册平台能力 `interaction.request/v1`。PreToolUse 先验证 request Schema，再返回 SDK `defer`；
  Provider 本体 fail closed，避免绕开 Worker transaction。AgentTaskBrief 冻结交互点、公开 checkpoint 与
  用户回答，Worker 按 INITIAL/RESUME/FORK/REPLACE 选择 engine operation。
- 公开 response 经 Project actor、Origin/CSRF、interaction version、expiry、类型与 idempotency 校验；
  CLARIFICATION/CHOICE/REVIEW 走追加式 response，EFFECT_APPROVAL 在 S5 Proposal endpoint 就绪前继续
  拒绝。Segment/Interaction/checkpoint/session lineage event 已加入 SSE contract 与 Web parser。
- Run detail 同步返回 Segment、Attempt、Session 与 Interaction；Workspace 右侧详情显示 lineage、期限、
  推荐选项和已答内容，并可直接回答三种输入交互。等待中仍可取消，回答后重新接入 SSE 并继续原 Run。

### 22.3 门禁与环境残余

新增执行器门禁完整覆盖“Session A → Review → fork Session B → Result”，仓储测试固定 suspend 时释放
lease、response 创建新 Segment、event sequence 与 dispatch；Interaction 纯函数测试固定敏感内容、选择
范围、multiplicity、approval 分流与 fingerprint。0020 增加真实 PostgreSQL constraint 回归，并通过
`0019_outcome_envelope:0020_interactive_run --sql` 离线生成。

本地门禁：58 schemas + 47 examples；Ruff、Mypy strict（109 files）、Alembic head、OpenAPI 与 S4
Backend 离线批次通过；Web strict typecheck、143 tests 与 production build 通过。当前容器没有
PostgreSQL/Redis，故 0020 实机 upgrade/constraint test、完整 lifespan API suite 与 Compose
“Session A → 用户回答 → Session B”网络级 smoke 留待配备服务后的发布验收。

## 23. ResourceBinding 与受控外部效果（2026-07-18）

### 23.1 持久资源边界

- 0021 增加 SecretReference、Integration，以及 Project default / Task / immutable Run 三级
  ResourceBinding；Run snapshot 固定 provider、capability version、Integration revision、scope、来源
  binding 与 canonical checksum。
- Provider catalog 只登记 Redmine `issue.read/v1` / `issue.update/v1` 和 Git/SVN
  `repository.read/v1`。写能力必须有明确 issue/field allowlist，未知/禁用/跨 Project/越界配置 fail closed。
- ADMIN API 与 Web 资源中心管理 locator metadata、Integration、Binding 和 policy；response 不返回 Secret
  locator、connection config 正文或 credential。TaskCatalog 合并文档与 Integration candidate 计算 readiness，
  Run Tool 按精确 `binding_id` 解析，避免同类多绑定歧义。

### 23.2 Proposal、批准与 Provider

- `change.propose/v1` 只产生无权限 candidate；仓储重新验证 Blueprint effect intent、Run binding checksum、
  capability/operation/scope、Evidence 所有权、版本/checksum、期限和敏感内容。Git/SVN 第一版继续只在
  Outcome 中生成 patch/commit proposal，不开放 commit。
- System Interpreter Skill 升级到 2.6.1；prompt 明确区分“repository proposal 作为 Outcome deliverable”
  与“有 apply intent 时调用 change.propose”。source/prompt checksum 同步冻结为
  `sha256:071c22f5c7707c7742365fca0ca94416c028b5077228f707f211dc5006798b99`。
- 默认创建 EFFECT_APPROVAL。User decision 绑定精确 Proposal version/checksum/idempotency，初版只允许 Run
  发起人或 system ADMIN；LOW-only preauthorization 还必须精确匹配 Redmine `issue.update/v1`、Integration、
  `update_fields`、scope 与有效期。
- Redmine write 不直接调用 stock issue endpoint。Provider 先验证
  `projectmind.redmine-effect/v1` discovery 对 revision 原子 CAS 与 idempotency key 的声明，再执行 pre-read、
  adapter apply 和 read-back；成功保存 before/after Evidence。目标 revision 漂移为 STALE，transport retry
  保持同一 EffectExecution/幂等键，verification failure 作为独立人工处理状态。
- 普通 Interaction、approval 和 Effect lease 都进入 durable recovery。普通超时创建
  INTERACTION_TIMEOUT Segment 且不推断默认回答；approval 超时把 Proposal 置为 STALE；技术 retry 不新增
  Segment。Result 持久化并在 API/Web 展示 `change_proposal_refs`、Approval 和 Effect verification。

### 23.3 本地门禁与迁移验收残余

- Contract：67 schemas + 53 examples；Provider discovery/apply request/response、Tool request/response/error、
  Run event/detail 和 Result 引用均同步验证。
- Backend：Ruff 全绿、Mypy strict 127 source files；排除真实 PostgreSQL invariant 的 537 个测试全绿，
  其中 69 个 API contract test 使用 in-memory queue/service，不再误依赖 Redis。0020→0021 offline SQL、
  Alembic head/model metadata 与 OpenAPI 均纳入回归。
- Web：strict typecheck 与 147 tests 全绿；资源管理、精确批准、敏感 response 拒绝、Proposal/effect/read-back
  详情已有 contract/component 测试。

当前执行环境没有 PostgreSQL、Redis、Docker/Compose daemon 和真实模型 credential，因此 0021 实机
upgrade/constraint、部署 CAS adapter 联调、网络级
`import → interpret → bind → run → interaction → effect/result`、固定 fixture 至少三次模型反复和人工质量
对比仍属于 `docs/01` §15 S6。上述环境残余不能用本地 mock 结果冒充完成，也不创建新的 active plans
state 文件。（2026-07-18 后记：`.env` 已补配模型 credential，模型反复测量转入 §25 先行执行；
PostgreSQL/Compose 相关残余不变。）

## 24. 工程维护交付：Review、重构与文档整备（2026-07-18）

在 S6 等待部署环境期间完成一轮全量 Review 与重构，外部可见行为零变化，全部验证与重构前基线一致
（后端 528 passed + 9 skipped、Ruff/Mypy strict 全绿、Web 147 tests、契约 67 schemas + 53 examples）。

### 24.1 结构重构

- `runs/repository.py`（4996 行）按关注点拆为 4 模块：`repository_base.py`（`_RunRepositoryBase`：
  `_lock_run_row` / `_lock_segment_row` 行锁顺序、`_next_sequence` 采番、RUN_SNAPSHOT/Outbox 生成、
  AgentSession 管理）、`repository_interactions.py`（`InteractionOperationsMixin`）、
  `repository_effects.py`（`EffectOperationsMixin`）、`repository.py`（`RunRepository` 组合 +
  `OutboxRepository`）。对外导入面不变，AGENTS「共通実装の利用規約」同步更新定义位置。
- Run → Segment 行锁获取序列收敛为具名单点，替换 13 处手写（transition/cancel/claim/heartbeat/
  respond/decide/effect claim/finalize/recovery），各站点异常语义逐字保留。
- Web：`http.ts` 失败 body 解析收敛为 `throwProblemFromBody`；新增 `parseItemList` 应用于 9 处
  list 契约验证（错误消息逐字不变），消除 2 处双重 cast。

### 24.2 测试健壮性与工具加固

- `test_claude_engine.py` 的环境变量精确断言测试密封化：先清空全部 `_AGENT_ENVIRONMENT_KEYS` 再
  设定输入，避免父进程（如 Claude Code 会话）注入的 allowlist 变量造成假失败。
- `test_real_database_invariants.py` 在 PostgreSQL 不可达时由 9 个 setup ERROR 改为带理由 skip
  （仅捕获 OSError；连接可达后的失败仍 fail），README 同步记载。
- `scripts/export_openapi.py` 增加 argparse 防护：任意未知参数（含此前的 `--help`）不再触发契约
  文件覆写。

### 24.3 文档整备

- 新增 `contracts/README.md`（布局 + example 双表登记义务 + OpenAPI 回写规则）与 `web/README.md`
  （分层与边界规约索引），至此全部主要子目录具备日文 README。

## 25. 模型反复测量的首次真实数据（2026-07-18，未过门禁）

`.env` 补配模型 credential 后，在本机对 `jaf-ticket-quality` 与 `repository-review` 执行
`scripts/measure_skill_interpretations.py --repeats 3`（默认 300 秒/候选）。结果未达
`structural_valid_rate=1.0` + `semantically_stable=true` 技术门禁，但明确了失败模式：

- 300 秒轮（完整）：jaf 0/3 全部 `model_timeout`；repository-review 1/3（成功件零修复、
  11 field paths、3 review items；其余 2 件 timeout）。
- 900 秒重测（应操作者要求中止，仅完成 1 件）：300 秒下三连超时的 jaf 候选 1 转为 valid
  （1 次修复）。

结论：当前自定义 endpoint 的大 prompt 时延普遍超过 300 秒，两件跑完的解释在结构上均有效，
未观测到 Schema 质量问题。门禁判定仍属 S6 残余；后续在时间允许时以
`--timeout-seconds 900` 重跑完整 3 次反复（约 30–90 分钟），或先降低 endpoint 时延再测。
本条只记录事实数据，不以部分结果冒充门禁通过。

## 26. §17 界面三语化：偏好持久化、i18n 基建与壳层交付（2026-07-19）

S6 部署项挂起期间，按 `docs/01` §17（计划先行于 2026-07-18 落档）交付首个工作包：

- Backend：`users.ui_language`（nullable，check 约束 `zh|ja|en`，0022 migration）；AuthService 新增
  `get_ui_language` / `set_ui_language`（服务层复验允许集合）；`GET/PUT /users/me/ui-language` 以
  独立 "users" tag 进入 OpenAPI；API 测试覆盖认证、CSRF、422 值域与 nullable 往返。
- Web：`src/lib/i18n/messages.ts` 以 `Record<UiLanguage, ShellMessages>` 型检查保证 zh/ja/en 键
  齐全；`resolve.ts` 固定「保存值 → 浏览器语言 → `zh`」；`src/i18n.tsx` provider 缺省 `zh`，
  存量中文断言测试全部不变绿。`routing.ts` 去除硬编码 label/description，App shell、
  AppNavigation、LoginPage、HomePage 全部经 catalog 取文案；sidebar footer 新增语言切换器，
  保存失败复用共享 error 栏；未资源化画面在非 zh 下回退 zh。
- 验证：backend 全量 pytest（含新 API 测试）、Ruff、Mypy strict、OpenAPI 契约 dict 相等、
  web typecheck + 157 tests + build 全绿。浏览器画面级三语目视验收与实机 0022 migration
  属部署环境残余；U2（业务画面）、U3（api/lib 层文案与三语校对）为后续工作包。

## 27. §17 业务画面文案资源化（2026-07-19）

同日完成 U2 工作包：11 个画面/component 文件（SkillsPage、WorkspacePage、ProjectsPage、
ResourcesPage、DocumentsPage、RunResultPanel、DocumentManagerPanel、AgentConversation、
RunHistoryPanel、SchemaTaskInput、PageElements）的全部 user-facing 文案迁入语言目录。

- 语言目录按语言拆分为 `lib/i18n/zh.ts / ja.ts / en.ts`（约 300 键 × 三语），接口更名
  `UiMessages`，`Record<UiLanguage, UiMessages>` 型检查继续保证键齐全。带参数文案使用函数键，
  连接词与模板差异随语言封装；纯函数 helper（`parseObject`、`summarizeDiff`、`connectionLabel`）
  改为显式接收文案参数，不在纯逻辑里隐式依赖 React context。
- 迁移采用「断言替换」脚本：每条替换先 assert 原文存在再执行，避免静默漏迁；`zh` 目录逐字
  保留原文案，U2 完成门禁（主要画面无硬编码中文、`zh` 表示与现状一致）由残留扫描与存量
  157 个 Web 测试（含中文断言，provider 缺省 `zh`）共同确认。
- Backend 本轮零改动（U1 的 531 passed + 9 skipped 基线继续有效）；web typecheck、157 tests、
  vite build 全绿。剩余 U3：`lib/projectResources.ts` 等 lib 层与 `api/` 层的用户可见展示文案、
  三语人工校对与 `docs/07` §16-8 三语基础检查。

## 28. §17 api/lib 层收尾与三语校对（2026-07-19）

- 严格残留扫描（含跨行模板字符串、排除日文注释）确认 `api/` 层无用户可见中文——此前统计的
  中文出现数实为日文 docstring 中的汉字；契约错误消息（`did not match its contract` 等）按
  §17.2 决定保持英文。
- `lib/projectResources.ts` 经消费方检索确认为零引用死代码（S2 交付真实 SecretReference/
  Integration/ResourceBinding API 后遗留的 localStorage 暂定实现），连同 `tests/lib/
  projectResources.test.ts` 一并删除（Web 测试 157 → 153）。
- 补充 `<html lang>` 随界面语言同步（zh-CN/ja/en，读屏发音正确性）；ja 目录标点统一
  （`deprecateConfirm` 全角 `？` → 半角，zh 副本按「现状一致」门禁保持原文）。机器校对
  （空值、en 目录 CJK 泄漏、全角标点混用）通过。
- 验证：web typecheck、153 tests、vite build 全绿；backend 零改动。§17 剩余为母语者人工校对
  与 `docs/07` §16-8 浏览器级三语/键盘验收，与 S6 部署验收同批执行。

## 29. 已完成工作包的计划正文归档

`docs/01` は「方針 + 現在の状態 + 進行中/今後の計画」に絞る。完了した工作包の计划正文（目标・决策・工作包・门禁）は信息を失わないよう本节へ移設した。以降の各項は移設時点の原文であり、追記のみとする。

### §29.1 docs/01 §14：动态 Task Contract 与业务 Schema 去预定义化（计划正文）

> 2026-07-15：S1–S5 已完成本地实现与验证；S6 的真实 PostgreSQL migration 和部署 Compose
> authenticated smoke 作为发布验收残余保留。以下内容作为迁移设计与交付依据，不再是当前主线。

#### 14.1 目标

平台只预定义可跨 Skill 复用的协议和 Schema 元模型，不预定义 JAF、repository-review 或
未来业务任务的字段。原始 Codex/Claude Code Skill 可以只有 `SKILL.md`、references 和可选 Agent
metadata，不要求作者提前提供 ProjectMind 专用 input/output Schema。

Task 仍必须具有可验证的输入输出边界，但该边界改为 Interpretation 的派生产物：

```text
通用 SkillSource（无业务 Schema）
  → Interpreter 理解任务语义
  → 生成 TaskContractDraft
  → 平台确定性编译并校验 JSON Schema
  → 用户预览、调整或强制重新生成
  → SkillVersion 冻结生成 Schema、checksum 和生成依据
  → Run 冻结精确 Task Contract
  → Web 表单、Agent 输出验证和 Result 展示使用同一快照
```

本工作包必须区分两类契约：

| 契约类型 | 归属 | 是否平台预定义 |
| --- | --- | --- |
| API、Problem、SSE、Tool、RuntimeManifest、TaskContractDraft 元模型 | Platform | 是 |
| JAF Ticket、repository review 等具体输入输出字段 | Interpretation/SkillVersion | 否，按 Skill 动态生成 |

生成后的 Schema 可以是任务特定的，但不能作为平台源码中的预定义业务规则，也不能要求原始
Skill 提前适配 ProjectMind。

#### 14.2 目标数据形态

Interpreter 使用平台定义的通用 `TaskContractDraft` 表达字段、类型、必填性、说明、枚举、数组和
嵌套对象。平台使用确定性 compiler 将 Draft 转换为 Draft 2020-12 JSON Schema。这样 model 输出
仍受结构化输出约束，同时避免让 model 任意拼接不可控的 JSON Schema keyword。

RuntimeManifest 的每个 Task 冻结：

- `input_contract`：Interpreter 生成的通用 contract draft。
- `output_contract`：Interpreter 生成的通用 contract draft。
- 编译后的 input/output JSON Schema。
- 两份 Schema 的 canonical checksum。
- Interpreter version、Interpretation ID 和 source trace。

第一版直接保存在不可变 RuntimeManifest JSON 中，不新增业务数据库表。Run 创建时把编译后的
Schema 正文和 checksum 复制到 `task_snapshot_json`；Worker 不再为新 Run 从 image 内
`contracts/` 查找业务 Schema。

通用 Contract 必须限制最大字段数、最大嵌套深度、enum 数量、description 长度和 pattern
复杂度；禁止远程 `$ref`、外部 URI、可执行表达式和来源 Skill 通过 Schema 扩大 Tool/网络权限。

#### 14.3 实施工作包

#### 规格与规划收敛

1. 将当前实施顺序收敛到本文件，停止维护重复的 current-plan 文档；交付历史仅保留必要的记录
   交付记录，完成态状态文件归档后删除。
2. 更新 `docs/05`：原始 Skill 无业务 Schema 前置要求，Schema 是 Interpretation 派生产物。
3. 更新 `docs/07`：Workspace 消费 SkillVersion 冻结 Schema，不要求 ViewSpec 或源文件 Schema。
4. 更新 `docs/08`：JAF 是普通验收 Skill，不再以预定义字段 Contract 约束平台。
5. 更新 `docs/11`：Interpreter 必须生成 TaskContractDraft，而不是生成指向预置业务文件的路径。

完成门禁：正式规格中不再出现“新增通用 Skill 必须先把业务 Schema 放入平台 contracts root”
的要求。

#### 通用 TaskContractDraft 与确定性 compiler

1. 新增 versioned `TaskContractDraft` 元 Schema，只描述通用字段模型，不包含 Ticket、repository、
   JAF 等业务词汇。
2. 实现纯函数 compiler，将相同 Draft 稳定转换为相同 JSON Schema 和 SHA-256。
3. RuntimeManifest 在迁移期同时接受 legacy `$ref` 和 generated contract；新 Interpretation 只生成
   generated contract。
4. 对 compiler 增加深度、数量、字符串长度、非法 key、外部引用和不支持类型测试。
5. 将 generated Schema 的验证失败归类为可调整的 Interpretation diagnostic；只有无法形成有效
   输入输出边界时才阻止可执行发布。

完成门禁：不读取任何业务 Schema 文件即可把一个 TaskContractDraft 编译、校验并重复得到同一
checksum。

#### Interpreter 生成与调整闭环

1. 升级 `projectmind-skill-interpreter`，从 Skill 说明提取 task、字段语义和结果结构，输出
   TaskContractDraft 与 source trace，不再要求来源包含 schema/view/fixture path。
2. 删除 `missing:schema_view_assets`、`contract_ref_missing` 等“来源未携带业务 Schema”诊断；
   仅保留无法推断最低输入输出边界、冲突类型和安全风险诊断。
3. Interpreter response contract 增加 generated contract drafts；confidence、assumption、question
   继续作为可选说明。
4. Web Preview 展示生成字段、类型、必填性和结果结构，允许用户通过追加式 adjustment 修改后
   重新解释；不自动重新解释既有 SkillVersion。
5. 固定输入至少重复解释三次，记录结构有效率、字段语义一致性和人工调整量。

完成门禁：只有 `SKILL.md` 的 JAF 和 repository-review source 都能生成 PREVIEW_READY，不需要
上传任何 JSON Schema 文件。

#### SkillVersion、TaskCatalog 与 Run 全链冻结

1. DRAFT 创建时确定性编译 contract draft，并把 compiled Schema、checksum 和 lineage 固定进
   RuntimeManifest；publish 不依赖 SkillSource 中的业务 JSON 文件。
2. `TaskCatalog` 从精确 PUBLISHED SkillVersion 投影 inline Schema 与 checksum，不再只返回文件
   reference。
3. 通用 Run 创建使用投影 Schema 校验输入，并始终把 Schema 正文冻结进 Run snapshot。
4. `ProductionRunContextBuilder` 对新 Run 只读取 snapshot Schema；删除业务 contract root fallback。
5. Result 保存稳定 Schema checksum/identity，不使用 `jaf/.../output.schema.json` 这类路径决定行为。
6. 保持 source hash、manifest checksum、Run snapshot 和 Result 不可变，不新增“读取最新 Schema”
   的隐式路径。

完成门禁：删除运行容器内的 JAF/repository-review Schema 文件后，新生成 SkillVersion 仍可创建
并完成 Run。

#### 通用 Workspace 与 Result 解释

1. 标准输入表单直接读取 TaskCatalog 的 generated Schema；暂不支持的复杂字段降级为 JSON
   editor，不增加业务组件分支。
2. 标准结果视图按 generated output Schema 渲染 object、array、scalar、Evidence 和 raw JSON。
3. 删除通过 `schema_ref` 选择 JAF 专用 result interpreter/renderer 的机制；业务展示差异未来只能
   由可选 ViewSpec 或注册的通用 component capability 表达。
4. Web/API parser 验证 Schema object 和 checksum，不依赖已知业务路径字符串。

完成门禁：JAF 与 repository-review 使用同一表单、同一 Result renderer 和同一 Run API，平台代码
中没有按 task key、业务 Schema path 或业务字段名分支。

#### 删除预定义业务 Schema 与旧默认入口

完成 S1–S4 的双读迁移后删除下列当前规则资产：

- `contracts/jaf/ticket-analyze/**`。
- `contracts/skills/repository-review/**`。
- `contracts/fixtures/skills/repository-review/schemas/**`。
- JAF input/output/view 和 repository-review input/output/view 的 contract examples 与 example 映射。
- Generic Native fixture 中对 repository-review 预定义 Schema path 的依赖。
- `jaf_result_interpreter.py` 及按 JAF output schema path 注册的验证/渲染分支。
- 新运行路径中的 JAF 固定 Run request、固定 task resolver 和专用 Workspace 分支。

`contracts/fixtures/skills/repository-review/SKILL.md` 与 Agent metadata 移到
`skills/examples/repository-review/`，作为“无业务 Schema 的通用 Skill 样例”；`contracts/` 不再
混放 Skill source fixture。JAF 若继续作为验收场景，必须以普通 SkillSource 导入并通过
Interpreter 生成 Task Contract，不再由平台内置字段规则驱动。

历史 Alembic migration、已发布 SkillVersion 和已创建 Run snapshot 不原地改写。它们属于审计
事实，不再作为新 Task 的规则来源。迁移时先停止创建 legacy JAF Run、排空或终止非终态 legacy
Run，再将内置 JAF seed 从 TaskCatalog 的活动版本中退役；历史结果继续只读展示。

完成门禁：repository 扫描不存在活动代码或正式 contract 对上述业务 Schema path 的引用；新数据库
安装结束后也不会暴露内置 JAF 任务。

#### 迁移验收与收尾

1. 增加两个无 Schema source：JAF、repository-review；两者都走
   `import → interpret → adjust → draft → publish → task discovery → run → result/evidence`。
2. 增加一个字段信息不足的通用 Skill，验证它进入可调整 Assisted Preview，而不是伪造业务字段。
3. 验证非法 generated contract、未知 Tool、Shell、外部 write 和来源 hash 不一致仍为 hard error。
4. 验证 historical terminal JAF Run 可读取、Evidence 可定位、Result/Evaluation 不变；不支持历史
   legacy Run 重新执行。
5. 执行 Backend、Web、contract、OpenAPI、migration、真实 PostgreSQL 和 Compose smoke。
6. 删除迁移期 legacy `$ref` 双读代码和过渡测试，更新交付历史。

完成门禁：平台 `contracts/` 只剩通用协议与元 Schema；新增 Skill 无需修改平台源码或提前编写
ProjectMind 业务 Schema；JAF 和 repository-review 均只是普通 Skill 验收输入。

#### 14.4 推荐发布顺序

为避免一次删除破坏现有服务器，按以下顺序部署：

1. **Release A — 双读**：加入 TaskContractDraft、compiler、inline Schema 和 legacy `$ref` 兼容。
2. **Release B — 新写入切换**：Interpreter 和 DRAFT 只生成 inline contract；TaskCatalog/Run 只为新
   版本写入 frozen Schema。
3. **数据迁移窗口**：用户主动重新解释仍需使用的 Skill；停止 legacy JAF 新 Run，确认无非终态
   legacy Run。
4. **Release C — 去业务规则**：退役内置 JAF seed，删除预定义 JAF/repository-review Schema、专用
   result interpreter 和 legacy 创建入口。
5. **Release D — 清理**：移除 legacy `$ref` reader 和仅为迁移存在的 warning/test。

任何 Release 都不自动重新解释或原地修改 PUBLISHED SkillVersion；新 contract 只通过新
Interpretation 和新 SkillVersion 生效。

#### 14.5 非目标

- 不生成后端 service 或业务数据库表。
- 不允许任意 Shell、外部 write 或来源 Skill 自行授权。
- 不在本工作包实现 generated FrontendModule、调度或多 Agent workflow。
- 不把 AI 生成的 Schema 当作可信输入；确定性 compiler、限制和 publish gate 始终生效。

### §29.2 docs/01 §16：Skill 库提升到 Organization 与项目启用关系（计划正文）

#### 16.1 目标

修复一处既有的规格自相矛盾，而不是新增功能。

`docs/05` §7.2、本文件 §15.3 S2 与 `docs/11` §13 S2 的完成门禁都要求「同一 SkillVersion 在不同项目
按资源配置得到不同就绪度」。但 `SkillSource.project_id` 使 SkillVersion 经 source 唯一归属一个
Project，同一版本不可能出现在两个项目里，该语义无法成立。S2 交付时以纯函数单测（假 catalog）验证
了就绪度判据，未经 DB，因此矛盾一直未暴露。

本工作包把 Skill 资产提升到 Organization，并引入显式启用关系：

```text
ADMIN 在 Organization 导入 → 解释 → 发布 SkillVersion（一次）
  → ADMIN 为 Project 显式启用精确 PUBLISHED 版本
  → 该 Project 的 TaskCatalog 才发现它
  → 就绪度按该 Project 的资源独立计算
  → Run 冻结精确版本（不变）
```

#### 16.2 已定决策

- **启用粒度为 SkillVersion**，不是 Skill。Run 冻结精确版本，平台不自动切换 PUBLISHED 版本；若启用
  到 Skill，新版本发布会静默改变项目的可执行内容。
- **默认不启用**。新发布的版本不会让任何项目突然多出可执行任务；扩大执行面必须是显式 ADMIN 操作。
- 启用关系**不授予权限**。能否执行仍由 Project 资源、Tool policy 与 Run 权限快照决定。领域实体
  `ProjectSkillVersion` 定义在 `docs/04` §3.1/§5.1，与 `docs/11` §6.0 的「先可见、后就绪」分界配套。
- 复用既有模式：`SkillComposition`（organization_id）+ `ProjectComposition`（启用关系）已跑通同一
  结构，不引入第二套作用域机制。
- 停用只影响新 Run 的发现与创建；既有 Run、snapshot、Result 与审计不受影响，也不被改写。

#### 16.3 已完成的作用域结构

T3 前的数据模型是混合作用域，项目隔离只靠 `SkillSource.project_id`。0017 expand 与 0018 contract
完成后，当前结构为：

| 对象 | 当前作用域与强制点 |
| --- | --- |
| `SkillSource` | `organization_id` 非空 + `unique(organization_id, content_hash)`；旧 `project_id` 已删除 |
| `SkillInterpretation` / `SkillVersion` / `RuntimeManifest` | 经 source/skill identity 归属 Organization；不携带 Project anchor |
| `Skill` | `organization_id` 非空 + `unique(organization_id, key)`，避免跨组织 identity 耦合 |
| `ProjectSkillVersion` | Project 对精确 PUBLISHED 版的显式启用；`disabled_at` 关闭新发现而不改历史 Run |
| `SkillComposition` | `organization_id`（已是组织级） |
| `ProjectComposition` | `project_id` 启用关系（先例） |

Organization 管导入、解释、Draft、发布和废弃；Project 的 TaskCatalog、Composition 与 Run 创建只接受
active 且仍为 PUBLISHED 的 `ProjectSkillVersion`。未启用、已停用、已废弃、跨 Organization 与不存在
均在执行读取侧 fail closed。

#### 16.4 实施工作包

##### 领域与契约收敛（已完成）

- 更新 `docs/04` §2/§3.1/§4.4/§5.1/§6 与 `docs/05` §7.2，冻结「Skill 资产归 Organization、Project
  显式启用精确版本」的边界。
- 明确与 §15 S2 的分界：本工作包决定「哪些 SkillVersion 对该 Project 可见」；ResourceBinding
  决定「可见的版本在该 Project 能否就绪」。两者都是项目侧绑定，但前者是资产可见性，后者是资源可得性，
  不合并为同一张表。

完成门禁：正式文档不再同时主张「SkillVersion 归单个 Project」和「同一 SkillVersion 跨项目就绪度」。

##### 数据模型扩张（纯增量，已完成）

`skill_sources.project_id` 的移除不能与读取它的代码分开：除 `skills/repository.py` 的 12 处作用域
强制点外，它还经 `skills/domain.py` 的 5 个 DTO 投影到公开 API response 与 Web。因此按
expand/contract 拆分，T2 只做可加项且不改变任何行为，把无法本机验证的 DDL 风险降到最小。

- `skill_sources` 增加 `organization_id`（FK、非空、索引），从 `projects.organization_id` 回填；
  暂时保留 `project_id` 与既有唯一约束。
- 新增 `project_skill_versions`（`project_id`, `skill_version_id`, `enabled_by`, `enabled_at`,
  `disabled_at`），只允许 PUBLISHED 版本。
- Migration 为每个既有 PUBLISHED 版本，向其 source 当时所属的 Project 播种启用记录。这保证 T3
  切换强制点后，各 Project 的可执行任务集合不变。
- 作用域仍由 `SkillSource.project_id` 强制；repository 与 API 不动。

完成门禁：迁移为纯增量（无 drop、无唯一约束变更）；既有行为与各 Project 的 TaskCatalog 内容不变。

##### 切换强制点、API 与授权重新划层，并收缩旧列（已完成）

- 作用域强制从 `SkillSource.project_id` 改为 `project_skill_versions`；DTO 的 project_id 语义随之
  重新定义。
- 导入、解释、发布、废弃移到 Organization 级 endpoint，保持 ADMIN 授权。
- 新增 Project 启用/停用 endpoint 与启用列表读取。
- 收缩：删除 `skill_sources.project_id`，唯一约束改为 `(organization_id, content_hash)`。同一
  Organization 内多个 Project 曾导入同一 `content_hash` 时会冲突；migration 必须探测并 fail closed，
  不得静默合并 SkillSource——其 id 被 Interpretation/SkillVersion/Run 引用。

完成门禁：跨项目读取未启用版本与越权访问仍折叠为同一 404；未启用版本无法创建 Run；迁移前后各
Project 的 TaskCatalog 内容一致。

交付结果：Organization lifecycle endpoint 与 Project enable/list/disable endpoint 已接通；0018 在
收缩前探测同组织重复 source、跨组织/未发布 enablement 与跨组织 Skill identity，冲突时停止而不合并。
动态 Contract acceptance 在发布后显式启用精确版，再进入 TaskCatalog/Run。

##### Web 管理闭环（已完成）

- SkillsPage 去掉 projectId 依赖，成为 Organization 级 Skill 库。
- 新增「该 Project 启用哪些 SkillVersion」的管理界面。

完成门禁：未选择 Project 时 Skill 库可用；任务发现仍要求选择 Project。

交付结果：SkillsPage 的导入、解释、Draft、发布与废弃均不依赖 Project；组织版本列表始终可用，
选择 Project 后才显示精确版启用/停用关系。任务发现入口仍保持 Project-scoped。

#### 16.5 非目标

- Skill marketplace、跨 Organization 共享与自动版本升级。
- 启用关系承载权限、资源绑定或执行策略。
- USER 角色的导入/解释/发布/启用授权。

### §29.3 docs/01 §17：界面中/日/英三语资源化与语言切换（计划正文）

#### 17.1 目标

把 §12 已确认、`docs/07` §10 定为 MVP 验收前提的「中/日/英三语资源化与语言切换」落为可执行工作包：
界面文案退出源码硬编码进入按语言的资源目录，User 可保存语言偏好，`report_language` 与界面语言
继续解耦。前置条件（用户体系可保存偏好）已由 P6 认证与 `/users/me` preference 路径满足。
S6 的部署验收与本增量互不阻塞。

#### 17.2 已定决策

- 语言集合为 `zh`（既定文案）、`ja`、`en`。偏好未设定时跟随浏览器语言，无法匹配则回退 `zh`。
- 偏好保存在 `users.ui_language`（nullable，取值限 `zh|ja|en`），经 `GET/PUT /users/me/ui-language`
  读写，归属 AuthService（User 档案属性，与 Project 无关）。匿名画面（login）只跟随浏览器语言，
  不引入 Web storage。
- 未资源化的文案在非 `zh` 语言下回退显示 `zh`，不阻塞增量推进。测试与默认语言保持 `zh`，
  存量断言不变。
- 不引入第三方 i18n 依赖：资源目录与解析逻辑放 `web/src/lib/i18n/`，画面经 React context/hook
  取文案。契约错误消息（`did not match its contract` 等）是技术诊断文案，保持英文，不进入翻译范围。

#### 17.3 实施工作包

##### 偏好持久化 + i18n 基建 + 壳层三语化（本地完成，2026-07-19）

- `users.ui_language` 列 + 0022 migration + `GET/PUT /users/me/ui-language` + OpenAPI 回写 +
  Backend/Web 测试。
- `web/src/lib/i18n/`：zh/ja/en 目录、语言解析（保存偏好 → 浏览器 → `zh`）、React provider/hook。
- 导航语言切换 UI；App shell、AppNavigation、routing 标签、LoginPage、HomePage 文案三语化。

完成门禁：登录、概览与导航在三种语言下完整显示；已登录偏好跨会话保持；全部既有测试不变绿。

交付结果：`users.ui_language`（nullable，check 约束 `zh|ja|en`）由 AuthService 读写，`GET/PUT
/users/me/ui-language` 以 "users" tag 进入 OpenAPI；语言目录 `web/src/lib/i18n/messages.ts` 以
`Record<UiLanguage, ShellMessages>` 型检查保证三语键齐全，`resolve.ts` 固定「保存值 → 浏览器 →
`zh`」的解析顺序。`routing.ts` 只保留结构（route/scope），表示名迁入语言目录；provider 缺省为
`zh`，存量测试对中文文案的断言全部不变绿。切换器位于 sidebar footer，保存失败复用共享 error 栏。
浏览器画面级验收（三语切换目视）与实机 0022 migration 属部署环境残余。

##### 业务画面文案资源化（本地完成，2026-07-19）

- Skills、Workspace、Projects、Resources、Documents 各页与共享 component 的文案迁移。

完成门禁：主要画面无硬编码中文；`zh` 表示与现状一致。

交付结果：11 个画面/component 文件（SkillsPage、WorkspacePage、ProjectsPage、ResourcesPage、
DocumentsPage、RunResultPanel、DocumentManagerPanel、AgentConversation、RunHistoryPanel、
SchemaTaskInput、PageElements）全部经 catalog 取文案；语言目录按语言拆分为 `zh.ts/ja.ts/en.ts`
（接口更名 `UiMessages`），约 300 键 × 三语。带参数文案用函数键（模板差异随语言封装），纯函数
helper（`parseObject`、`summarizeDiff`、`connectionLabel` 等）改为显式接收文案参数。pages/
components 内不再有非注释中文；`zh` 目录逐字保留原文案，存量 157 个 Web 测试（含中文断言）不变绿。
`lib/`（`projectResources.ts` 等）与 `api/` 层的展示文案按计划留给 U3。

##### api/lib 层展示文案与三语验收（本地完成，2026-07-19）

- api client 与 lib 层的用户可见展示文案（状态标签、摘要用语）资源化；三语言人工校对；
  `docs/07` §16-8 键盘与三语基础检查。

完成门禁：非 `zh` 语言下无未翻译的界面文案残留（技术诊断文案除外）。

交付结果：严格残留扫描（含跨行模板字符串、排除注释）确认 api 层无用户可见中文——U2 后唯一
残留 `lib/projectResources.ts` 经消费方检索确认为 S2 真实 API 接线后的零引用死代码，连同其测试
一并删除（-4 tests）。`lib/presentation.ts` 的日期/字节格式化已随浏览器 locale。补充 `<html lang>`
随界面语言同步（读屏发音正确性）；ja 目录标点统一（1 处全角修正，zh 按「现状一致」门禁保持
原文不动）。机器校对（空值、en 目录 CJK 泄漏、标点一致性）通过。残余：三语文案的母语者人工
校对与 `docs/07` §16-8 的浏览器级键盘/三语目视验收（属部署环境残余，与 S6 同批执行）。

#### 17.4 非目标

- `report_language` 与模型输出语言（任务输出配置，另行管理）。
- 契约错误消息、结构化 log、API field 名的翻译。
- 日期/数字格式的深度地区化与右到左布局。

### §29.4 docs/01 §18：托管凭据自助与应用层信封加密（计划正文）

#### 18.1 目标

消除「新增需凭据的资源必须运维放密钥/重启 Worker」的痛点，让 ADMIN 在资源向导直接录入凭据即完成自助配置。做法是新增第三种 SecretReference resolver `MANAGED`：明文经创建端点一次，服务端用部署级主密钥（KEK）做 AES-256-GCM 信封加密，只把密文存库，Worker 用时解密。既有 `ENVIRONMENT`/`FILE` 零改动。决策与边界记录见 `docs/09` §7.2。

#### 18.2 已定决策

- **应用层信封加密，而非 Vault**。`docs/09` §7 的「不部署 Vault、不新增对外服务」不被推翻：MANAGED 全程在现有 API/Worker 进程内，只多一个 KEK 环境变量与一个加密模块。外部 secret manager 会引入对外服务与运维面，属本工作包非目标。
- **KEK 来源为环境变量 base64 keyring**。与既有 `.env` 注入一致，无外部依赖。keyring 首键为加密活动键、其余仅供解密，支撑就地轮换。
- **密文落新表 `managed_secret_material` 1:1**，不给 `secret_references` 加可空密文列——公开 response model 从类型上不可能带出密文。
- **明文永不落库/回传/进日志/Evidence/Agent**；`secret_value` 字段名由 `find_sensitive_key` 命中遮断。
- **AAD 绑定 project_id + secret_reference_id**；KEK 缺失/篡改/AAD 不一致一律 fail closed，复用现有 credential-unavailable 路径。
- **威胁模型**：防 DB/备份单独导出，不防进程级攻陷（详见 `docs/09` §7.2）。KEK 丢失 = 托管凭据不可恢复，运维须 KEK 与备份分离（`docs/10`）。

#### 18.3 实施工作包

##### 信封加密与托管 resolver（本地完成，2026-07-20）

- `core/secret_crypto.py`：AES-256-GCM cipher + KEK keyring 解析 + AAD 组装 + fail-closed 例外。
- `PROJECTMIND_MANAGED_SECRET_KEK` 设置项（`core/settings.py`）+ `.env.example` + API/Worker 注入。
- `managed_secret_material` 表 + migration（含放宽 `secret_references_resolver` CHECK 至含 `MANAGED`）；`db/models.py`、repository projection。
- `MANAGED` 分支：创建端点加可选 `secret_value`（ADMIN，明文即时加密落库）；`DeploymentSecretResolver` 解密分支；`ops/rotate_secrets.py` 轮换命令。
- 前端向导「平台托管（直接输入）」一档 + zh/ja/en 三语 + 契约/OpenAPI 同步。
- 依赖：`cryptography` 显式声明为直接依赖（此前经 `pyjwt` 间接锁定为 `49.0.0`）。

完成门禁：加密往返、AAD 越权解密失败、KEK 缺失 fail-closed、创建端点脱敏、迁移 up/down 均有测试；`pytest && ruff check . && mypy src` 与 Web 测试全绿。实机 migration 与三语目视验收属部署环境残余。

交付结果：`core/secret_crypto.py`（AES-256-GCM、`version:base64key` keyring、AAD 绑定 project+reference、坏 KEK/篡改/AAD 不一致一律 `SecretCryptoError`）；`managed_secret_material` 表 + `0023_managed_secrets` migration（含 resolver CHECK 放宽与 down 回退）；`DeploymentSecretResolver` 增 MANAGED 解密分支并复用 credential-unavailable 路径；创建端点加可选 `secret_value`（422 handler 已确认不回显值）；`ops/rotate_secrets.py` 只输出计数；API/Worker 经 `load_secret_cipher(settings.managed_secret_kek)` 注入；前端 ResourcesPage 两处向导加「平台托管（直接输入）」一档（`type="password"` 输入），zh/ja/en 三语补齐；`cryptography==49.0.0` 显式声明并正规重生成 `requirements.lock`/`dev-requirements.lock`（顺带修正 dev 锁遗漏的 `minio` 闭包）；OpenAPI 回写。后端 `pytest`（含新增 crypto/domain/resolver/API 用例，10 项实机 DB 测试因无 PG 带理由 skip，含新增托管往返+轮换）、`ruff`、`mypy strict`（132 文件）与 Web `typecheck`/185 tests/`build` 全绿。

#### 18.4 非目标

- 外部 secret manager / KMS / HSM 托管 KEK（3b）。
- 凭据的自动轮换调度（轮换是手工运维命令）。
- 明文凭据的任何读取回传端点（托管凭据只能被 Worker 解密消费，永不经 API 读出）。
- 防进程级攻陷的 at-rest 保证（见威胁模型）。

## 30. §21 解释器过程重表达：静态门、步骤重表达与能力守卫（2026-07-23 → 2026-07-24）

> **回填记录**：本节与 §31 于 2026-07-25 实施 §19 W4 时补录，来源为 `docs/01` §21/§19.3 的计划正文与
> `docs/13` 时间线，并逐条核对了当前代码。当时未留 pytest 具体计数的，本节不补写计数。

背景：`docs/11` §5.4 规定「解释器是翻译器而非校验器」，但脚本编排型 Skill（步骤写死 `svn cat` /
`curl` / `python3 x.py`，`allowed-tools` 声明 `Bash` / `Write`）在解释器执行前就被静态门整体硬挡，
`jaf-quality-ticket` 连蓝图都生成不出来——这是 §19.5 文档资源验收层的前置阻塞。

### 30.1 静态门从「因声明而拒绝」降为可重表达信号（2026-07-23）

- `skills/interpreter.py`：`hard_denied_builtin_tool`（error → `blocked` → 硬停）改为 non-blocking 诊断
  `declared_builtin_tool`；`blocked` 仅由真实不安全静态发现触发（明文 credential 物料、敏感文件外泄、
  路径逃逸），`UnsafeSkillSourceError` 只在此时抛出。
- 修补声明解析漏洞：`allowed-tools` 归一化（剥 `(...)`），使 `Bash(python3 *)` 与裸 `Bash` 同等处理。
  此前精确匹配反而让带参形式漏过硬挡，宽严颠倒。
- 依据是 `SKILL.md` 已定义的「source 声明是 untrusted evidence」：**声明 ≠ 授予**。Agent 永不获得
  bash/edit/web/write，强制点仍是 permission snapshot 与 tool registry；`external_write_policy=deny`、
  `write_capabilities=[]` 不变。松开的只是「因声明而拒绝解释」。
- 测试：新增 fixture `skills/examples/unsafe-credential`；
  `test_skill_importer.py::test_static_analysis_records_declared_builtin_tool_without_blocking` 与
  `test_generalization_acceptance.py::test_declared_builtin_tool_is_reconciled_not_blocked` 固定
  「声明 Write/Bash 可进解释」，含明文 credential 的 Skill 仍 blocked。

### 30.2 system Skill 增加「过程重表达」（2026-07-24）

- `skills/projectmind-skill-interpreter/SKILL.md` 新增 Procedural re-expression 一节，
  `references/output-contract.md` 把 `without invention` 限定到业务内容：**过程/工具机制**（`svn cat`
  → 经 `repository.read` 读）可重表达为能力调用，**业务规则/约束**（enum、整合性规则、验收条件）
  仍保真，`source_trace` 不变。
- 三档落法：① 直译（`svn cat`→`repository.read`、`curl`→`issue.read`）；② 换 idiom（`svn list | grep`
  → 物化树 + `workspace.search`，依赖 §19，未到位则降级）；③ 降级 guidance（`svn log`、xlsx 无等价
  能力）。硬失败仅当 required 核心步骤三档皆不落。
- Security boundary 增「重表达不是授权」「接续信息与 credential 不得进入解释」。
- 版本升 3.1.0，request/response fixture 的 `source_hash` / `prompt_checksum` 同步重算；**指令在 prompt
  中的在场由 `test_skill_importer.py` 固定**——模型执行的规则无法由确定性检查守住，指令掉出即静默
  失效，因此固定的是「指令存在」这件事本身。

### 30.3 确定性能力守卫（2026-07-24）

- `skills/manifest_gate.py` 的 `_check_step_capability_references` 从 guidance 文本抽出 `xxx.yyy/vN` 形的
  名指能力，校验 ① 已注册 ② 被某条 `resource_requirements` 开示；`workspace.*` / `change.propose` 等
  Run 域能力经 `_RUN_SCOPED_PLATFORM_CAPABILITIES` 豁免资源开示。
- 不通过发 warning `capability_blueprint:step_capability_unregistered` /
  `capability_blueprint:step_capability_not_disclosed`，按 D-A3 **降档不拒发**——否则又回到「对不齐即
  失败」。不名指能力的降级 guidance 不受影响。离线已由 `test_manifest_gate.py` 验证。

### 30.4 验证与残余

- 三个工作包均通过 ruff / mypy strict / pytest（当时记录只写「通过」，未留计数）。
- 残余（需重新部署）：I1 的 jaf 实机越门（`jaf-quality-ticket` 首次越过静态门、进到解释、生成蓝图）；
  I2 的模型行为观测（`svn cat`→`repository.read` 是否被重表达、业务规则是否未被发明）。判定标准见
  `docs/01` §21.5。

## 31. §19 就绪度、workspace.write 与 document 物化（2026-07-24）

> 回填记录，说明同 §30。

### 31.1 就绪度反映已安装 Provider（604 passed）

- 核验确认谎报的粒度是 **Provider 级**而非 capability 级：`repository.read/v1` 这个 capability 是装了的
  （git fixture 提供），但 `PROVIDER_DEFINITIONS` 同时声明 svn，于是用户配一个 svn 集成后就绪度仍显
  `RUNNABLE`，运行时才在 `(capability, provider)` 粒度抛 `Tool Provider is not installed`。capability 级的
  「已安装」检查对此是 no-op。
- `ProviderDefinition` 增 `installed: bool`（git/redmine=True，svn=False，待 W4 切 True）→ 派生
  `INSTALLED_PROVIDER_CAPABILITIES`（能力 → 已配线 provider 名，与候选同语汇）；就绪度新增可选注入
  `installed_provider_capabilities`，候选的 provider 必须在该能力的已装集合内才算可运行，只有 svn 的
  repository 要求因此判 `UNAVAILABLE`（`CONFIGURATION_REQUIRED`），且不再把该候选提示为可绑定。
- 注入点在 `api/main.py`（合入 document 的 `project-documents`），沿用 `registered_write_capabilities`
  的先例：从真实 Provider 定义注入，不从 Skill 自述或 catalog 造能力。未注入（offline/未配线）时保持
  旧判据，后向兼容。
- 同时核验发现 W3 的 document 一路没有绑定身份可用，记为待决 **D-W3**。

### 31.2 `workspace.write/v1`（611 passed）

- Agent 在 Run 工作区内写中间产物与报告草稿。写入面限 `workspace/` 与 `output/`，**明确排除物化的
  `input/`**——物化快照是冻结证据，可写即破坏「内容对应 binding revision、可复现」这一不变式。
- 落法与既有 read/search 同构：新增 `WorkspaceWriteProvider`（UTF-8、单文件 1 MiB 上限 fail-closed、
  `created` 标记新建/覆写）+ 写路径解析（目标可不存在，逐段拒 symlink，`os.open` 带 `O_NOFOLLOW` +
  0o600，父目录建后 resolve 校验 `is_relative_to(base)`，裸 root 拒写）。
- `contracts/tools/workspace.write/v1/{request,response,error}` 三 schema + 两 example；注册进
  `_workspace_tool_definitions`（`minimum_execution_profile=SUPERVISED`，与 search 同级）；capability
  catalog 加 `workspace.write/v1` 并同步 checksum 三处（catalog 文件、interpreter-request 内嵌、importer
  测试）；`_RUN_SCOPED_PLATFORM_CAPABILITIES` 加入以豁免资源开示；system Skill 升 3.2.0，把该能力列入
  Run 域例外（写 `workspace/`/`output/`、绝不碰 `input/`）。
- 授权路径与 read/search 完全一致（manifest 声明 + permission snapshot allowed + 执行 profile），Agent
  不因此获得任何外部写；写入产出 `workspace-write` Evidence（`read_only:false`）以保结论可追溯。

### 31.3 document 一路物化与方案确定（620 passed）

- 新增 `agent/workspace_materializer.py` 单点实现：若蓝图声明任一 `kind=document` 要求，则把该 Project
  **全部**文档物化进 `input/documents/<folder>/<name>`。新增 `ProjectDocumentInventory` 端口 +
  `DatabaseProjectDocumentInventory`（复用既有 `list_for_project` 枚举 + `ProjectDocumentSource.fetch`
  取正文，不新造取数路径）。
- 非 UTF-8 / 超 per-file 上限（1 MiB）/ 不安全路径 → 记入 `manifest.skipped`（「读不了 ≠ 不存在」）；
  总体积或件数超上限 → **fail-closed 不截断**（写前分类，一个 byte 不落）；物化 file 写后 `chmod 0o400`
  只读、`O_NOFOLLOW` + 父目录 `is_relative_to` 校验；生成
  `input/documents/.projectmind/manifest.json`；按 Run 幂等。设置项
  `PROJECTMIND_WORKSPACE_MATERIALIZE_MAX_BYTES` / `_MAX_FILES` 三处同步（core/settings、.env.example、
  worker/settings）。
- 集成测试验证核心链路：物化 → `workspace.search` 命中 → `workspace.read` 精读，全部走**既有**能力，
  不为「检索」另立 capability。
- **D-W3 决策记录**：核验 `agent/document_provider.py` 与 `_selected_source` 确认——`document.read/v1` 是
  live 按 `arguments.path` 读、仅 `project_id` 作用域，RUN 绑定对 document 只冻结 capability/provider、
  **不携带具体文档身份**，故「物化被绑定的那份文档」原本无定义。选 **(a) 全量物化**：`document.read`
  本就项目作用域可读任意文档，全量物化**不扩大也不收窄**访问边界，只补「跨文件检索」，且直接满足
  §19.5 文档资源验收层。未选 (b) per-requirement 作用域（那是把访问从项目级 narrow 到子集，属另一功能）；未选
  (c) 暂缓（文档资源验收层需要它）。代价：`requirement_key` 对 document 变虚设。

### 31.4 残余

- W2：system Skill 让模型实际使用写能力的行为需在部署上验证（离线不可验）。
- W3：实 DB / object storage 经由的文档列举需部署回归；repository 一路待 W4（已于 §32 落地）。
- W1：svn 的 `installed` 在 W4 切为 `True`（见 §32），本节记载的 `False` 是当时状态。

## 32. §19 真实 git/svn 客户端、repository 快照物化与增量能力（2026-07-25）

同日完成 §19 的最后两个工作包，合并记录：先打通「外部 repository 的到达」并落地 repository 一路物化，再在**同一条物化路径**上补三项增量。两者共享同一套边界（binding 再校验、scope 强制、上限 fail-closed 与 skip 语义），拆成两节会让这套边界在文档上看起来像两份。

### 32.1 真实 git/svn 客户端与 repository 物化

`docs/01` §19.3 W4 全量。把「外部 repository 的到达」收敛为单点实现，并完成 repository 一路物化。

- **`agent/repository_client.py`（新增）**：git/svn 的 subprocess 边界。git 是「临时目录 `--no-checkout`
  clone → `rev-parse` 定具体 commit → `ls-tree -r -z --long` 枚举 → `cat-file blob` 取正文」；svn 不建
  工作副本，是「`info --xml` 把 `HEAD` 解析成 revision 号 → `list -R --xml` 枚举（含 size）→ `cat`」。
  symlink（mode 120000）与 submodule 不取内容，作为 `skipped` 上报。
- **`agent/repository_source.py`（新增）**：Run 凍结 binding → 认证过的 session。`ScopedRepositorySession`
  在**取数之前**拒绝 scope 外路径（不是取回再丢），`select_frozen_revision` 让物化与 live 读取用同一条
  revision 规则。
- **`agent/run_binding.py`（新增）**：binding 再校验（checksum / status / provider / revision / capability）
  与 Secret 解决的单一实现。`redmine_provider.py` 一并收敛到该实现，消除 Provider 各写一份的隐患。
- **`agent/repository_provider.py`（新增）**：真实 `repository.read/v1`。返回解析后的具体 revision 与
  `content_hash`，Evidence URI 为 `git://integration/<id>/<revision>/<path>`（不含连接 URI 与凭据）。
  历史 M0 fixture 路径按「有无 Integration 绑定」分流保留，未绑定的 Run 仍读 fixture。
- **`agent/workspace_materializer.py`**：repository 一路落 `input/<requirement_key>/` + manifest（provider、
  解析后 revision、binding checksum、scope、统计、files、skipped）。先按枚举 size 判预算再取数；
  `requirement_key` 按单段目录名校验，拒 `..`、`/` 与和 `documents/` 撞名。
- **幂等强化**：按 `docs/06` §6.4 把「manifest 在即 reuse」升级为「逐文件校验 content hash 与
  `content_digest` 后 reuse，不一致 fail closed」，document 一路同享；manifest 的文件清单键统一为
  `files`。
- **配线与设置**：`worker/settings.py` 组装 client 与 source，materializer 与 Tool registry 注入同一实例；
  新增 `PROJECTMIND_REPOSITORY_COMMAND_TIMEOUT_SECONDS`（默认 120 秒）三处同步；svn 的
  `ProviderDefinition.installed` 切 `True`；backend image 增装 `git` / `subversion`。

### 32.2 真实 repository 访问的决定与理由

- **凭据不进 argv**：`/proc/<pid>/cmdline` 同 host 其他进程可读。git 经环境变量注入
  `http.extraHeader: Authorization Basic`，svn 经 `--password-from-stdin`；子进程只拿最小环境
  （PATH/HOME/LC_ALL 与工具变量），**不继承 Worker 的 DB URL 与 KEK**。
- **凭据本文按 `username:secret` 解析**：无冒号时按 token 处理并补 `x-access-token`（token 认证忽略用户
  名），password 侧可含冒号（只按首个冒号分割）。这样不必给 Integration config 加字段，既有
  SecretReference 登记路径（ENVIRONMENT/FILE/MANAGED）原样可用。
- **冻结 revision 一意化**：`revisions` 恰好 1 条即用之；为空视为「不限 revision」（UI 上是可选项，硬
  边界是 `paths`），取 Integration 的 `default_revision`；多条时仅当含默认值才取默认，否则 fail closed。
  擅自取第一条会把用户没打算冻结的 revision 写进 Run。live 读取额外接受「冻结式解析出的具体 SHA/号」，
  因为它与物化内容同一——Agent 通常从物化 manifest 得到该值。
- **URI scheme 在登记时就限制**：git 收 `http`/`https`/`file`，svn 另加 `svn`；ssh 系因平台不持有密钥而
  拒绝，URI 内嵌 `user:password` 一并拒绝（凭据只由 SecretReference 运载）。这是 W1「就绪度必须反映实际
  可执行」在 scheme 粒度上的补齐。
- **stderr 正文不回传 Agent**：其中可能混入 URI 与服务器应答，只按已知 marker 分类为
  `not_found` / 凭据被拒 / 一般失败。

### 32.3 文件索引、提交历史与表格文本化

`docs/01` §19.3 W5 全量。三项增量**均未新增 capability**，全部落在「物化那一刻多做一点」上——这正是 §19「按资源边界切，而非按动作切」的兑现。

- **文件索引**：物化时生成 `input/<key>/.projectmind/files.txt`。每行一个物化路径（变换物追注原文件），随后以 `# skipped\t<path>\t<reason>` 列出被跳过的原本。`workspace.search/v1` 只做内容检索，没有索引就无法「按文件名找文件」，`docs/11` §5.4 的 idiom 档（`svn list -R | grep <name>` → 物化树 + `workspace.search`）也就无法成立。skipped 一并入索引，使「没找到」与「读不了」可区分。
- **提交历史**：`input/<key>/.projectmind/history.txt`，`revision\tcommitted_at\tauthor\tsummary` 制表分隔。git 用 `log --max-count=N -- <paths>`，svn 按 scope 路径分别 `log --xml` 后按 revision 降序并合；**只取触及 scope 路径的提交**，binding 未授权的路径不经由历史泄漏。上限 200 条，且把「最近 N 条」写进文件首行。
- **xlsx→文本**：新增 `agent/spreadsheet_text.py`，用标准库 `zipfile` + `xml.etree` 解析 OPC：`## Sheet: <名>` 分节 + 逐行 `A1: 值 | B1: 值`。共享字符串（含 rich text run）、inline 字符串、布尔、公式缓存值均处理；单元格内换行折为一行以保持行级检索粒度。落为 `<原文件名>.txt`，原本二进制不再落盘。
- **manifest 扩展**：新增 `generated`（索引与历史，带各自 hash）；变换条目带 `converted_from` 与 `source_content_hash`。生成物计入体积/件数预算，并与资源内容一同参与再访 hash 校验；但标识资源内容的 `content_digest` 只由资源文件构成——索引与历史不是资源的同一性。

### 32.4 增量能力的决定与理由

- **不引入 openpyxl**：xlsx 是 ZIP + XML，取单元格文本所需的范围用标准库即可完整覆盖。为此改动锁文件、扩大依赖面与供应链面，收益不抵成本。展开前按 `ZipInfo.file_size` 的声明值与条目数拒绝 ZIP 炸弹（不先解压再判断）。
- **保留单元格坐标**：目的不是复现表格外观，而是让「设计书某 sheet 某单元格」可被 Evidence 指认。因此按行聚合、每个值前缀坐标——检索命中一行即可同时得到同行其他列（查找表按键取值的实际用法）与精确坐标。日期按 xlsx 的存储形式（serial 值）原样输出，不做格式推断。
- **转换失败仍进 `skipped`**：`unconvertible_spreadsheet`（坏文件/非 OPC）与 `conversion_exceeds_file_limit`（转换后超单文件上限）都不静默丢弃，维持「读不了 ≠ 不存在」。
- **历史只取显示名**：`%an` 而非 `%ae`。工作区文本里增加联系人对追溯没有收益，却扩大个人信息面。
- **历史按 scope 引**：svn 对 repository root 引 log 会带出 binding 未授权路径的变更；因此按 scope 路径分别引再并合，且不加 `-v`（不取变更文件清单）。

### 32.5 验证与残余

- `ruff check` / `mypy src` / `pytest`：W4 完成时点 **647 passed, 12 skipped**（W4 前为 620 / 11），W5 完成后
  **662 passed, 12 skipped**。
- **实 git/svn 验证**：`tests/agent/test_repository_client.py` 在 tmp_path 上用 `git init` /
  `svnadmin create` 造真实仓库并以 `file://` 读取。W4 覆盖 revision 解析、scope 裁剪、symlink skip、
  traversal 拒绝、读取上限，以及「物化 → `workspace.search` 命中」这条 §19 主链路；W5 追加 git 的 scope
  过滤与新旧顺序、条数上限，svn 的多路径并合与 revision 降序。命令缺失的环境自动 skip。计划原记
  「W4 本机不可验」，实际本机具备 git 2.34.1 / svn 1.14.1，故改为已本地验证。
- **xlsx 验证**：`tests/agent/test_spreadsheet_text.py` 在测试侧同样只用标准库组装真实 OPC 结构
  （共享字符串、inline 字符串、多 sheet、布尔、空单元格、换行折叠、ZIP 炸弹拒绝）。
- **materializer 验证**：索引内容（含 skipped 行）、xlsx 文档物化为 `.xlsx.txt` 且可经 `workspace.search`
  命中、`converted_from` 与原文件 hash、坏 xlsx 记 `unconvertible_spreadsheet`、历史文件首行的条数声明、
  生成物在再访时也做 hash 校验。
- `scripts/validate_contracts.py`（70 schema・53 example）、`validate_compose.py`、
  `probe_claude_agent_sdk.py` 均通过（W5 未新增 capability / schema，契约面无变化）；web `tsc -b` 与
  216 tests 未受影响。
- **残余**：分类与当前状态见 §35。其中「凭据注入路径只跑了无凭据分支」已由 §35.1 的 stub 测试补齐。

## 33. §19 把物化产物告知 Agent（2026-07-25）

### 33.1 问题

资源快照工作包把内容物化进了 `input/`，但 AgentTaskBrief 与执行 prompt **从未提到它们存在**：Brief 的 `resources[]` 只有 `key`/`kind`/`capabilities`/`binding`，prompt 里也没有一句说明落点。于是 §19 的整条前提——「Agent 用既有 `workspace.search/read` 自走发现」——依赖模型自己想到去搜 `input/`。这是 §19.5 两个资源验收层最可能的失败方式，且失败时表现为「Agent 说资源不存在」，与真正的缺资源无法区分——验收会把结论指向错误的原因。

### 33.2 交付内容

- **`agent/domain.py`**：新增 `MaterializedResource`（requirement_key / kind / provider / root / manifest / index / history / revision / files / skipped）。放在 domain 而非 materializer，是为了让 Brief 投影层不依赖物化实现。
- **`agent/workspace_materializer.py`**：`materialize()` 从返回 `None` 改为返回记述子元组。**初次物化与 reuse 经路都由同一份 manifest 派生**（`_describe(manifest)`），因此重试与首次的案内不会漂移；`_verify_materialized` 相应改为返回校验过的 manifest。
- **`agent/task_brief.py`**：Brief 的资源项按种别/key 对应上落点——document 按种别（D-W3=(a) 下全 Project 文档共用一个根，多个 document 要求指向同一处），repository 按 key。prompt 增一节列出根、索引、清单、历史与冻结 revision，并写明「先看索引或清单再检索」「引用文件时带上 manifest 的 revision」「`skipped` 里的是读不了、不是不存在」。
- **`agent/context_builder.py`**：把物化器的实际返回值传给 Brief。**未物化的要求不写落点**——落点只来自物化器写下的结果，不由蓝图推测，否则未配线环境会案内一个不存在的目录。
- **契约同步**：`contracts/agent-task-brief/v1.schema.json` 的资源项新增可选 `materialization`（`additionalProperties: false`，故必须显式声明），`contracts/examples/agent-task-brief.v1.json` 补一份带 `materialization` 的资源示例。

### 33.3 决定与理由

- **D-W6 定为写进 Brief**（prompt 是它的渲染）。Brief 是「Agent 被告知了什么」的受审记录；只改 prompt 会让审计记录与实际告知不一致。实际同步面比预估小：`task_brief_checksum` 未被任何测试钉死字面值，Web 只消费 checksum 字符串、不消费 Brief 正文，因此 OpenAPI 与 Web 侧零改动。
- **落点只来自实际物化结果**。用蓝图推测会在未配线/未物化环境案内不存在的目录，把「读不到」变成新的失败源。
- **history 仅在存在时写出**。document 一路没有提交历史，写一个读不了的 path 会诱导 Agent 反复尝试。
- **prompt 用 capability 名（`workspace.search/v1`）而非 SDK 工具名**，与 prompt 内既有的工具指示保持同一套词汇。

### 33.4 验证

- `ruff` / `mypy strict` / `pytest`：**668 passed, 12 skipped**（W5 后为 662 / 12）。
- 新增测试：Brief 携带落点且通过冻结契约校验、未物化时不写落点、prompt 含落点与 `skipped` 释义、未物化时 prompt 无该节；物化器返回的记述子与实际写下的文件一致（案内的 path 都 `is_file()`）、reuse 经路返回同一组记述子。
- `validate_contracts`（70 schema・53 example）、`validate_compose` 通过；OpenAPI 与 Web 未受影响（Brief 正文不经 API 公开）。
- **残余**：模型是否因此真的先读索引/清单，仍需部署上观测——这正是 §19.5 文档资源验收层的观测点。

## 34. §20 代码仓库回写：受控变更提案 → 分支 commit（2026-07-25）

D1–D4 于本日批准（建议原文见 `docs/01` §20.2）。R1–R3 一并实施，把 `observe → propose → apply` 从 Redmine 字段更新延伸到代码仓库。

### 34.1 仓库写入契约与提案验证

- **契约**：`contracts/tools/repository.write/v1/{request,response,error}.schema.json` + 两 example，并同时登记进 `scripts/validate_contracts.py` 与 `backend/tests/contracts/test_contracts.py` 的 example 表。capability catalog 增 `repository.write/v1`（providers `["git"]`），checksum 重算并同步三处（catalog、interpreter-request 内嵌 catalog 全文、importer 测试）。
- **`effects/repository_write.py`（新增）**：提案验证器。分支必须在 `projectmind/` 预约 namespace；变更 path 必须落在 binding 的 `paths` scope 内（复用 §19 的 `path_within_scope`，为此把该判定从 `agent/repository_source.py` 移到 `integrations/domain.py`——scope 语汇的单一实现处）；只接受 UTF-8 全文 `SET` 与 `REMOVE`；单文件 1 MiB、单次 50 文件 / 4 MiB 上限。
- **`effects/catalog.py`（新增）**：apply 可能 capability 的单一登记表（provider、provider_version、`preauthorizable`、验证器、scope 投影）。此前 apply chain 的四处判断都硬编码 `issue.update/v1`，加一个 capability 就要同时改四处、且容易只改松不改紧。`runs/repository_effects.py` 的四处改为查表。

### 34.2 git Provider 与可写工作副本

- **`agent/repository_client.py`**：新增 `open_writable`（带 checkout 的 clone → `rev-parse` 定 base → `checkout --detach`）与 `GitWriteSession`（`remote_branch_head` / `read_file_at` / `fetch_branch` / `commit_and_push`）。凭据、URI scheme 限制、最小环境、超时全部沿用 W4 的同一实现；commit 的 author 固定为平台身份（承认记录由 Approval 承担，不把执行者写进历史）。
- **`effects/repository_effect.py`（新增）**：`GitRepositoryWriteProvider`。顺序是「解析 base（CAS）→ 查 remote 分支 → commit + push → 从 remote 取回读校验」。三种结果被明确区分：分支不存在则新建（`replayed=False`）；分支存在且内容与提案一致则 replay（不再 push）；分支存在但内容不同则 `target_branch_conflict` 失败，**不使用任何 force 选项**。
- 修复一处分类缺陷：git 用 `fatal: Not a valid object name <sha>:<path>` 表示「该 revision 无此 path」，此前未归入 `not_found`，导致删除文件的 read-back 与连接失败无法区分。

### 34.3 接入 effect service

- `worker/settings.py` 注册 `EffectProviderDefinition(repository.write/v1, git, requires_secret=True)`，与读取共用同一个 `GitCommandRepositoryClient` 实例。**push 需要凭据而匿名 clone 不需要**，这种非对称由 capability 粒度的 `requires_secret` 表达，不下沉到 provider 粒度。
- `effects/policy_repository.py` 的事前许可可否改由能力表驱动：`preauthorizable=False` 的 capability 连 policy 都建不出来。只在提案侧守「常时人工承认」是不够的——policy 一旦存在，后续提案就会被自动批准。

### 34.4 实施中新增的两条边界（已回填 §20.2）

- **分支前缀由平台固定为 `projectmind/`**，而非逐 Integration 配置。「绝不 push 到默认/受保护分支」因此由结构保证，不依赖配置正确；Provider 在承认済み snapshot 上也再检查一次（最终防线）。
- **PR/MR 的自动开设推迟到 R4**：需要 forge 类型、API endpoint 与 project 标识，而 Integration config 只有 `repository_uri`/`default_revision`；从 host 名猜 forge 类型对自建实例不可靠，与 fail-closed 取向相悖。当前产出「分支 + commit + read-back Evidence」，评审由人在 forge 上发起。闸门不受影响：该 capability 不可预授权，每次 apply 前都已有人工批准。

### 34.5 验证

- `ruff` / `mypy strict` / `pytest`：**692 passed, 12 skipped**（W6 后为 668 / 12）。
- **真实 git push 验证**：`tests/effects/test_repository_effect.py` 用 bare repository 作 remote，经 `file://` 实际 push，确认分支内容、read-back、既定分支未被触碰、冪等 replay、同名分支冲突不覆盖、base revision 不符判 STALE、预约 namespace 外拒绝。
- 提案侧：预约 namespace 外的分支、scope 外 path、`..` 逃逸、`APPEND`、非文本值、超限、空 scope binding 均在提案校验时拒绝；事前许可 policy 对 `repository.write` 创建失败。
- Worker 侧：凭据缺失时 Provider 不被调用即失败；分支冲突映射为 `target_branch_conflict` 且 `retryable=False`。
- `validate_contracts`（73 schema・55 example）、`validate_compose` 通过。
- **残余**：分类与当前状态见 §35。其中 PR/MR 开设已在 §35.3 落地，svn 写入仍待一个决策（D5）。

## 35. 凭据传递测试、残余重新分类与仓库回写扩展（2026-07-25）

此前各节的「残余」把四类不同的东西列在一起，读起来像「十几项都卡在部署上」。本节补上可本机验证的部分、按性质重新分类，并落地 R4 中不依赖新决策的增量。

### 35.1 凭据传递的回归约束（原先只靠读代码保证）

「secret 不进 argv」是安全性质，之前没有任何测试守护。新增 `tests/agent/test_repository_credentials.py`：把 client 的 `executable` 指向 stub 脚本，记录**实际启动的 argv、环境变量与 stdin**，然后断言

- git：secret 与 `user:secret` 都不在 argv；`GIT_CONFIG_KEY_0=http.extraHeader`，其 value 的 base64 解开正好是 `user:secret`；`GIT_TERMINAL_PROMPT=0`；无凭据时连 `GIT_CONFIG_COUNT` 都不设置。
- svn：argv 有 `--password-from-stdin` 与 `--no-auth-cache` 但没有密码本身，密码只出现在 stdin；无凭据时不加该选项、stdin 为空。
- 两者的子进程环境都在最小集合之内（用「集合包含」而非「逐个黑名单」判定），且即使 Worker 侧设了 `PROJECTMIND_DATABASE_URL` / `PROJECTMIND_MANAGED_SECRET_KEK` 也不会继承。
- 承认済み写入路径（`open_writable`）走同一注入，同样不落 argv。

真正不可本机验的只剩「HTTPS 服务器是否接受该 header」这一段互通性。

### 35.2 残余的四类分法

| 类别 | 内容 | 何时能清 |
|---|---|---|
| **A 实机 + 模型** | §19.5 文档资源验收层/仓库资源验收层；解释器静态门越门与过程重表达行为；资源快照的模型使用行为；仓库回写端到端「提案 → 批准 → 落地」 | 需重新部署且有模型 egress |
| **B 只差依赖服务，测试已写好** | 12 个 skip 全部来自「本容器无 PostgreSQL」，含 §20 的 `load_bound_run_resource` 实 DB 再校验 | 任何有 PG 的环境跑 `pytest` 即可 |
| **C 外部系统互通** | 真实 HTTPS git/svn remote 的读与 push；真实 forge 的 PR API | 有可达 remote 的环境；凭据**传递方式**已由 §35.1 固定 |
| **D 刻意未做的范围（不是验收项）** | svn 写入（待 D5）；PDF 文本化；PR 自动合并（§20.4 长期非目标）；多仓库原子变更 | 需要决策或新需求，不应记入「残余」 |

### 35.3 仓库回写扩展（不依赖新决策的部分）

- **按 Integration 收窄分支前缀**：git/svn config 增可选 `write_branch_prefix`，只能在 `projectmind/` 预约 namespace **内部**再收窄，向外扩展一律拒绝。提案校验与 Provider 各执行一次。为此把 `EffectCapabilityDefinition.validate` 的签名扩为 `(draft, binding_scope, integration_config)`——config 侧独有的约束若拖到 apply 才发现，批准画面上就会出现「批准了也落不了地」的提案。
- **docx 文本化**：`agent/spreadsheet_text.py` 更名为 `agent/binary_text.py`（对应格式已不止表格），新增按扩展名分派的 `render_text`。docx 按「段落号 + 表座标」输出（`P12: 本文` / `T1R2: 机能ID | 担当者`），见出し保留 `#` 层级——与 xlsx 保留单元格坐标同理，让检索命中的行可被 Evidence 指认。仍**不引入新依赖**（docx 同为 OPC）。PDF 需要 parser 实现或新依赖，保持不做，读不了的二进制照旧进 `skipped`（理由码统一为 `unconvertible_document`）。
- **PR/MR 开设**：新增 `effects/forge.py`（GitHub / GitLab）。forge 的种别、API endpoint、project 标识由 Integration config **明示**（三者要么齐备要么全无，部分设置直接拒绝），不从 host 名推测——推错的后果是「往别的地方写」。开设前先查同 source branch 的 open PR 并复用，因此重放不会产生重复 PR。PR 失败不回滚 commit：commit 已经过 read-back 验证，PR 只是把评审入口交给人。

### 35.4 待决 D5：svn 写入

svn 的「分支」是服务端 `svn copy`，其**源与目标都依赖仓库布局**（trunk/branches/tags 与否、`repository_uri` 指向 trunk 还是根）。猜布局的后果是往错误路径提交，与本项目一贯的 fail-closed 取向相悖，因此不实现。

需要的输入只有一个：**该 SVN 仓库的分支目标 URL 约定**（例如 `https://svn.example/repo/branches/projectmind/`）。给出后，实现形态与 git 对称——预约 namespace 下的新分支路径、承认为闸门（该 capability 不可预授权）、提交后 `svn cat` 逐文件读回校验、同名路径内容不同则判冲突不覆盖。

### 35.5 验证

- `ruff` / `mypy strict` / `pytest`：**706 passed, 12 skipped**（§20 R1–R3 后为 692 / 12）。
- `validate_contracts`（73 schema・55 example）、`validate_compose`、`probe_claude_agent_sdk` 通过；web `tsc` 与 216 tests 未受影响。
- 新增测试：凭据传递 6 项；docx 段落/表/见出し/分派入口/坏文件 5 项；PR 开设、未配置 forge 不开 PR、重放复用既有 PR 3 项。

## 36. §20 svn 写入与「批准后直接进主分支」（2026-07-25）

用户决定两件事：svn 的分支目标由代码固定一个约定；承认后直接合并进主分支。两者一并落地。

### 36.1 svn 分支目标 = `<仓库根>/branches/projectmind/<名称>`

之前推迟 svn 写入的理由是「分支目标依赖仓库布局，猜错就写到别处」。解法不是让用户逐仓库配置，而是**问 svn 自己**：`svn info` 的应答带 `<repository><root>`，所以无论 `repository_uri` 指向 trunk 还是仓库根，都能定位到仓库根，再拼平台约定的 `branches/projectmind/`。`branches/` 不存在时由 `svn copy --parents` 建立。这样「代码里约定一个」成立，且不含对客户布局的假设。

- **`agent/repository_client.py`**：新增 `SvnWriteSession`（`branch_url` / `path_exists` / `head_revision` / `create_branch`（服务端 copy）/ `commit_files`（按 base revision checkout → 写 → commit）/ `read_file_at`）与 `open_writable`。凭据、最小环境、超时沿用 W4 同一实现。
- **`effects/repository_effect.py`**：新增 `SvnRepositoryWriteProvider`，与 git 侧同构——同名分支内容一致判 replay、不一致判 `target_branch_conflict` 不覆盖，提交后 `svn cat` 逐文件读回校验。
- svn 的 CAS 由「按 base revision checkout 后 commit 被 out-of-date 拒绝」天然承担，不需要另造机制。
- `PROVIDER_DEFINITIONS["svn"]` 因此可声明 `repository.write/v1`；能力表的 `provider_version` 改为 **Provider → version 的映射**（git 是 `git-branch-commit/v1`，svn 是 `svn-branch-commit/v1`），EffectExecution 记录实际选中的那个。

### 36.2 `write_mode` = `branch` / `direct`

`direct` 时承认済み变更**直接 commit 到既定分支**，不建预约分支、不开 PR。

- **git**：目标必须等于 `default_revision` 指向的分支；远端分支头必须仍等于提案冻结的 base（否则 STALE），push 为 fast-forward，**不使用任何 force**。
- **svn**：直接对绑定的 `repository_uri` 路径 commit，out-of-date 即拒绝。
- 提案校验与 Provider 各校验一次「direct 只能写既定分支」——任意分支的直接写入会让预约 namespace 这个唯一的结构性歯止め失效。
- `verification` 增 `write_mode`，审计能读出这次是以哪种方式落地的。

**本工作包交付时默认值为 `branch`**，理由是「既有 Integration 不应因一次改版获得主分支写入权」。同日用户决定把默认值改为 `direct`，变更与其连带影响记于 **§37**（本节保留交付当时的状态）。

### 36.3 验证

- `ruff` / `mypy strict` / `pytest`：**716 passed, 12 skipped**（§35 后为 706 / 12。默认值变更後の計数は §37）。
- **真实 svn 验证**（本地 `svnadmin` 仓库，trunk/branches 布局）：branch 模式下内容落在 `branches/projectmind/...` 且 **trunk 未被触碰**；direct 模式下内容进入 trunk；同名分支已存在且内容不同判冲突。
- **真实 git 验证**（bare remote）：direct 模式下主分支头变为新 commit 且不建预约分支、不开 PR；主分支在提案后被他人推进时判 STALE 且**他人的 commit 保持不变**；direct 模式指向其他分支被拒绝。
- 提案层：direct 只接受既定分支、branch 模式仍要求预约 namespace、收窄 prefix 的外部被拒。
- `validate_contracts`（73 schema・55 example）、`validate_compose` 通过。
- **残余**：真实 HTTPS/https-svn remote 与真实 forge PR API 的互通（属 §35.2 的 C 类）；端到端实机验收（A 类）。

## 37. `write_mode` 默认值改为 `direct`（2026-07-25）

§36 的 R5 按默认 `branch` 交付，理由是「既有 Integration 不应因一次改版获得主分支写入权」。**用户决定把默认值也改成 `direct`**：判断依据是 ChangeProposal 的人工批准即是闸门——该 capability 不可预授权，每次落地前都有人批过；想再挟一层分支/PR 评审的仓库显式设 `branch`。

本节记录这次变更本身、它连带逼出的边界，以及影响面。

### 37.1 变更内容

- `integrations/domain.py` 新增 `REPOSITORY_WRITE_MODE_DEFAULT = REPOSITORY_WRITE_MODE_DIRECT`，三处读取默认值的地方（登记正规化、提案验证器、git/svn 两个 effect Provider）统一走这一个常量，避免默认值分散在三处各写一遍。
- 语义本身未变：direct 与 branch 两种落地方式、CAS、read-back、冲突判定、`preauthorizable=False` 都与 §36 相同。变的只是「不配置时走哪条」。

### 37.2 默认值翻转逼出的一条硬边界

**git 的 `default_revision` 不能停留在 `HEAD`。** `HEAD` 不是分支名，而 direct 要写「既定分支」——push 目标会变成无效的 `refs/heads/HEAD`。默认是 `branch` 时这个组合不会发生（分支名来自提案），改成 direct 后，**只填 `repository_uri` 的 git Integration 必然落进这个状态**。

因此：声明了 `repository.write/v1` 的 git Integration，若 `default_revision` 仍是 `HEAD`，**登记时即拒绝**并提示要具体分支名。

三点边界：

- 只声明 `repository.read/v1` 的 git Integration **不受影响**。判定最初被放进 `_validate_provider_config`（不认识 capability 的那层），于是把只读 Integration 也一起拒了——**这是既有测试 `test_repository_scope_rejects_reserved_wildcard_token` 当场挡下来的**，随后把判定移到 `normalize_integration_command`（认识 capability 的那层）。
- svn 的 `HEAD` 是正当的 revision 式，不在此列。
- `write_mode=branch` 时 `default_revision` 保持 `HEAD` 依然合法（分支名来自提案的预约 namespace）。

### 37.3 影响面与运维要点

- **既有 repository Integration 的 config 里没有 `write_mode`，因此继承新默认 `direct`。** 该能力与本次默认值同日交付，实际无历史配置受影响；将来若要为存量保留旧行为，需要一次把 `write_mode: branch` 写入既有 config 的迁移。
- **保护分支的仓库应显式设 `branch`**。否则 direct 下 push 被服务端拒绝——这是预期的 fail closed，但更该在配置时决定，而不是运行时才发现。运维要点已写入 `docs/10` §8.6。

### 37.4 测试侧的调整

验证 branch 模式性质的用例（预约 namespace、收窄 prefix、冲突不覆盖、PR 开设等）此前依赖「默认即 branch」，现在一律**显式声明 `write_mode: branch`**——让「这个用例测的是什么」不随默认值漂移。另新增一个用例直接钉住「git 的 direct + `HEAD` 在登记时被拒，而 `branch` + `HEAD` 合法」。

### 37.5 验证

- `ruff` / `mypy strict` / `pytest`：**717 passed, 12 skipped**（§36 后为 716 / 12）。
- `validate_contracts`（73 schema・55 example）、`validate_compose`、`probe_claude_agent_sdk` 通过；web `tsc` と 216 tests 未受影响。
- 文档同步：`docs/01` §20.2 D6（含影响面与两条约束）· §20.3/§20.4/§13 表、`docs/10` §8.6（配置注意三条）、AGENTS、README、`docs/13`。

## 38. §20 Web 面：写入配置与代码变更审批界面（2026-07-25）

R1–R5 全部落在 backend，Web 侧留下两个缺口。第二个是安全相关的，因为 §37 把默认落地方式定成 `direct` 之后，**审批界面成了唯一的闸门**。

### 38.1 缺口一：Web 根本配不出可写的 repository Integration

`PROVIDER_FORMS` 里 git/svn 的 `writeCapability` 仍是 `null`（§20 之前的事实），所以资源画面只能建只读集成，§20 的全部能力只有直接调 API 才够得着。

- git/svn 的 `writeCapability` 置为 `repository.write/v1`。
- 连接表单在选了「读写」时展开写入设置：落地方式（`direct` 默认 / `branch`）、分支前缀（仅 branch 模式）、forge 三项（可选，GitHub / GitLab）。
- `buildIntegrationConfig` **只在启用写入时**才发送写入相关 key。只读配置里混入 `write_mode`，会在将来补上 write capability 时带着一个没人想过的默认值跑起来。
- `findWriteConfigIssue` 把 backend 的 fail-closed 规则镜像到提交前：git 的 `direct` + `HEAD` 组合、分支前缀越出 `projectmind/`、forge 三项不齐——都在本地给出可读提示，而不是等 422。三语文案同时补齐。

### 38.2 缺口二：审批界面把变更原样 `JSON.stringify`

原实现是 `<pre>{JSON.stringify({changes, precondition})}</pre>`。issue field 更新的值是标量，这样看没问题；但 repository 变更的 `value` 是**整份文件正文**，审批者看到的是满屏 `\n` 转义的单行 JSON——在 direct 默认下，这等于让人对着看不懂的东西按批准。

- `/files/` 开头的变更改为**逐文件渲染**：路径、操作徽标（写入 / 删除）、行数与字节数，正文放在可折叠的等宽块里（`<details>` 折叠，符合既有页面测试用 `renderToStaticMarkup` + `toContain` 的约束）。
- 基线 revision 提到显眼处——它是「这份改动基于哪个版本」的前提。
- 非 `/files/` 的提案保持原有 JSON 渲染：issue field 更新的值很小，结构化反而更慢读。

### 38.3 验证

- web `tsc -b`、**220 tests（此前 216）**、`vite build` 全绿；backend 未改动（717 passed / 12 skipped 保持）。
- 新增测试：写入 key 仅在启用写入时发送、分支前缀仅在 branch 模式发送、forge 三项同进同出、四条 fail-closed 规则的镜像；审批界面逐文件渲染且**断言正文不是转义形式**（`not.toContain('\\n    if payload is None:')`）。
- 既有测试 `capabilitiesForAccess > ignores read_write for providers without a registered write capability` 随 §20 的事实变化改名并更新预期——它原本钉住的是「repository 没有写能力」，那已不再成立。

## 39. §21 把过程重表达规则对齐到已实现能力（2026-07-25）

I2（§30.2）写下的三档映射是按**当时可用的能力**定的。§19 W5 与 §20 之后，其中三条例子已经变成错的——而且是会持续造成损失的那种错：模型会照着规则，把平台**现在能做的事**继续降级成 guidance，且降级是静默的（schema 照样通过，蓝图看起来正常）。

### 39.1 三条过时的规则

| 原规则 | 现状 | 改为 |
|---|---|---|
| idiom 档：`svn list -R \| grep` → 「资源树物化到工作区**之后**的检索」 | 物化已是常态，且 W5 生成了 `.projectmind/files.txt` 索引——`workspace.search` 只做内容检索，按文件名找文件正是靠这个索引 | 明确写出索引路径，并说明索引同时列出读不了的文件，使「不存在」与「读不了」可区分 |
| guidance 档举例：「**commit 历史**没有等价能力」 | W5 物化 `.projectmind/history.txt`（限 scope、限条数） | 升为 idiom 档：读该文件 |
| guidance 档举例：「**binary 表格解析**没有等价能力」 | W5/§20 R4a 把 xlsx/xlsm/docx 在平台侧文本化为 `<原名>.txt`，保留 sheet 名与单元格坐标 / 段落与表格坐标 | 升为 idiom 档：读文本化副本 |

同时补上两条 §19/§20 之后才存在的映射：中间产物写入 → `workspace.write/v1`（并写明物化 `input/` 是冻结证据、永不可写）；**仓库变更步骤（`svn commit` / `git commit` / `git push` / 应用 patch）不是直译，而是 `access=write` 资源上的 `apply` effect intent + `change.propose/v1`**——平台落地，Agent 只提案。

guidance 档保留给真正没有等价能力的：为副作用运行任意脚本、平台无法文本化的格式（例如 PDF）、没有注册 Provider 的系统。

system Skill 升 **3.3.0**，`source_hash` / `prompt_checksum` 与两个 fixture、importer 测试同步重算。

### 39.2 顺带堵上 §20 遗漏的一处同步点

`manifest_gate._EFFECT_ONLY_CAPABILITIES`（「只能由 EffectExecution Worker 调用、不得作为 Agent Tool 宣言」的集合）在 §20 注册 `repository.write/v1` 时**没有同步**。后果：Skill 可以把 `repository.write/v1` 写进 `tools`，publish gate 不报错，于是「Agent 直接持有仓库写能力」的宣言能通过发行门。

已加入该集合，并把门禁测试改成对两个 write capability 都断言——只守一个的话，下次再加 apply 能力还会漏。

### 39.3 验证

- `ruff` / `mypy strict` / `pytest`：**718 passed, 12 skipped**（§38 后为 717 / 12）。
- 新增测试 `test_system_skill_maps_procedure_onto_the_capabilities_that_now_exist`：钉住新映射在 prompt 中的在场（索引 / 历史 / 文本化副本 / workspace.write / 「Agent 只提案不 commit」/ guidance 档只留真正无能力的）。**这条只能这样守**——规则由模型执行，确定性检查看不见它掉没掉，掉了也只是安静地降级。
- 门禁测试扩为两个 write capability。
- `validate_contracts`（73 schema・55 example）通过；契约 fixture 的 identity 与新 3.3.0 一致。
- **残余**：模型是否真的按新档位映射，仍需部署上观测（与 §21.5 的 I2 验收同批）。
## 40. §22 任务调度（TaskSchedule）（2026-07-26）

`docs/01` §13 表里「调度、generated UI、并行 multi-agent｜未开始」合写了三件互不相干的事，其中调度是三者里唯一**设计已在 `docs/07` §8.1/§8.3 写好、只差实装**的。本节把它实现完，并把那一行拆成三行（§22 已实现、§23 决策已定、§24 受阻）。

### 40.1 调度不是新的执行路径

发火最终调用的是与即时执行**完全相同**的 `RunService.create_task_run`。调度只回答「**什么时候**、**以谁的身份**、**用哪份冻结配置**」开始；Run 之后的一切（Segment/Attempt、交互、审批、效果、binding 再校验）一字未动。这是刻意的：另开一条创建路径，就等于给每一道闸门开一个只在调度上生效的旁路。

### 40.2 决策里三条值得单独说的

**cron 用标准库自实现，不引入 `croniter`。** 5 段式的解析与「求下一个触发时刻」是确定性纯函数，可以完整测试；为它引入依赖反而扩大供应链面（与 `agent/binary_text.py` 同一取舍）。`L` / `W` / `#` / `@reboot` 等扩展语法**保存时即拒绝**——黙って别的意思解释，会让用户写的时刻和实际发火长期不一致。POSIX 的「日与星期都非 `*` 时取 OR」特例按原样实现，并且靠**记录原字段是否字面为 `*`**来判定，而不是拿集合和全集比（`1-31` 与 `*` 集合相同但特例待遇不同）。

**DST 的两处歧义显式定死，而不是让 `zoneinfo` 的默认行为决定。** 春季**不存在**的本地时刻（跳过的那一小时）**跳过该次**，取下一个匹配时刻——顺延到邻近小时会让「每天 02:30」在那一天变成不确定的时间；秋季**重复**的本地时刻只触发**一次**（取 `fold=0`），否则每年秋天多跑一次。PEP 495 下不存在的时刻不会抛异常、而是按遷移前 offset 解释，所以检测方式是「本地 → UTC → 本地」往返后比对壁钟。这类缺陷平时全绿、一年只坏两次，必须由测试钉住。

**错过的触发不追赶。** Worker 停机后重启，若已错过 N 次，只按「最近一次到期」执行一次，`next_run_at` 直接推进到当前之后的第一个匹配时刻，错过次数记进 `missed_count`。追赶 N 次会在恢复瞬间对外部系统产生突发写压力。

### 40.3 两道互不依赖的二重发火防线

1. **`next_run_at` 的 CAS 认领**（`UPDATE ... WHERE id = ? AND next_run_at = ?`）。认领的同时就把 `next_run_at` 推进到下一候选，因此发火处理本身失败也不会掴住同一时刻无限重试。没有专用 lease 列。
2. **决定性 idempotency key** `schedule:<id>:<发火时刻 ISO8601Z>`。`create_task_run` 已按 `(project, task, idempotency_key)` 幂等，所以即使两个 Worker 同时 tick、或 tick 与恢复重叠，同一次发火也只会产生一个 Run。key 强制归一到 UTC——否则 worker 的 TZ 设置不同就会让同一发火算出不同 key，防线直接失效。

### 40.4 触发时重新校验的两件事

- **重叠**：上一个 Run 仍处非终态则跳过并记录原因，`next_run_at` 照常前进（`docs/07` §8.3，MVP 不并行执行同一 Task）。
- **创建者权限**：schedule 记录 `created_by`，触发时重新确认该 user 仍 `ACTIVE`、仍能到达该 Project、且 Project 未归档。不满足则转 `ERROR` 停止。否则离职者留下的 schedule 会带着他的权限一直跑下去。判定复用 `ProjectRepository.get_accessible` 这个单一实现，不另写一份授权判断。

`ERROR` 只能由用户手动改回 `ACTIVE`，**不自动复活**：平台无法判断失效原因是否真的消除了，而 D4 要求「不黙って别的来源」。

### 40.5 保存时用本番同一条路径做验证

`ScheduleService` 保存前会跑 `SkillService.resolve_task_run`（版本可见性 + 输入 schema）和新增的 `RunService.validate_task_sources`。后者复用 `create_task_run` 内部的同一个 `_resolve_selected_sources`，只是不建 Run。写一份「验证专用」的实现看着更省事，但它和本番判定一旦分叉就毫无意义——而分叉是迟早的。目的只有一个：不产生「保存得下、第一次发火必然失败」的 schedule。

### 40.6 交付面

| 工作包 | 内容 |
|---|---|
| S1 | `schedules/cron.py`（5 段解析 + 时区求值）、`schedules/domain.py`（状态机、发火决议、`schedule_idempotency_key`）、`db/models.py` 的 `TaskSchedule`、migration `0025_task_schedules`（含 kind 与必填列的 CHECK、`(status, next_run_at)` 复合索引）、`schedules/repository.py`（CAS 认领） |
| S2 | `schedules/service.py`（增改、暂停/恢复/归档、发火预览、到期认领与发火）、纯函数 `plan_occurrence`（次回候选 / 见送回数 / 是否打断）、Worker 每分钟 `trigger_due_schedules` cron |
| S3 | `api/routes/schedules.py`（Project 作用域 CRUD + 预览 + 状态；不存在与越权同折 404、乐观锁冲突 409、定义不合法 422），OpenAPI 契约重新导出 |
| S4 | `web/src/api/schedules.ts` + `components/SchedulePanel.tsx`（左轨面板 + ModalDialog 表单 + 下次触发预览），zh/ja/en 三语键 |

Web 术语按 `docs/01` §17 的去术语化分层走**一般用户语**：中文「定时执行」、日文「定期実行」、英文「Scheduled runs」，不出现 cron/schedule 术语本身；周期规则字段的标签直接写出格式（`分 时 日 月 周`），不要求用户知道「cron」这个词。

### 40.7 验证

- `ruff` / `mypy strict` / `pytest`：**777 passed, 13 skipped**（§39 后为 718 / 12）。新增 59 项测试、1 项 skip。
- 新增 skip 的那一项是实 PostgreSQL 上的 CAS 认领测试（同一发火只能被一个 Worker 掴住、越 Project 不可读）——**只有真实 DB 的 UPDATE 影响行数能证明**，属 `docs/12` §35.2 的 B 类。
- `validate_contracts`（73 schema・55 example）、`validate_compose`、`probe_claude_agent_sdk` 通过；web `tsc` + **229 tests** + `vite build` 通过。
- `test_migration_head_removes_bootstrap_seed` 改名为 `test_migration_chain_has_a_single_expected_head` 并指向 `0025`：它守的其实是「Alembic 只有一个 head」，钉住具体名字是为了让分支 revision 混入时立刻失败。
- **残余**：migration `0025` 需实机应用；tick 的实际发火与 `missed_count` 行为只有部署上能观测（A 类）。

## 41. §23 / §24：把「未开始」拆成「方向已定」与「受阻」（2026-07-26）

同一行里并排的三项，性质完全不同。合写成「未开始」会让人以为它们只差排期。

**§23 并行 multi-agent —— 决策已定，实装未开始。** 决策见 `docs/01` §23.2。关键的一条是 **D1：子 Agent 一律只读，能力是主 Agent 的真子集**（永不含 write / effect / interaction 能力）。这不是保守，而是把最难的两类冲突——外部效果的归属、工作区写入的相互覆盖——**从存在性上消掉**：所有效果仍然只从主 Session 发起，人工批准面对的仍是同一条提案链。其次是 **D3：Worker 仍是唯一 event 写入者**，子 Session 的活动收敛成主 Session 上的一次 ToolCall + Evidence，因此 AGENTS 的「Run 内 sequence 严格单调增」不必改成分层序号，SSE 契约不动。落点已逐条核对过当前代码：引擎的 `_active` 本就是带锁的 dict（结构上已支持并发 session），`RunContext` 是 frozen dataclass（`replace()` 即可派生受限子上下文），唯一的契约同步点是新增一个 continuation mode。

**§24 generated FrontendModule —— 受阻，且不是排期问题。** 三个阻塞项都不是写代码能解决的：①生成模块要求**不与 Web 同源**的独立静态 Origin（`docs/07` §12.1），而 `compose.yaml` 里没有这样的服务、`AGENTS.md` 的 Ingress 规约又限定只有 `web`/`api` 能接外部 edge network——这属于部署拓扑变更；②构建沙箱与内部依赖镜像不存在；③威胁模型被文档列为启用前置。

**为什么不先做「能做的那部分」**：静态拒绝分析器确实是纯函数、可离线测试，但它是为一扇尚不存在的门配的锁。在隔离 Origin 与构建沙箱到位之前把生成代码跑起来，正是 `docs/01` §8.3 与 AGENTS「不得添加生成 FrontendModule」所禁止的形态；而只交付分析器，会让「generated UI 已部分完成」这个误判进入计划——那比什么都不做更贵。解阻所需的三项输入列在 `docs/01` §24.4。
## 42. §25 前端信息架构重整：任务中心、待办出口与概览改造（2026-07-26）

### 42.1 诊断：问题不在视觉

样式基线是成熟的——token 体系、960/480 断点、`prefers-reduced-motion`、50 组配色对比度已验 ≥4.5:1。
不到产品级的是**新功能被逐个焊在旧画面上**积累的信息架构债：

- `docs/07` §8 的 Task Center **早已写在规范里，实现中却不存在**。任务只是工作空间弹窗里一个
  `<select>` 的选项，它的就绪度、需要的数据来源、定时安排、上次执行分散在四处，用户无法回答
  「我现在能跑什么、哪些还差配置」。
- 工作空间左轨过载：新建入口 + 全高历史 + §22 S4 塞进去的定时面板，三块挤在 300–340px 一列。
- **等待中的 Run 没有全局出口**：只有进工作空间**且恰好选中那个 Run** 才看得见。
- 概览的功能模块 card 网格是 sidebar 入口的复制品。

### 42.2 任务中心：新增一等画面，并定死与工作空间的分界

**任务中心「选」，工作空间「观」。** 前者把就绪度、资源、定时、上次执行横并排比较；后者聚焦一个 Run
的进展/结果/证据/待答项。任务中心的「立即执行」跳转到工作空间，**刻意不在这里复制第二套 Run
lifecycle**——两套执行路径意味着 SSE 订阅、取消、终态化各有一份实现，迟早分叉。

定时执行随之从工作空间左轨迁到任务行内。§22 S4 把它放在左轨是因为当时没有任务画面，它绑定的是
「此刻的草稿」，一眼读不出这条预定属于哪个任务。工作空间左轨恢复为「新建执行 + 全高历史」。

为避免即时执行与定时执行各持一份表单，抽出 `lib/taskDraft.ts`（纯逻辑：`TaskDraft`、资源要求投影、
来源映射、输入解析）与 `components/TaskLaunchFields.tsx`（就绪度 + 来源选择 + 输入）。两个画面用同一实现，
否则「保存得下、发火必然失败」的配置会从检验较松的那一侧漏进来。

### 42.3 上次执行的关联：不在前端重新推导 UUID

一版实现里 `buildRows` 把「最近一条 Run」贴给每一行，于是**每个任务都显示同一个时刻**——不是缺失，
是错误数据。正确的 join key 是 Run 的 `task_id`，它由 `uuid5(NAMESPACE_URL, "projectmind:task:<version>:<key>")`
决定性导出。在前端重新实现 uuid5 会让导出方式变化时出现「能执行但历史对不上」的静默不一致，因此：
把该导出收敛为 `runs/domain.py` 的 `derive_task_id` 单一实现（`RunService` 原先内联），并在 task
descriptor 上公开 `task_id`，前端只做相等比较。契约测试断言两侧一致。

### 42.4 待办：筛选必须在服务端

Run 在等待回答/批准时**不持有 lease 也不计 wall timeout**，会无限期停在那里——看不见的等待等同于停机。
因此「待你处理」常驻概览首位与导航徽标。

实现上有一处不能妥协：**筛选在服务端完成**。只取历史首页再前端过滤，会漏掉更早页里的等待项，而
「漏报待办」比「不显示待办」更糟。为此 Run 历史 endpoint 增加可重复的 `status` 查询参数
（契约外的值 422 拒绝——静默忽略会退化成返回全件），三层（route → service → repository）透传到 SQL。

徽标取不到数时倒向 0（不显示）而非报错，避免用「取不到」冒充「没有」。

### 42.5 概览：回答「我现在该做什么」

原先的功能模块 card 网格把 sidebar 的入口逐个复制成卡片，对「我现在该做什么」没有任何贡献。
改为「待你处理 → 最近执行 → 服务/项目状态」，并把「前往工作空间」改为「查看任务」——下一步该做的是
选任务，不是打开一个空的观测台。测试固定了两件事：待办排在最近执行之前，以及**不再出现**
`moduleCard`/`moduleIcon`。

### 42.6 死代码清理

- 概览改造后失效的 41 行 CSS（`.moduleSection` / `.moduleGrid` / `.moduleCard` / `.moduleIcon` /
  `.moduleAction` 及其响应式与 reduced-motion 分支）。
- 三语 catalog 里 6 个已无引用的 key（`fieldKeysLabel`、`previewAria`、`proposalsCount`、
  `segmentsCount`、`configure`、`nextRun`）——其中后两个是本次改造造成的，前四个是既有残留。
- `neverRun` 原本声明后未使用：任务从未执行时该行是空白，与加载中无法区分，现改为明示「从未执行」。
- 机械核查：三语 key 集合完全一致（各 569 键）；`src` 内 0 个未被引用的 export。

### 42.7 验证

- backend `ruff` / `mypy strict` / `pytest`：**781 passed, 13 skipped**（§40 后为 777 / 13）。
- OpenAPI 重新导出（新增 `status` 查询参数与 `task_id` 字段）；`validate_contracts`
  （73 schema・55 example）、`validate_compose` 通过。
- web `tsc` + **237 tests** + `vite build` 通过（§40 后为 229）。
- **残余**：真实浏览器上的三语/键盘/窄屏验收仍需部署（与 §17 的验收同批）；本机只验到静态 markup
  与 build 产物。
## 43. §25 收尾：「上次执行」的窗口缺陷与命名对齐（2026-07-26）

### 43.1 我在 §42 引入的缺陷

任务中心的「上次执行」原本是前端 join：拉最近 50 条 Run（`RECENT_RUN_WINDOW = 50`）再按 `task_id`
匹配。后果是——**最后一次执行不在项目最近 50 条里的任务，卡片显示「从未执行」**。这不是缺失，是
错误值；而且随项目活跃度增长必然发生，越是老任务越容易中招。

这与 §42.4 里「待办筛选必须在服务端」是同一类问题（前端窗口过滤 → 漏报），只是待办那侧当时走了
服务端，任务这侧没有。**同一个判断没有贯彻到两处，是这次的真实教训。**

### 43.2 修法：服务端投影，不依赖任何件数上限

新增 `RunRepository.latest_run_by_task`：用 window 函数
`row_number() over (partition by task_id order by created_at desc, id desc)` 取每个 task 的第一行。
不依赖件数上限的形式只有这一种。

合流点选在 **route 层**（`GET /projects/{id}/tasks` 同时持有 `skill_service` 与 `run_service`），
而不是让 `SkillService` 去读 Run——task catalog 是 Skill 派生的读模型，Run 历史是执行派生的读模型，
互相依赖会把两个聚合绑死。`PublishedTaskResponse` 因此多一个 `last_run`（未执行为 `null`，与
「取不到」可区分）。

前端相应地删掉整个 `loadRunHistory` 窗口取数与前端 join，`TaskRow` 不再自持 `lastRun`。

### 43.3 命名对齐

`workspaceModuleId` / `onSelectWorkspaceModule` 在 §25 之后已名不副实——模块筛选同时作用于任务中心
与工作空间。改为 `activeModuleId` / `onSelectModule`。同时把子菜单的跳转目标从工作空间改为
**任务中心**：按模块筛选后要看的是「这个模块能跑什么」，不是一个空的观测台。

### 43.4 验证

- backend `ruff` / `mypy strict` / `pytest`：**783 passed, 13 skipped**（§42 后为 781 / 13）。
- 新增契约测试两项：catalog 同梱最新 Run；未执行返回 `null`。为此把 `FakeSkillService` 的 catalog
  改为 instance 内不变——原先每次调用都重新随机 `skill_version_id`，突合せの検証が成立しなかった。
- OpenAPI 重新导出（新增 `last_run`）；`validate_contracts`（73 schema・55 example）、
  `validate_compose` 通过；web `tsc` + **236 tests** + `vite build` 通过。
## 44. §24 决策定案：generated FrontendModule 的方向与 Origin 形态（2026-07-26）

§24 此前记为「受阻」，阻塞的是三项**只能由用户决定**的输入。本次定案，§24 从受阻转为「决策已定 /
实装未开始」。

### 44.1 方向：走完整代码生成，并记录被否决的替代方案

选定 D1（生成 React/TS 源码 → 构建沙箱 → iframe 运行）。**同时记录被否决的方案**：曾提出「声明式
ViewSpec 扩展」——模型只产数据（布局 + 组件类型 + 数据绑定的 JSON），由平台用已审组件渲染，可一次
消掉独立 Origin、构建沙箱、供应链三个威胁面。否决理由是**表达力受限于平台组件库，每加一种呈现都要
先改平台**。把否决理由写下来，是为了将来重新权衡时不必从头论证。

### 44.2 Origin：用响应头换掉新域名，并写清换掉了什么

`docs/07` §12.1 原文要求「模块从独立静态资源 Origin 加载」。D2 改为**同主机专用路径
`{contextPath}/modules/<hash>/` + `Content-Security-Policy: sandbox allow-scripts` 响应头**。

依据来自 §12.1 自身：它已经承认「不启用 `allow-same-origin` 时 iframe 使用 opaque origin」并据此拒绝
把 `event.origin` 当身份。既然 iframe 内本就是 opaque origin，独立 Origin 真正多买到的只有一条——
**有人直接导航到 bundle URL（不经 iframe）时，它会在应用 Origin 上带完整会话 Cookie 运行**。而
`CSP: sandbox` 响应头对**任何**加载方式都强制 opaque origin，等效消掉这一条，且不需要新域名、新证书、
新 Traefik 路由（`AGENTS.md` 的 Ingress 规约因此不动）。

**代价写在明处**：与独立子域名相比少一层纵深——将来若有人误加 `allow-same-origin`，独立域名仍能兜住，
同主机不能。因此这个方案把「安全」押在两处必须常真的断言上，两处都要有测试钉死：①bundle 路径的每个
响应带该 header；②iframe 属性不含 `allow-same-origin`。这也是 `AGENTS.md` 新增该两条约束的原因。

### 44.3 威胁模型：不删，改为首次执行准入条件

用户选择「本轮不需要」。这与 D1（代码生成路线）并存的处理是：**威胁模型不是本轮决策的前置，但仍是
M3（首次真正执行生成代码）的准入条件，责任人待定**。不直接删掉的理由很具体——D2 恰恰是用「响应头
必须正确」换掉了一层纵深，这类取舍正需要威胁模型逐条确认残余风险。M1/M2 不执行生成代码，因此不受
它阻塞，可先行开工。

### 44.4 补定的三项（选了代码生成就必然要定，按 fail-closed 取值）

- **D4 构建沙箱**：独立 Compose service `module-builder`，不接 edge network，构建后断网跑测试。
  **内部 npm 镜像未提供时构建拒绝，不回退公网 registry**——回退正是要消掉的供应链面。镜像地址仍是
  M2 的外部输入。
- **D5 依赖白名单**：仅 React、`@projectmind/module-sdk`、`@projectmind/ui` 与批准的纯前端库；禁
  lifecycle scripts、原生扩展、Git dependency、动态依赖地址。
- **D6 失败即降级**：加载超时/协议不兼容/Host API 违规立即回退标准 ViewSpec，不触碰 Task/Run/
  Result/Evidence 数据。

### 44.5 同步范围

`docs/01` §24 全节重写 + §13 表该行 + §13.2 下一步；`docs/07` §10 的但书改为「方向已定、工作包见
§24.3」并**显式标注与 §12.1 原文的偏离**；`AGENTS.md` 的禁止条款从「不得添加」改为「只作为 §24.3
的工作包添加」，并把 D2 的两条不可放松约束写进规约。本次不含代码改动。
## 45. §23 扇出子 Agent 与 §24 生成模块静态闸门（2026-07-26）

### 45.1 §23 的安全性全部押在一条断言上，所以先把它做成结构

D1「子 Agent 只读、能力是主 Agent 真子集」不是一条检查，而是**让两类冲突不可能存在**的手段：
外部效果只能从主 Session 发起（帰属不会歧义），workspace 只有主 Agent 能写（并发子路不会互相覆盖）。

`agent/subagent.py` 的判定刻意做成**双保险**：明确的禁止集合（`change.propose` / `interaction.request` /
`subagent.dispatch` / `workspace.write`）**加上**按名字形状的兜底 `is_write_like`（含 `.write/` 或
以 `.update/v1` 结尾）。只有枚举的话，将来新增一个 write 能力、忘了加进来，就正好从那个缺口漏给子 Agent。
形状判断让**不认识的能力默认倒向拒绝**。

另一处刻意的选择：请求了禁止能力时**拒绝，而不是静默删掉**。悄悄减能力会让 Agent 按「我拿到了」的
前提写分支，失败点被推迟到子路运行中。

### 45.2 预算是切分，不是相乘

`split_budget` 把 Run 剩余的 `max_turns` / `max_output_bytes` 按 branch 数整除，**除不尽向下取整**。
向上取整会让合计超过 Run 上限——那样「并行 4 路」就等于把成本上限悄悄乘以 4。测试直接断言
`per_branch * branches <= 原上限`。

### 45.3 循环依赖用「调用时解析」破，不是构造时

engine → tool registry → 扇出 Provider → engine 是真实的环。Provider 收的是 `Callable[[], Engine]`
而非 engine 实体，解析推迟到**调用时**，于是 worker 的组装顺序不再是正确性的一部分。

### 45.4 失败隔离与 Evidence 收敛

一路失败（异常 / 超时 / 取消）作为**值**返回，不掀翻整组——否则昂贵的读取要全部重做，而且主 Agent
会丢掉「已经查清了哪些」。失败摘要为空串，连异常类型名都不回传：Provider 内部信息不进主 Agent 的 context。

D3 的落法是：子 Session 不各自写 RunEvent，全部收敛成主 Session 上的**一个** ToolCall + Evidence。
这样 AGENTS 的「Run 内 sequence 严格单调增」不必改成分层序号，SSE 契约一行不动。

单 branch 超时用 `subagent_branch_timeout_seconds`（默认 300，必须短于 Run 的 900 秒 wall timeout），
避免一路停滞吃掉整个 Run 的期限。

### 45.5 §24 生成模块静态检查：采用字句规则的安全取向

按 `docs/07` §11.2 的六类逐项实现（动态求值 / 直接网络 / 宿主上下文 / 外部 URL / 动态 import /
持久存储）。判定用**字句而非语法**：解析生成的 TypeScript 需要引入 parser，那本身既是新依赖又是新攻击面。
字句匹配会过检出，但**误判方向是安全的**——过检出只是让生成重来一次，而漏判是把危险的东西放行。
两者不对称，所以取过检出。

同时明确：**通过不等于安全**。`docs/07` §11.2 自己写了「静态检查不是唯一安全边界」，运行时 iframe、
`CSP: sandbox`、backend 权限各自独立生效。

依赖侧（D5）除白名单外还挡两样：**非 registry 指向**（`git+` / `github:` / `file:` / `link:` 等——
名字在白名单里但指向别处，内容就可以是任何东西）与 **lifecycle script**（在 install 阶段就跑任意代码，
冻结 lockfile 挡不住）。

### 45.6 §24 版本冻结与「无法配信的版本不叫已发布」

`frontend_module_versions`（migration `0026`）把 source/lockfile/bundle 三个 hash、Module API 版本、
**配信时 CSP**、静态检查与构建报告冻结在一行。DB CHECK 直接禁止「没有 bundle 却是 PUBLISHED」。

把 CSP 存进版本行是 D2 的直接后果：D2 用响应头换掉了独立 Origin，header 一旦落掉隔离就消失，
所以它不能只是配信侧的配置，必须随版本一起冻结、可复原、可核对。测试断言该值以
`sandbox allow-scripts;` 开头、不含 `allow-same-origin`、含 `connect-src 'none'`。

状态机上「**DISABLED 不可复活**」：平台无法判断停用原因是否真的消除，修好的东西作为新版本重建即可
——源变了 hash 就变，本来就是另一行。

### 45.7 验证

- `ruff` / `mypy strict` / `pytest`：**858 passed, 13 skipped**（§43 后为 783 / 13）。新增 75 项。
- 新增测试的重点全在**平台给子 Agent 什么**（能力集、预算、失败边界）与**静态闸门是否逐项覆盖文档**
  ——这两类都能离线确定性验证；模型是否善用扇出属 A 类残余。
- capability catalog checksum 三处同步（catalog example / interpreter request example / importer 测试），
  system Skill 升 **3.4.0** 并重算 identity；`validate_contracts` 76 schema・57 example 通过。
- **残余**：§23 P3（子 Session 行 + 新 continuation mode + 三语）未做；§24 M2b（构建沙箱 service）
  待内部 npm 镜像地址；M3/M4 待威胁模型（D3）。
## 46. 扇出结果的 Web 呈现与仓库级 RV（2026-07-26）

### 46.1 扇出的呈现风险不是"不好看"，是"部分覆盖被读成全面确认"

§23 落地后，扇出结果在 Web 上只散在两处：工具调用行的原始 JSON（`JSON.stringify(arguments_summary)`），
和折叠证据里的 excerpt——而证据的 `metadata`（分支会话、每路预算）**被整个丢掉**。

真正的问题在于扇出这项能力的固有性质：**一路失败也不阻断整体，摘要照常返回**。如果 `FAILED` 和
`COMPLETED` 长得一样，读者会把「三个面查了两个」当成「三个面都查过」——结论的根据缺了一块而没人察觉。

因此新增的 `SubagentDispatchSection` 围绕这一点设计：

- **不放进折叠的备查区**。扇出的完整性会改变结论的读法，不是事后追溯的材料。
- 有未完成的面时，**整节转 warn 色**并在顶部直接写出「以下方面没有查完，结论里不包含它们：…」。
- 完成/未完成/超时/取消用不同色的徽标（`branch-*` 复用 Run status 的 token 体系）。
- 每路的会话 ID 只显示短码、全文退到 `title`——它是追溯用的，不该挤掉结论。
- 全部完成时不加任何强调。常时强调会让警告失去意义。

数据只从证据 `metadata` 读，画面侧不重算；`collectSubagentDispatches` 对契约外的值（`null`、字符串、
缺字段）逐项跳过而不抛异常——metadata 由 Provider 写，画面不该因为它的一处异常而整个白屏。

### 46.2 仓库级 RV：机械扫描 + 两处真死代码

三语 catalog **574 键完全一致**；`src` 内 0 个未引用 export、0 个未引用 i18n key。

backend 扫出 23 个"从未被引用的公开名"，其中 21 个是 route handler（经装饰器注册，属误报）。剩下两个是真的：

- `agent/binary_text.py:is_supported_spreadsheet`——自带「旧名。新規は `is_textualizable` を使う」注释、
  零调用。**带着"别用我"注释的死代码**比普通死代码更该删：它会让读者以为存在两套判定。
- `effects/domain.py:ProposalChange`——已被 `ChangeProposalDraft.changes: tuple[dict, ...]` 取代，零引用。

### 46.3 ResourcesPage：把画面无关的纯逻辑放回 `src/lib/`

AGENTS 规定「画面非依存の純 logic は `src/lib/` に置く」，但四个 tab 的草稿类型、既定值与
`secretInputFromDraft` 一直留在页面里（1341 行）。抽到 `lib/resourceDrafts.ts`（146 行）后页面降到 1219 行。

抽出后立刻能给它写测试——这是搬动的真实收益，不是行数。新测试钉住两条安全性质：**MANAGED 只送明文
不送 locator，其余 resolver 只送 locator 不送明文**（混在一起会让「明文不落库」的前提在 server 侧变得
含糊），以及**仓库连接的默认权限是只读**、**默认 resolver 是 MANAGED**（默认成 ENVIRONMENT 会把用户
推向"把明文写进 .env"的运维方式）。

**仍未做**：四个 tab 的状态就地化。它们共用 `mutate` / `error` / `busy` / `revision` 与一次五路重载，
拆开要么把 mutation 管道复制四份，要么引入一个共享 hook 把耦合藏到更不显眼的地方。这是权衡后的选择，
不是遗漏。

### 46.4 验证

- backend `ruff` / `mypy strict` / `pytest`：**858 passed, 13 skipped**（删死代码后件数不变）。
- web `tsc` + **245 tests** + `vite build` 通过（§45 时为 236）。新增扇出呈现 4 项、草稿逻辑 5 项。
- 三语 574 键一致；0 未引用 export / i18n key。

## 47. 本地环境补齐、扇出子 Session 持久化与构建退避的封堵（2026-07-26）

本轮做完了当前开发环境里所有还能做的实现与优化，共三件。第一件推翻了此前记在文档里的一条环境结论。

### 47.1 「13 个 skip 来自本容器无 PostgreSQL」是可以消除的，不是既定事实

`docs/01` §13.2 与 §19 那行都把 13 个 skip 当成外部约束在记。实测：容器有免密 `sudo`、apt 源可达，
装的只是 `postgresql-client-14`（`/usr/lib/postgresql/14/bin/` 无 `postgres`/`initdb`/`pg_ctl`），
redis-server 全缺。装回 `postgresql-14` + `redis-server` 并手动拉起（容器无 systemd）后，13 个 skip
全部转为真跑。

真正的收益不在件数，而在 **migration `0025`（task_schedules）与 `0026`（frontend_module_versions）
至今没有在任何真 PostgreSQL 上执行过 DDL**。这两个 migration 里的 CHECK 约束与部分索引，是内存测试
和 metadata 断言都验不到的东西——`test_module_versions.py` 验的是 `require_publishable()` 这个 Python
函数，`test_alembic.py` 验的是列名在不在。全链 `alembic upgrade head` 一跑，两者才第一次被 PostgreSQL
真正接受。

**跑起来立刻暴露了一个只有真 FK 数据库能发现的夹具缺陷**：`test_0020_interactive_run_constraints_are_enforced`
用一次 `add_all` 插入整条 Run 骨架，但 `run_segments.parent_agent_session_id` 与
`agent_sessions.run_segment_id` 互指，table 层 FK 构成**环**。有环时 SQLAlchemy 无法位相整列，
`add_all` 的 INSERT 顺序变成不定；实 PostgreSQL 即时检查 FK，顺序一错就违约。生产路径不受影响
（`_ensure_agent_session` 引用的是已 claim 的 Attempt，不存在一次性批量插入），因此改的是夹具：
新增 `_insert_in_order` 按依赖顺序逐行 flush，并把「为什么不能用 `add_all`」写在函数注释里。

### 47.2 §23 扇出返回的 `agent_session_id` 持久化修复

`subagent_provider.py` 为每个 branch 生成 `uuid4()` 放进 response 与 Evidence，但从不落库——
`AgentSession` 行只在 `repository_base.py` 的主 Run 脊柱上创建。而 response schema 把
`agent_session_id` 列进 `required`，D3 也写明「每个子 Session 另存 AgentSession 行供审计」。
两边都承诺可查，实际是**悬空引用**。扇出的收敛设计把子 Session 的 event 全折叠进主 Session 的一次
ToolCall，那 `AgentSession` 行就是唯一的追溯锚点，它不存在等于这一路没有审计面。

落地时先撞上 schema：`agent_sessions` 的两个一意约束（Attempt 一个 / Run 一个 ACTIVE）都没有限定
对象，而子 Session **共用父的 Attempt** 且**最多 4 本同时 ACTIVE**，一落库就违约。

migration `0027` 的处理是**把约束收窄到 PRIMARY，而不是解除**——解除会让「一个 Attempt 有两个本体
session」也通过，守着的不变条件就此消失。同时用 CHECK 把 `session_kind ∈ {PRIMARY, SUBAGENT}` 和
`(continuation_mode = 'BRANCH') = (session_kind = 'SUBAGENT')` 固定，避免只名一半导致审计读不出
「为什么同一个 Attempt 下有多个 session」。

**`sdk_session_id` 改为 SUBAGENT 可空**。分支在 SDK 开会话前就失败时，列若保持 NOT NULL 就只能
**捏造一个 ID**——那正是本工作包在消除的缺陷（看起来能查、实际查不到）。PRIMARY 侧的 NOT NULL 由
`ck_agent_sessions_primary_has_sdk_session` 维持。Provider 相应改为从 branch 的 event 里捕获引擎实际
开出的 SDK session id，失败路若已开过会话就保留那个真实 ID。

一组 branch **一个 transaction 一次写完**。部分写入会留下缺了失败路的记录，让「只看了两个面」读成
「两个面都看了」——与 §46 P3a 消除的误读同型，保存侧不再制造一次。保存失败时不让 dispatch 失败
（结论已得，丢弃更贵），但**省略 `agent_session_id` 字段**而不是返回一个查不到的值。

- 新 continuation mode `BRANCH`：与另外四个不同，它表示的不是「如何继承前一段会话」而是**并行存在**。
  刻意**不**加入 interaction / effect 的继续 enum——用户不能选择以 BRANCH 方式继续。
- Web 时间线按 lineage 分组：`sessionsCount` 只数主分析（平坦排列会把「主分析 1 本 + 并行 4 本」读成
  「顺序会话 5 次」），子分析缩进挂在父下，未完走的一路警告色区分。父不在本 Segment 的孤儿子分析
  **保留为独立行**——从审计记录里悄悄消失比排版难看更糟。
- 三语补 `continuationMode.BRANCH` 与新目录 `sessionKind`；顺带补上 `sessionStatus` 一直缺的
  `ACTIVE`/`IDLE`/`CLOSED`/`INTERRUPTED`——子会话常态就是 CLOSED/INTERRUPTED，缺译会在三语界面直接漏出英文。

### 47.3 §24 构建拒绝路径不依赖镜像地址

D4 是「内部 npm 镜像未提供时构建拒绝、不回退公网」。等地址的是构建成功路径；**拒绝路径恰恰以
「没有地址」为触发条件**。此前 `settings.py` / `compose.yaml` / `.env.example` 里 npm/registry
一个字都没有，也就是说这条保证还只写在计划里，代码侧不存在。

`modules/build_plan.py` 的核心判断是：**只查配置不够**。凍结 lockfile 的 `resolved` 若指向公网
registry，无论镜像怎么配，取的都是公网那份。所以「配了镜像所以安全」不成立，必须同时看 lockfile。
拒绝一律抛异常而非返回值——返回值让调用方漏检时构建照跑，把「消除退避」这件事变成依赖调用方的注意力。

「是否内部镜像」无法判定（内部地址是待提供的外部输入），能做的是**拒绝已知公开 registry** 与
**拒绝未配置**，这两条即覆盖 D4 的「不回退」。

**`module-builder` Compose service 未加**，仍等镜像地址：加一个跑不起来的 service 会出现在
`docker compose up` 里，比不加更糟；同理未加 settings 字段——按仓库同步规约，设置项要有消费方，
现在没有消费方，加了就是悬空旋钮。等 M2b 正式开工时与消费方一并加入。

### 47.4 验证

- backend `ruff` / `mypy strict`（155 文件）/ `pytest`：**899 passed, 0 skipped**
  （本轮起点 858 passed + 13 skipped；PG 补齐 +13、P3b +12、M2b +9、真库新增 5）。
- web `tsc` + **248 tests** + `vite build` 通过（§46 为 245；新增时间线分组 3 项）。
- 仓库级 `validate_contracts` / `validate_compose` / `probe_claude_agent_sdk` 通过；
  OpenAPI 已重导出（LF，纯增量）。
- 残余仍是三类：部署验收（§19.5 两个资源验收层、解释器实机越门与过程重表达模型行为、§20 端到端落地）、
  §24 M2b 的 Compose service 与 M3/M4（等镜像地址与威胁模型 D3，责任人待定）。

## 48. 全仓文档与代码 RV：契约守护补强、凭据整治与文档漂移对齐（2026-07-27）

### 48.1 契约 example 的「双表登记」已经漂移，且有一个 example 两表皆无

AGENTS「同期点」一直警告：example 只有登进 `scripts/validate_contracts.py` 与
`backend/tests/contracts/test_contracts.py` **两张表**才有人验，"片方だけだと example は誰にも
検証されないまま通る"。实测这件事已经发生：磁盘上 63 个 example，validate 侧缺 6 个
（auth 三件、workspace.write 两件、generic-native-manifest），test 侧缺 5 个（run-detail、
run-history、两个 event example、generic-native-manifest）——其中
`generic-native-manifest.v1alpha1.json` **两表皆无**，处于「谁也不验」状态。

处置分两层：10 个漏登记全部补入两表（事前逐一验证均通过对应 schema，纯增量登记）；更重要的是
把「登记完整性」本身变成可执行守护——validate 脚本对磁盘上未登记的 example 直接 fail closed，
测试侧新增 `test_every_example_on_disk_is_registered` 断言「磁盘 ⇔ 表」双向完全一致。今后
漏登记当场红，不再沉默通过。

### 48.2 凭据入库两处：工作副本已处置一处，轮换与历史清理待管理员

- `skills/jaf-quality-core/SKILL.md` 的 `connection_settings.json` 结构示例里，
  `redmine_api_key` 用 x 掩码了，`svn_user` / `svn_password` 却是**实值**。工作副本已改为
  placeholder，并在 `skills/README.md` 写明该目录「接続先の実凭据は置かない」。该实值已随
  历史提交进入 SVN。
- `PJM/.env`（r175620）整体在版本管理内，含 `POSTGRES_PASSWORD`、`MINIO_ROOT_PASSWORD`、
  `PROJECTMIND_MANAGED_SECRET_KEK`、`ANTHROPIC_AUTH_TOKEN` 实值。README 与 `docs/09` 均禁止
  实值提交；KEK 与数据库同处一个可导出的仓库，正是 `docs/09` §7.2 要求避免的形态。
- 残余处置（需仓库管理员执行，本轮不做版本库操作）：`svn rm --keep-local PJM/.env` 并加
  `svn:ignore`；轮换上述全部凭据与 jaf 的 svn 密码（KEK 轮换走 `ops/rotate_secrets.py` 的
  keyring 流程）；条件允许时对 SVN 历史做 dump/filter 清理。

### 48.3 共通实装与规约补齐（全部行为等价或纯追加）

- `agent/task_brief.py` 的 `_compact_json` 与 `core/hashing.canonical_json` 输出逐字节相同
  （后者仅多 `allow_nan=False`，JSON 来源数据不含 NaN），按「共通実装の利用規約」收敛到单点。
  Brief 冻结 checksum 不变，由既有 checksum 测试钉住。
- `worker/settings.py` 的 `on_event` 闭包与 tests 下 40 个 helper（fake/closure）补日文
  docstring；web `lib/resourceDrafts.ts` 补 5 处 JSDoc。src 与 tests 的 docstring 缺失现为 0。

### 48.4 文档与实装的漂移对齐

- 0027 之后「同一 Run 只许一个 ACTIVE session」的正确表述是 **PRIMARY 限定**：`docs/04`
  （§4.2/§4.4/§5.3，字段表补 `session_kind`）、`docs/11` §8.2 与 AGENTS「信頼性不変条件」
  已统一；AGENTS 里「P1/P2 実装済み」这类进度复制改为指向 §23.3 正本——复制的那刻就开始腐化，
  本轮它自己成了例证。
- `docs/11` §14 的三项非目标标注其后由 §22/§23/§24 交付或定案的去向；§8.1 错字「墈→增」。
- README：対象外行移除已交付的并行 multi-agent，新增扇出能力条目；`skills/README.md` 补
  `examples/unsafe-credential/` 与 `jaf-quality-core/` / `jaf-quality-ticket/` 的定位说明。
- `docs/13` Roadmap 停在 07-26 上午：图例与看板写「§23/§24 实装未开始」，明细表 §20 徽章
  「未开始」但边界文字已写 R1–R5 完成，且只有 15 条主线。已整体对齐到 §13 的 07-26 收盘状态：
  §23 移入「本地完成」列、§24 独占「规划中」、明细表拆出 §22/§23/§24/§25 四行（18 条）、
  部署残余卡补 §23，并清掉「本容器无 PostgreSQL」「13 个 skip」两处已被 §47.1 推翻的旧说法。

### 48.5 验证

- 本容器按 §47.1 复原 `postgresql-14` + `redis-server`（手动拉起；role `projectmind`
  CREATEDB + `projectmind_test`）。
- backend `ruff` / `mypy strict`（155 文件）/ `pytest`：**900 passed / 0 skipped**
  （§47 为 899；新增 example 登记完整性守护 +1）。
- web `tsc` + **248 tests** + `vite build` 通过（与 §47 持平）。
- 仓库级 `validate_contracts`（76 schema / **63** example，此前实际只验 57 个）/
  `validate_compose` / `probe_claude_agent_sdk` 通过；OpenAPI 与导出脚本逐字节一致
  （无公开 endpoint 变更，未重导出）。
- 本轮全部为文档、测试、注释与等价重构：无 DB model、契约 schema、公开 API 变更，无 migration。

## 49. 服务器部署基线验收（2026-08-04）

本节记录当前服务端部署状态；前面各节按历史时点记录的残余不改写。

### 49.1 镜像与运行时

- `images/projectmind-images.tar` 已在服务端存在，大小为 `222363648` bytes，SHA256 为
  `e7db620fa726e2dd154ee0beadf6dcc382e47a065ce51ae48569dd5ca138fffc`，与本地归档一致。
- 服务端实际加载的 `projectmind/backend:0.1.0` 与 `projectmind/web:0.1.0` 的 image ID、创建时间和归档内
  config 一致，说明当前服务使用的是这份打包内容。
- Compose 服务 `api`、`web`、`worker`、`postgres`、`redis`、`object-storage` 正常运行；`migrate` 与
  `object-storage-init` 均以退出码 `0` 完成。

### 49.2 应用与后台基线

- Web 健康检查返回 HTTP `200`；页面静态资源指纹与归档内 Web 镜像最终层一致。
- OpenAPI 为 `0.1.0`，共 `54` 个 path、`72` 个 operation；与本地契约的 method/path 集合完全一致。
- `preflight` 返回 `ready`；PostgreSQL migration head 为 `0028_skill_source_file_index`，Redis 检查通过。
- Worker 的近期 `schedule.tick.completed` 日志为成功状态，`failed=0`。这证明 tick 正常执行，但尚未证明
  真实 schedule 的触发、重叠跳过和 `missed_count` 处理。

### 49.3 尚未由本次部署基线覆盖的验收

- 应用级认证 smoke：仍需要应用管理员、Project UUID、已发布任务和输入等业务数据；本次只提供了服务器登录
  凭据，因此不使用未知的应用凭据，也不写入或修改业务数据。
- 部署镜像不包含源码、测试和契约目录，9 项真实数据库 invariant 测试未在服务端执行；migration head 与
  `preflight` 只能作为数据库连通和迁移基线证据。
- 真实 HTTPS Git/SVN、Redmine/forge 端到端落地、模型行为与质量、多 Agent 实际模型效果、真实 schedule
  发火，以及浏览器三语/键盘/窄屏验收仍需专项环境和业务数据。

## 50. 文档体系重整与设计核对（2026-09-05）

### 50.1 阅读与维护入口

- 文档按 overview、design、planning、development、operations、acceptance、history 分类。workspace 与
  PJM 的 README 负责入口，Backend/Web/Contracts/Skills/Images 的 README 负责各自代码导航。
- 原统一 PLAN 正文与手工 Roadmap 图保留为有日期的历史快照。现行计划只维护范围、顺序和验收缺口，
  §13 为状态正本；旧文档编号继续通过索引定位，不批量修改代码中的编号注释。
- Runtime 的资源快照、仓库受控写入、子 Agent、TaskSchedule，以及后续 Task Flow/生成模块各有独立规范；
  增加产品概览、术语、设计到代码映射、开发与运维操作入口。
- 新增可离线打开的 [文档浏览版](../index.html)：分类导航、正文搜索、历史筛选、页内目录、深链接、
  返回/前进、打印和窄屏菜单。Markdown 是来源，HTML 由脚本生成，不手工维护第二套正文。

### 50.2 设计纠偏与未完成边界

- 按实际契约修正领域载体、SkillVersion/readiness enum、AgentEngine 签名、ViewSpec 示例、项目显式启用、
  交互权限、来源导入能力及已有 Git/SVN/调度/workspace/子分析能力的过时描述。
- 明确 document 当前为 Project 全集、首次准备时物化，不是创建 Run 时按绑定冻结；提出文档集合与内容
  hash 的冻结设计。子 Agent 仅单次 dispatch 分配预算，没有 Run 共享扣减账本；修正要求进入优先计划。
- 说明 Schedule 认领推进与 Run 创建的事务间隙、重叠查询的并发局限，以及 commit 与 PR 不跨系统原子完成。
- 生成模块保留同主机专用路径与 opaque-origin 决策，补充 Cookie/CSP/Host 消息与构建网络约束；
  静态拒绝不证明代码可安全执行，首次执行前仍需威胁模型和跨层验收。
- 运维入口区分不清库的 ADMIN CLI 与清除全部 volume 的 make target；backup/restore 补充 blob、workspace
  一致性及历史备份所需旧 KEK 的保留要求。
- 本次未修改应用业务代码、API、Schema、migration 或已发布 Skill 的 SKILL.md/references；上述运行缺口
  只是完成定位和修正设计，不能视为代码问题已解决。

### 50.3 本次验证与未覆盖项

- 文档 build/check：34 份 Markdown 的标题、代码块、本地文件/章节/浏览版链接，以及 1 份 ViewSpec 示例通过。
- 文档工具 10 项 unittest 通过；工具与测试的 Ruff 检查/格式检查通过。覆盖无效引用、代码块闭合、
  中文/重复标题、原始 HTML 不执行、嵌入范围与可复现构建。
- 契约验证：76 份 Schema、63 份 example 通过；没有改写契约来适配文档。
- 独立 Chromium 离线验证：34 页桌面/手机浏览、搜索、历史筛选、深链接、返回、菜单/Escape、打印样式、
  无效页面回退及 3 份独立 HTML 正常；无 JavaScript 错误、无 HTTP(S) 请求。
  入口与结构图另在 320/375/768/1024 宽度检查页面溢出，修复技术图长标签撑破窄屏的问题。
- 未重跑 Backend/Web 全量测试、真实 DB、Compose 部署、SDK 模型调用和业务 UI 验收：本次影响范围为
  文档与独立文档工具，未改变应用逻辑；环境无 Docker。这里的浏览器验证仅针对文档，不能抵消 §49.3 的专项残项。

## 51. 文档设计边界与阅读体验续整（2026-09-08）

### 51.1 内容与导航

- 实施计划的当前状态、下一步和全项目登记前置，旧章节降为引用索引，保留既有锚点与代码编号入口。
- 产品页增加按目的的阅读路径，架构页增加规则归属表；变更指南补上冻结时点、失败/幂等、兼容和验收问题清单。
- Backend/Web README 增加具体调用链和页面入口；Contracts 区分内部 field 与公开协议。新增 Scripts README，按只读验证、生成覆盖、模型调用与配备副作用分类。
- 文档维护约定加入设计骨架与更新顺序。Task Flow 改用独立、连续的章节层级，实施阶段不再复制状态表。
- 浏览版增加窄屏/中等宽度的“本页内容”、键盘章节跳转，以及按实际溢出显示的表格滚动提示。Markdown 仍是唯一正文来源。

### 51.2 设计核对与保留的差距

- 资源规范分开授权快照、内容快照、物化副本，明确文档单份/集合/显式全集、槽位并集、创建/准备/读取时序、缺失与篡改、缓存复用和旧 Run 兼容。
- 工作副本已有 document ID/hash 清单改动，但集合选择 UI、公开快照、创建重放与既有回归尚未贯通；没有标记为完成或部署通过。
- 仓库分支名/HEAD 不等于创建时固定 commit；首次物化与额外 live Evidence 的版本分别解释。物化器的逐根额度不再描述成所有资源共用的 Run 总量保证。
- Runtime 区分“必需资源未绑定时拒绝创建”与“已有 scope 内缺少事实时澄清”，避免通过对话扩展冻结权限。两张架构图同步修正 Segment/Attempt/Brief 的创建时点。
- Task Flow 新增节点事实来源：批准不等于执行、动态 STEP 不自动证明质量规则完成、无关联不补造进度，旧 Run 不拼接最新计划。
- JAF 建议目录将可导入 Skill、执行资源、独立评价存储分开，Gold/Rubric/expected 不进入 Agent 可见面。删除固定 20 脚本/Native seed 等历史门槛，补充计分分母、失败 case 和实际 Evidence 支撑的判定规则。
- Runbook 修正旧“禁止 SVN write”“所有 ACTIVE Session 同时唯一”说法，补充按症状导航和资源排障。API 指南不再示范把真实 password 放到 command 行。
- 按项目规范保留可执行 Skill 的 SKILL.md/references，不改变 version/hash。没有修改应用业务代码、Schema、OpenAPI、DB migration、部署配置或正在进行的 R01 代码。

### 51.3 验证范围

| 检查 | 本次结果 |
| --- | --- |
| 文档 build/check | 35 份 Markdown，1 份 ViewSpec 示例，本地文件/章节/浏览版链接通过 |
| 文档工具回归 | 12 项 unittest 通过；新增现行标题跳级拒绝与历史结构保留测试 |
| 文档工具 Ruff | check 与 format check 通过，使用 Backend 的现行配置 |
| 契约 | 76 Schema / 63 example 通过；Backend contracts 测试 7 passed，含 OpenAPI 一致性 |
| Compose 静态检查 | 8 service 与共享 Traefik 边界通过；没有启动容器 |
| 文档浏览器 | 离线 Chromium：35 页桌面/390px 手机、搜索/历史过滤、深链接/刷新/返回、键盘目录、打印、无效页回退；3 份独立 HTML 通过 |
| 额外窄屏检查 | 产品、计划、资源、Task Flow、Runbook 在 320/375/768/1024px 无整页溢出；表格滚动提示与实际溢出一致 |

浏览检查没有 JavaScript 错误或 HTTP(S) 请求。本轮未执行应用全量 Backend/Web 测试、真实 DB、模型、服务器部署或业务页面验收；不能用本表替代计划中的实现/专项验收。原有 Web build/cache 未因文档工作被删除。

## 52. 预算、生命周期与文档工具续整（2026-09-08）

### 52.1 设计与工程入口

- 新增[Run 预算与执行限额](../design/run-budgets.md)，作为主 Agent、子分析和恢复共同遵守的设计正本；原子分析预算章节保留链接入口，不再单独维护一套协议。
- 对照实际代码区分 SDK 单次 turns/美元上限、单 Tool 响应字节、准备后 engine deadline、Segment 内技术重试和逐根物化额度。dispatch 的返回分配值不当作实际消耗，未知成本不当作零。
- 共享预算设计补上计量维度、账户/预留、锁顺序、幂等、主子额度不能重复承诺、取消与崩溃后的不确定用量、晚到结算及历史 Run 兼容。该协议仍需 R02 实现，不是已可调用的 API。
- 领域模型新增 Run 状态速查，修正 RETRY_PENDING → PREPARING，补上等待时 Attempt 的 DEFERRED。Runtime 示意分开 Segment/Outbox 持久化与 Worker 冻结 Brief，不再把所有 timeout 表述为自动重试。
- Runtime Tool 清单区分调用前检查与返回内容检查；预算硬限制和全局精确一次不由局部检查推导。
- Backend/Web README 增加常见修改的文件阅读顺序，Contracts 解释 Schema 与内部 JSON/设计语义的区别；AGENTS 同步 i18n 的 `UiMessages` 与 zh/ja/en 文件位置。技术结构图同步 Brief 和局部限额说明。
- 导航、术语、变更指南和计划全部指向新的预算正本。README/AGENTS 保持日文，设计正文保持中文；执行 Skill 的 SKILL.md/references/version/hash 未修改。

### 52.2 文档工具与阅读体验

开始核对时，工作副本的 `index.html` 已过期，现有 12 项文档工具回归报告 4 个失败、1 个错误。历史 §51 记录不作为这份工作副本已经通过的证明，本轮重新修复和验证：

- 使用 Markdown parser 的引用/list 上下文验证代码块闭合，拒绝空的未闭合 fence、错误缩进和短闭合符号，不误拒合法引用块。
- 现行文档拒绝标题跳级；归档保留当时标题层级。
- 浏览版深链接即使在首次构建时也检查文档 ID 与章节，不能因 index 尚不存在就跳过。
- 中等/手机宽度提供可折叠本页目录；章节跳转聚焦目标标题，移动目录 Escape 返回按钮。
- 长表格保留可读列宽；仅实际溢出时在表格前显示提示并开放键盘滚动，打印时不显示导航/提示。

### 52.3 本轮验证范围

| 检查 | 结果与边界 |
| --- | --- |
| 文档 build/check | 36 份 Markdown、1 份 ViewSpec 示例，本地链接与章节校验、生成物一致 |
| 文档工具回归 | 13 项 unittest 通过，含新增的嵌套/隐式代码块闭合边界 |
| 文档工具 Ruff | Backend 配置下 check 与 format check 通过 |
| 通用契约 | 76 Schema、63 example 通过；未修改公开数据形状 |
| 相关 Backend 回归 | contracts 与 Run domain 合计 38 passed；不代表整个应用已验证 |
| 离线文档浏览器 | Chromium 在 1440px/390px 遍历全部 36 页；另对 6 个关键页面检查 320/375/768/1024px，无整页溢出 |
| 导航与操作 | 搜索/历史过滤、章节深链接/刷新/前进/后退、手机键盘目录、无效页回退、打印样式通过；2 张架构图在桌面/手机打开 |

浏览器未发现 JavaScript 错误或 HTTP(S) 请求，并查看了桌面/手机截图。测试依赖放在 workspace 外临时目录，未创建 venv 或清除原有应用产物。

本轮没有修改应用业务代码、Schema、OpenAPI、DB migration、执行 Skill 或部署配置；没有执行完整 Backend/Web、真实 DB、模型、服务器/Compose 或业务 UI 验收。R01/R02 等代码缺口仍按计划登记，不能以文档工具绿色结果标记完成。

## 53. R01 冻结文档读取与物化接入（2026-09-08）

### 53.1 实现与覆盖

- `WorkspaceMaterializer` 接受 ContextBuilder 已验证的 document snapshots，不再因为 Blueprint 声明 document 就枚举 Project 全集。必需槽位缺失拒绝，可选未选不读取；多个槽位物化去重并集，manifest 保留各自模式和成员关系。
- Inventory 内容必须与冻结 ID 集合及实际字节 hash 一致。删除、同路径换 ID、伪造 metadata/字节不当作 skipped；合法二进制/超大文件保留原 skip 语义。
- 文档 Provider 的测试同步为真实冻结 context；未选路径不查询 Source，缺失/变化的原文不可替换。额外校验 RunAttempt/actor 身份，Evidence locator 包含 document ID。
- 缓存加入版本、Project/Run 与资源身份；文档校验成员覆盖与原文 hash，生成索引根据清单重建比较，实际树拒绝额外文件/目录、缺失和特殊文件。缺 manifest 的目录保留现场，不静默覆盖重建。
- 物化文件 I/O 抽到 `materialization_storage.py`：逐级 directory descriptor、不跟随 symlink、只新建文件、限制读取字节并拒绝 FIFO/device/hardlink，避免路径解析后再打开的边界漂移与单次 os.write 的短写风险。
- 既有 Provider/ContextBuilder/物化/本地 Git/SVN fixture 接口同步，并新增多槽位实际 ContextBuilder → 物化器 → Brief 与 retry 的连通测试。

### 53.2 本地验证

| 检查 | 结果 |
| --- | --- |
| Backend 全量 Pytest | 934 passed、18 skipped；跳过的是 PostgreSQL 不可达的真实 DB invariant 测试 |
| Backend Ruff / Mypy | check 通过；158 个 source 文件类型检查通过 |
| 通用契约 | 76 Schema、63 example 通过，含全量 Pytest 中的 OpenAPI 一致性检查 |
| SDK offline probe | SDK 0.2.110 / CLI 2.1.191 的 options、MCP、interrupt 与 SessionStore 接口检查通过 |
| Compose 静态检查 | 8 service 与共享 Traefik 约束通过；未启动容器 |
| 文档 | 更新资源规范、Runtime、变更指南与 R01 状态，并重新 build/check 浏览版 |

未执行真实数据库、真实资源/模型、Web 或部署专项；没有 Docker。此次没有修改公开 Schema/前端/Skill 执行资产，也没有操作未知生产数据。Python 依赖和 Mypy cache 置于 workspace 外，不创建 venv。

### 53.3 未完成范围

R01 未完成：集合选择 UI、公开快照/错误展示、ALL 成员变化后的创建幂等、历史 Run 升级处置、跨根总量与完整缓存来源验证仍需继续。当前派生文件/history 的 hash 与 manifest 共处文件系统，不能声称已阻止同时改写二者的篡改；仓库 binding checksum/scope 缓存再验证也尚未补齐。

该次回归证明后端读取/物化的接入及列出的负向边界，不证明全项目目标完成。R02–R13 继续按[全项目工作登记](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)推进。

## 54. 创建、调度与开发入口续整（2026-09-08）

### 54.1 文档与设计

- 本轮用户请求为继续整理文档。只读核对 API、Run 创建/hash/事务、文档选择、调度时间/认领/回写与对应测试，未继续修改业务实现。
- 新增[Run 创建与幂等](../design/run-creation.md)：区分稳定创建意图与首次执行快照，说明键作用域、当前授权、原请求身份、并发胜者、未知提交结果与旧 hash 兼容。修正设计仍待 R01 实施，不因增加文档就声明重放缺口已解决。
- [TaskSchedule](../design/task-scheduling.md)按“速览 → 当前链路 → 故障窗口 → 修正要求 → 验收”组织。明确三个事务、旧 occurrence 的迟到执行、missed 截顶、配置竞争、自身重叠与 run_count/max_runs 的含义。竞争风险来自代码推导，不冒充实 DB 故障注入结论。
- 资源设计拒绝重复文档 ID，与当前 parser 一致；创建身份细则集中链接新页面。Runtime、领域模型、术语、架构、API/变更指南与 Runbook 分别链接各自负责的规则。
- 文档导航新增状态阅读说明；Backend/Web/Contracts/工程 README 同步阅读链路，AGENTS 将过长不变量段拆为可扫描条目，保留安全边界。执行 Skill 的 SKILL.md、references、版本与 hash 未改动。
- 实施计划保留 R01–R13 和旧章锚点，§13.4/§13.5 的重复核对表改为历史入口；明确本轮文档请求不自动授权其他业务改动。旧交付正文不倒改。
- 浏览版新增核心页顺序；文档工具增加推荐顺序不可重复/不可指向缺失文档的回归。
- 实际截图发现窄屏章节标题被固定顶部栏遮挡；浏览模板改为按顶部栏实际高度设置滚动留白和菜单起点，并随 resize/顶部栏尺寸变化更新。增加目标标题位置的浏览器断言，不仅检查 focus。

### 54.2 本轮验证

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 37 份 Markdown、1 份 ViewSpec 示例；本地链接、章节与生成物一致性通过 |
| 文档工具 | 14 项 unittest 通过；build/tests 的 Ruff check 与 format check 通过 |
| 通用契约 | 76 Schema、63 example 通过；本轮未修改公开契约 |
| 相关 Backend 回归 | 121 passed：Run domain/创建服务、document snapshot、调度时间/计划/API/tick、contracts；不是全量应用验收 |
| 离线浏览布局 | Chromium 1440px/390px 遍历全部 37 页；6 个重点页面另测 320/375/768/1024px，无整页横向溢出 |
| 章节可见性 | 320/390/760/768/1440px 检查创建与调度页的深链接/刷新、普通及放大正文；标题位于固定栏下方且完整可见 |
| 阅读操作 | 工程入口/文档导航、正文搜索与历史过滤、前进/后退、旧 §13.4–§13.6 锚点、手机键盘菜单/章节、打印与无效页回退通过；两张架构图桌面/手机可打开 |

浏览器无 JavaScript 错误或 HTTP(S) 请求，已查看桌面和手机截图。依赖、浏览检查脚本与备份位于工作区外临时目录，没有创建 venv、新增仓库缓存或清除用户已有应用产物。

未执行全量 Backend/Web、真实 PostgreSQL 并发/故障注入、真实资源/模型、Docker/服务器部署或业务 UI 验收。本轮结果不能证明 Run 幂等或调度修正已实现，相关验收责任继续保留在 R01 / R09。

## 55. 工作副本对齐与开发交接文档续整（2026-09-08）

### 55.1 范围与设计修正

- 本轮用户请求为继续整理文档。保留已有代码改动，只读核对创建请求/兼容、API/服务/repository、调度和 Web 提交流程；未继续编辑业务代码或测试。
- [Run 创建设计](../design/run-creation.md)反映已有的 `TaskRunIntent`、内部 versioned JSON、新旧 hash、原键先行与失败后复查，不再把工作副本已有入口写成完全未实现。现行 POST 授权与原 actor 匹配仍是前提。
- 修正省略选择的表述：可选 document 不选择不授权，Integration 仍可能使用 Task/Project default；原请求省略默认与显式 override 不合并。内部身份字段不是公开 API 字段。
- 补充客户端“草稿 → 固定提交 → 结果未知 → 原键确认”状态与隐私边界。现行 Workspace 每次提交生成新键，Backend 重放不能替代前端待确认请求；该 UI 仍待实现。
- [资源设计](../design/resource-snapshots.md)细化独立数据库回执、世代隔离候选副本、完成发布/崩溃窗口与旧缓存不补签；明确跨根总量的存量计量、生成文件、去重与临时峰值。这些是新增修正设计，没有新增表、字段或物化实现。
- [调度设计](../design/task-scheduling.md)同步已存在的原键查询/重叠后复查，同时保留三个事务、持久在途、配置版本、幂等结算及迟到差距。
- Runtime、领域模型、Workspace、术语、API/变更指南和 Runbook 同步单一正本链接。工程 README 明确实现入口、内部/公开契约和 package import 边界。
- 新增[历史目的索引](README.md)，保留旧正文与章节锚点；变更指南增加可接手任务的场景、非目标、已有基础、同步范围与证据模板。计划不再并列保留互相冲突的“本轮文档/本轮代码”授权说明。

### 55.2 工作副本的相关诊断

在 `PJM/backend/` 使用既有锁定依赖，设置 `PYTHONDONTWRITEBYTECODE=1`、禁用 pytest cache，执行：

```bash
python3 -m pytest -p no:cacheprovider \
  tests/runs/test_creation_replay.py tests/runs/test_task_run_service.py \
  tests/api/test_run_api.py tests/schedules/test_schedule_replay.py tests/contracts
```

结果：**71 passed、3 failed**。失败均来自[调度重放测试](../../PJM/backend/tests/schedules/test_schedule_replay.py)的 `if authorized`：该参数属于前一个测试，当前参数化测试没有定义它，触发 `NameError`。这说明当前测试尚未整理完成，不是已证明的生产调度故障；本轮未移动断言或修改测试来消除失败。

已通过部分覆盖创建规范化/兼容、创建服务、API 和公开契约一致性等局部行为；不能据此判定整个 R01/R09 完成。后续代码工作应先恢复可信测试，再验证完整链路及真实数据库竞争/回滚。

### 55.3 文档交付与验证范围

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 38 份正式 Markdown、1 份 ViewSpec 示例；链接、章节、代码块与生成一致性通过 |
| 文档工具 | 14 项 unittest 通过，含嵌入范围、可复现 build 和旧锚点规则 |
| 通用契约 | 76 Schema、63 example 通过；相关 Backend 测试也包含 OpenAPI 一致性 |
| 桌面/手机阅读 | Chromium 在 1440px/390px 遍历全部 38 页，无整页横向溢出；表格滚动提示与键盘可聚焦状态一致 |
| 新章节 | 5 个目标章节 × 4 种宽度（320/390/768/1440px）× 普通/放大正文，共 40 个布局场景；深链接刷新后标题完整可见 |
| 导航 | 历史索引到 §55、正文搜索与历史过滤、前进/后退、手机键盘目录、打印及旧计划锚点通过；两张架构图可离线打开 |

浏览器无 JavaScript 错误或 HTTP(S) 请求，已查看桌面和手机截图；依据手机截图将提交状态图收窄为纵向分支，不改变含义。没有执行全量 Backend/Web、真实 PostgreSQL、真实资源/模型、服务器/Compose 或业务 UI 验收；不会把历史绿色数字转记为本轮结果。

执行 Skill 的 SKILL.md/references 是 version/hash 关联的运行资产，本轮不移动或改写。公开 Schema/OpenAPI、DB migration、应用依赖与部署配置均未修改；临时依赖、备份与浏览检查置于工作区外，不创建 venv，不清除既有应用产物。

## 56. 设计导航与提交状态文档续整（2026-09-08）

### 56.1 范围与整理

- 本轮继续整理文档，保留已有业务代码和测试。本次核对发现工作副本已包含 Web 原请求确认和调度测试修正，不能继续沿用 §55 当时的“客户端未实现 / 测试失败”作为现状；也不把既有改动归为本轮新实现。
- 新增[设计阅读顺序与责任分工](../design/README.md)，按用户场景给出短阅读路线，说明各设计负责什么、哪些问题交给相邻设计。架构页转向该单一索引，避免复制同一张责任表；文档总入口、工程入口与离线阅读顺序同步。
- [Run 创建](../design/run-creation.md)区分 `sending / unknown / rejected / conflict`，明确 30 秒 HTTP 等待不是 Run 取消、原请求与草稿分离、拒绝不能抹去此前不确定结果，以及显式新建、CSRF 更新、账号/Project 切换和内存丢失的边界。
- Workspace、资源设计、调度、API/变更指南和 Backend/Web README 与当前代码入口对齐。Web README 增加 hooks 的职责；本地开发说明既有浏览器 fixture 的启动、依赖、mock 范围和结束方式。
- 计划保留完整 R01–R13，§13.8 改为当时记录的入口，§13.9 记录本次核对；未完成的集合 UI/公开快照、真实事务、可信缓存、跨根总量、共享预算和调度恢复均未判定完成。旧历史正文及旧计划章节编号保留。

### 56.2 工作副本的针对性验证

在 `PJM/backend/` 使用既有依赖、`PYTHONDONTWRITEBYTECODE=1`、禁用 pytest cache，显式将 DB 指向专用 loopback 测试 URL，执行：

```bash
python3 -m pytest -p no:cacheprovider \
  tests/runs/test_creation_replay.py tests/runs/test_task_run_service.py \
  tests/schedules/test_schedule_replay.py tests/api/test_run_api.py tests/contracts
```

结果为 **74 passed**。首次手工指定了不存在的 `test_creation_request.py`，未收集测试；按实际清单改用以上入口后通过。§55 的调度 `NameError` 不再复现。本次未修改测试，也未运行真实 DB invariant；不能把 loopback 配置或 fake repository 测试写成数据库并发验收。

在 `PJM/web/` 执行以下既有测试，结果为 **3 files / 37 tests passed**：

```bash
node_modules/.bin/vitest run \
  tests/lib/runSubmission.test.ts \
  tests/components/RunSubmissionPanel.test.tsx tests/pages/WorkspacePage.test.tsx
```

另在新的 loopback Vite `5189` 端口运行既有 [check_run_submission.py](../../PJM/web/tests/browser/check_run_submission.py)，**15 个场景通过**：丢响应、非 JSON、契约错误、502、错误 Project、显式新意图、409、422、未知结果后的 403、三语/键盘/窄屏、重复点击、timeout、账号切换、Project 切换与刷新。断网确认仍是原 body/key 和同一 mock Run；独立新意图使用新键。没有 Web storage 写入或未定义业务 API 请求。

最初使用已占用的 `5178` 端口时，旧 Vite 返回的 Modal transform 与当前文件不一致，键盘场景失败；未修改应用或放宽断言，换用本轮新起的 Vite 后完整通过。测试全面拦截 API，仅静态资源到达 Vite，不使用实际 Backend、模型、账号、业务资源或数据库。

### 56.3 文档验证与边界

文档来源、浏览版与人工阅读检查在本轮一起完成，验证结果在下表记录。业务源码、应用测试、公开 Schema/OpenAPI、DB migration、执行 Skill、应用依赖和部署配置均未在本轮修改。文档工具只增加设计索引的推荐顺序；执行资产不因 Markdown 格式而被移入文档目录。

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 39 份 Markdown、1 份 ViewSpec 示例；本地文件/章节引用、代码块与生成物一致性通过 |
| 文档工具 | 14 项 unittest 通过；build script 的 Ruff check 通过 |
| 通用契约 | 76 Schema、63 example 通过；未改变公开契约 |
| 桌面/手机阅读 | Chromium 1440px/390px 遍历全部 39 页，无整页横向溢出；长表滚动提示和键盘聚焦一致 |
| 重点章节 | 6 个章节 × 4 种宽度（320/390/768/1440px）× 普通/放大正文，共 48 个布局场景；深链接刷新后标题完整可见 |
| 阅读操作 | 新设计索引到创建页、历史 §56、正文搜索/历史过滤、前进/后退、手机键盘目录、打印、旧计划锚点和两张架构图通过 |

已查看设计导航桌面和提交状态手机截图。文档浏览没有 JavaScript 错误或 HTTP(S) 请求；依赖、备份和截图位于工作区外临时目录，不创建 venv，不清除既有应用产物。

没有执行全量 Backend/Web、真实 PostgreSQL、真实资源/模型、Docker 或服务器验收。局部绿色结果只支持上述说明，不证明全项目重构目标完成。

## 57. 公开契约交接与章节检索文档续整（2026-09-08）

### 57.1 本轮范围与设计整理

- 本轮仅继续文档与离线阅读工具，保留已有业务代码、应用测试和契约。新文档选择/公开投影已经部分进入工作副本，但不能沿用 §56 的检查数字证明当前代码完整可用。
- 新增[公开契约变更与联调](../development/contract-workflow.md)，分开持久快照、公开投影和页面状态，明确 required/enum 变化、历史数据与新旧客户端兼容、旁路消费者和分层验证。开发目录负责交付流程，领域字段仍由 design/contracts 定义，当前失败仍由计划登记。
- 资源设计以表格解释选择编码和 `FROZEN / LEGACY_UNAVAILABLE / INVALID`，明确验证清单不等于 blob 可达、完成物化或 Run 成功；旧响应缺字段不默认成空集合。补充候选刷新/失效不偷偷替换、集合未完成不提交，以及原请求确认不受后来草稿有效性影响。
- Workspace 与调度说明当前默认草稿的局限，要求即时/调度共用实际输入与显式范围；时间预览不代表资源就绪。消除 CRON 受截止限制可不足三次与必须补足三次的矛盾。
- 文档/设计索引、Backend/Web/Contracts/Scripts 和工程 README 同步阅读入口；计划 §13.10 保留现有实现与具体同步缺口，不把本轮文档请求扩大成业务代码实施。
- 浏览版搜索按真实标题锚点索引正文和代码示例，提供分类/path、最多三个命中章节与安全关键词高亮；相同章节的再次点击可重新定位。索引不复制一份独立的全文搜索正文，不扩大扫描范围，旧链接继续使用。

### 57.2 工作副本的只读诊断

文档编辑前，`build_docs.py --check` 报浏览版过期；本轮重新生成。应用诊断分别执行，并与文档工具的结果分开：

| 检查 | 观察到的状态 |
| --- | --- |
| `scripts/validate_contracts.py` | 失败：`runs/detail/v1.schema.json` 已要求 `document_snapshots`，现有 `examples/run-detail.v1.json` 尚缺此字段 |
| Web `tsc -p tsconfig.app.json --noEmit`（外置增量缓存） | 失败：DocumentSourceField 与 TaskLaunchFields 引用的 `workspace.documentSelection` 尚未进入消息类型；两处 TS2339 |
| 全部已注册 example 的只读逐项核对 | 66 份中 65 份有效、1 份无效；没有跳过错误后把总体写成通过 |
| `app.openapi()` 与保存快照比较 | 不一致：新增四个文档/资源投影模型，RunDetailResponse 与 RunHistoryItemResponse 已改变；未调用导出命令覆盖快照 |
| Backend 资源投影与 Run API 回归 | `tests/runs/test_resource_projection.py`、`tests/api/test_run_api.py` 共 34 项通过；不是全量 Backend、契约联调或真实 DB 验收 |

这些失败在本轮整理前已存在。没有回写应用 example/OpenAPI、增加业务文案、放宽 validator 或调整测试来制造通过；不把工作副本失败推断为服务器故障。后续代码责任与接续顺序见[计划 §13.10](../planning/roadmap.md#1310-公开契约交接与章节检索2026-09-08)。

诊断使用外置的既有依赖、`PYTHONDONTWRITEBYTECODE=1` 和 `pytest -p no:cacheprovider`；Web 增量缓存也写在工作区外。未启动应用或调用真实资源/模型；仅导入 app 生成内存 OpenAPI，不运行 lifespan、不写数据库。

### 57.3 文档验证与阅读体验

| 检查 | 结果与范围 |
| --- | --- |
| Markdown 清单 | 核对 51 份：40 份文档/工程入口进入浏览版，11 份执行 Skill/示例/references 保留原位且不嵌入 |
| 文档 build/check | 40 份 Markdown、1 份 ViewSpec 示例；本地链接、章节、代码块与生成物一致性通过 |
| 文档工具 | 17 项 unittest 通过；新增章节分段、深层/重复标题、空章节、标题前文字及全部搜索落点检查 |
| 工具静态检查 | build script 与文档测试按 Backend Ruff 配置执行 check/format check 通过 |
| 桌面/手机 | Chromium 1440px/390px 遍历全部 40 页，无整页横向溢出；表格滚动提示与键盘聚焦一致 |
| 重点章节 | 6 个章节 × 4 种宽度（320/390/768/1440px）× 普通/放大正文，共 48 个布局场景；刷新深链接后标题完整可见 |
| 搜索与导航 | 正文命中章节、关键词高亮、文件名优先、历史过滤、重复同一章节定位、前进/后退、手机键盘跳转/菜单、打印和旧锚点通过；两张架构图可离线打开 |
| 浏览安全 | 搜索中的 HTML 样式文字不变成元素；无 JavaScript 错误或 HTTP(S) 请求 |

初次新增的文件名测试假定只有一篇结果，但其他文档的相对链接也合法命中。保留这些引用，同时调整排序让精确文件名优先；完整浏览回归重新通过。已查看契约搜索桌面和资源选择手机截图，长表格在自身区域滚动，标题位于固定栏下方。

本轮不改变应用源码、应用测试、Schema/example/OpenAPI、DB migration、执行 Skill、依赖锁或部署配置；仅 Markdown、文档生成/检索工具及其测试有变更。备份、浏览检查、截图和验证缓存位于工作区外，不创建 venv，不删除已有应用产物。

未执行全量 Backend/Web、真实 PostgreSQL 竞争/恢复、真实资源/模型、Docker/服务器或业务页面验收。文档验证成功与 §57.2 的应用未同步同时成立，不把其中一个覆盖成另一个。

## 58. 资源公开链路与调度边界文档续整（2026-09-08）

### 58.1 整理范围与设计判断

本轮按“继续整理文档”处理，保留已有应用实现、测试和契约，不继续推进业务代码。工作副本已包含 §57 当时尚缺的显式文档选择、Schedule 实际输入、公开清单消费者、三语和契约同步；本轮核对它们，不将既有代码归为新实现。

- 资源页补充 A/B/C 文档的变化示例，区分旧 Run、原请求重发、新 Run 与下一次 occurrence；明确全集也有非空和数量约束，三个投影状态不代表可下载或执行成功。
- Workspace、Runtime、创建页、API 指南与 Backend/Web README 去除已过时的“未接入”描述。Contracts README 提供请求/响应/example/生产者/消费者/测试的可点击映射，不复制协议正文。
- 调度页纠正“更新 API 存在就等于页面可编辑”“预览已按规则时区显示”的误述。新增编辑冲突、浏览器/规则时区、无 offset 输入、DST 歧义和未知保存结果的设计要求；不借文档整理修改调度代码。
- 计划保留 R01–R13 范围与历史锚点，§13.10 改为明确的当时记录，当前核对由 §13.11 接续。历史 §57 的失败证据不倒改。
- 新增 [check_docs_browser.py](../../PJM/scripts/check_docs_browser.py)，把已有临时浏览检查收为可重复执行的正式工具。默认只读，阻断 HTTP(S)，截图仅在显式指定输出目录时保存；工程入口与文档维护指南同步。

### 58.2 工作副本的针对性核对

依赖与 TypeScript 增量缓存外置，不创建 venv。Backend 使用 `PYTHONDONTWRITEBYTECODE=1`、`pytest -p no:cacheprovider`，以下不是全量 Backend/Web 或真实 DB 验收：

| 检查 | 结果与范围 |
| --- | --- |
| Schema/example | `scripts/validate_contracts.py`：76 份 Schema、67 份已注册 example 通过 |
| Backend API/契约/资源投影/调度重放 | `tests/api/test_run_api.py`、`tests/contracts/`、`tests/runs/test_resource_projection.py`、`tests/schedules/test_schedule_replay.py`：64 项通过；含实际 API 响应的 Schema 验证和内存 OpenAPI 一致性 |
| Web 类型 | app/node 两份 tsconfig 分别以 `--noEmit`、外置 tsBuildInfoFile 检查通过 |
| Web 局部回归 | documentSelection、runSubmission、runResources、DocumentSources、RunResultPanel、TasksPage 共 6 份测试文件，88 项通过 |
| 业务表单 mock 浏览器 | `web/tests/browser/check_document_sources.py`：即时/调度 × 单份/集合/全集/可选不选 × zh/ja/en，共 24 场景通过；全部业务 API 被拦截，未连接真实 DB/blob/模型 |
| 时间输入诊断 | 当前 ScheduleDefinitionRequest 接受无 offset 的 `run_at`，得到 tzinfo=None；service 的 astimezone 可能依赖进程时区。只在内存构造 request model，未提交或保存 Schedule |
| 业务浏览器脚本静态检查 | check_document_sources.py / check_run_submission.py 按 Backend Ruff 规则仍有 19 项问题：import/行长与循环闭包捕获。浏览器场景通过不等于静态检查通过，本轮未修改这两个既有测试 |

§57 的 example 缺字段、OpenAPI 不一致和两处 TS2339 已不再复现。可信缓存回执、跨根总量、历史非终态升级、真实并发/回滚、调度持久在途与其他 R01–R13 工作仍未完成。

### 58.3 文档和阅读验证

文档生成物在本轮编辑前已过期，随来源重新生成。以下是本轮实际执行的文档验证，不引用 §57 的数字作为当前通过证据：

| 检查 | 结果与范围 |
| --- | --- |
| Markdown 清单 | 核对 51 份：40 份文档/工程入口进入浏览版，11 份执行 Skill/示例/references 留在原位、不嵌入 |
| 文档 build/check | 40 份 Markdown、1 份 ViewSpec 示例；本地链接、锚点、层级、代码块与生成物一致性通过 |
| 文档工具回归 | 17 项 unittest 通过；保留搜索落点、原始 HTML 不执行与构建可复现性检查 |
| 文档工具静态检查 | build_docs.py、check_docs_browser.py、test_build_docs.py 按 Backend Ruff 配置 check/format check 通过 |
| 正式浏览器脚本 | Chromium 遍历全部正文：1440px/390px 共 80 个页面布局；6 个重点章节 × 4 种宽度 × 普通/放大文字，48 个章节布局及 48 个刷新后布局通过 |
| 导航与安全 | 章节搜索、高亮、文件名优先、历史开关、同章再定位、前进/后退、键盘/菜单、打印、旧锚点与两张结构图通过；无 JavaScript 错误、无 HTTP(S) 请求 |

已查看资源示例桌面、清单状态手机、调度时间边界手机与契约导航桌面四张截图。表格超宽时仅自身滚动，标题完整出现在固定栏下方；正文中的字段、来源链接与历史提示可辨。

此次未运行全量 Backend/Web、真实数据库迁移/竞争/回滚、部署、业务外部写入或模型。mock 表单检查不覆盖现有调度编辑、跨时区页面、5000 件显示性能或 Worker 崩溃恢复；文档浏览检查也不替代这些业务验收。文档备份、截图与本轮验证缓存保存在工作区外，已有用户产物和执行 Skill 保留。只关闭本轮使用的自建 Vite，不清理其他已有进程。

## 59. 运维恢复与工作副本文档续整（2026-09-08）

### 59.1 整理范围与依据

按用户“继续整理文档”的要求，本轮只修改正式文档、代码目录的导航 README 与文档浏览检查脚本。不继续此前的输入回执代码草稿，不改应用、公开契约、Make/Compose、migration、执行 Skill，也不运行部署或外部写入。

- Runbook 统一中文操作正文，保留旧章节标题/锚点；新增环境文件边界、同一恢复点、迁移/回退比较、分阶段放行、恢复前停止条件和恢复后验证。领域规则改用摘要/对照和正本链接，避免在运维手册维护另一套执行设计。
- 只读核对 Makefile/Compose 与官方 Compose 规则，确认 `ENV_FILE` 只作为 CLI 插值入口、Backend env_file 仍固定 `.env`；标注混用风险与待实现的统一配置验收，不声称实际部署已复现。
- 快速启动与开发验证改为 `config --quiet`，避免正常检查输出展开的配置；明确 make deploy 会整体重启，不承担恢复期逐项放行。
- 备份示例使用本次独立目录，区分 dump 成功、archive/字节校验与完整恢复演练；恢复导入增加单事务/遇错退出，明确不回滚此前删库和已发生的外部 Effect。并列保存 DB/blob/workspace/image/config 引用及独立 KEK，旧队列/Outbox/Effect 对账作为放行条件。
- 对照迁移扩充 0018–0029 的审查入口；0027 downgrade 的审计会话删除、新格式消费者、旧非终态续行各自说明风险。表名或 migration 文件存在不作为部署和运行保证。
- 回执 DTO/model/repository/0029 已存在但未接入物化/Worker/读取；Executor 准备先于 heartbeat 的风险按代码顺序记录。两者没有在本轮修复或通过运行验收。Runbook 不再沿用“选择与清单 API/Web 完全未联调”的过期描述。
- 文档总索引、资源设计、开发交接、PJM/Backend/Scripts/Images README 与计划同步；浏览回归加入配置、恢复点、破坏操作前置和恢复验证章节，以及两张运维截图。
- 截图人工检查发现手机顶栏把“目录”和来源链接挤成多行；模板改为必要时整组换行，保持操作标签完整，并新增实际文字行数断言。没有修改业务 Web 页面。

### 59.2 验证范围

| 检查 | 本轮结果与限度 |
| --- | --- |
| 文档 build / check | 40 份 Markdown、1 个 ViewSpec 示例，本地文件/锚点与生成物检查通过 |
| 文档工具单元测试 | 17 项通过，包含旧锚点、章节搜索、HTML 不执行、可复现构建与执行 Skill 排除 |
| 文档工具 Ruff | build_docs、check_docs_browser、test_build_docs 三个文件按 Backend 配置 check / format --check 通过，使用 --no-cache |
| 可执行契约 | 76 个 Schema、67 个 example 通过；本轮没有改公开形状或重导出 OpenAPI |
| Compose 静态规则 | 8 个 service 的结构/既有 Traefik 边界通过；不证明自定义环境注入正确或真实服务可用 |
| 运维相关单元测试 | tests/ops/test_preflight.py、test_smoke_client.py 共 7 项通过；使用 fake，不连接 DB 或模型 |
| shell 示例 | Runbook 14 段、Quickstart 5 段 bash 示例经 bash -n 语法检查通过；未执行其中的操作 |
| 离线 Chromium | 40 页 × 桌面/窄屏 = 80 次页面布局；10 个关键章节 × 4 宽度 × 2 字号 = 80 次章节布局，再刷新检查 80 次；导航、搜索、历史分离、键盘、打印与旧链接通过，JS 错误和 HTTP(S) 请求均为 0 |

Ruff 初次从 PJM/ 使用默认配置时提示三份既有文档工具需重排；显式选用 backend/pyproject.toml 后 check/format 均通过，未因此重写既有工具格式。维护说明补充这一执行位置差异。

生成六张截图，人工抽看恢复点桌面、恢复前置手机和资源清单手机：窄屏表格独立滚动、顶部操作标签完整、跳转标题在固定栏下方。发现顶栏折行后修改模板，再次执行完整文档浏览回归；截图和浏览检查不作为业务 UI 验收。

历史 §1–§58 的原始 214211 字节经备份前缀比较保持不变，本轮只追加 §59。工作副本的应用代码与执行资产没有纳入文档整理的重写范围。

本轮不执行数据库迁移/替换、跨存储恢复演练、真实调度/lease 故障注入、业务 Web 全量回归、模型或外部 Provider。手册中的运维命令仅核对/语法检查，未连接任何部署环境执行。保留旧工作副本、旧测试产物和所有执行 Skill；本轮临时备份/截图位于工作区外。

## 60. 输入准备协议与开发交接文档续整（2026-09-08）

### 60.1 整理范围与设计判断

按用户“继续整理文档”处理，仅编辑设计/导航 Markdown 与文档工具。保留已存在的应用重构、测试、DB migration、公开契约、部署配置和执行 Skill；未继续 R01 业务接线。

- 资源页将 document 选择和全 Run 输入准备分成同级主题，补充按问题阅读的入口。准备单位明确为一份 Run 级回执覆盖所有根，各根仍有 manifest；避免逐根成功被理解为 Agent 已可使用部分输入。
- 区分逻辑 input 路径与受控物理世代，文件同步落盘与数据库提交 READY 也分开表达。两个短事务之间不持行锁，准备、取消、失效 lease、未知提交结果和再次使用有独立处理表。
- 明确 Tool 响应/Evidence 使用校验过的同一份字节，search 的完整性失败不伪装为 skipped 或零命中；逐文件、逐根、全输入存量与单次搜索限制分别说明，例子展示最后一根超限时不能启动 Agent。
- Runtime 修正仍把 document 显式选择整项列为待实现的旧描述；新增领取、RUNNING、输入 READY、Brief 与模型启动之间的区别。领域模型/术语、Backend 接线表、Web/开发/运维入口同步。
- 计划保留 R01–R13 范围与全部旧章节锚点，旧核对表和长摘要改为历史证据链接，不在当前状态旁维护重复的过期失败表。历史 §1–§59 的正文不倒改。
- 浏览目录按创建、资源准备、Runtime、预算排列；章节验证增加输入准备和 Backend 接线入口，截图增加准备流程桌面和中断表窄屏。不扩大 Markdown 扫描到业务 Skill、配置或任意源码。

### 60.2 工作副本的只读核对

核对代码可见：物化器已有 input_snapshots 必需依赖、begin/complete、PreparedInput、跨根累计及独立世代；ContextBuilder 已消费返回的 workspace/resources。Worker startup 仍未注入必需 store 和新总量设置；workspace Provider 未调用 input_workspace 的可信读取；Executor 的 heartbeat/取消 TaskGroup 仍晚于 ContextBuilder。

在 PJM/backend 使用既有外置依赖、PYTHONDONTWRITEBYTECODE=1 和 pytest 的 -p no:cacheprovider，执行：

```bash
python3 -m pytest -p no:cacheprovider tests/agent/test_document_materialization.py::test_materialization_uses_selected_subset_not_project_inventory -q
```

结果为 1 项失败：既有 fixture 在构造 WorkspaceMaterializer 时缺少必需 input_snapshots，抛出 TypeError，尚未进入文档范围断言。它证明当前调用方未同步，不证明隔离逻辑已被绕过，也不是部署故障证据。没有修改应用或测试、把依赖设为可选、重导出契约或复用以前的绿色数字来掩盖这项失败。

### 60.3 文档验证与阅读检查

依赖、备份和截图放在工作区外，Python 不写 bytecode，Ruff 使用 --no-cache；未创建 venv、安装业务依赖或清理已有应用产物。以下为本轮实际执行的检查，不借用 §58/§59 的应用验证结果：

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 40 份 Markdown、1 份 ViewSpec 示例，本地链接/锚点、标题层级、代码块与生成物一致性通过 |
| 文档工具单元测试 | 17 项通过，含阅读顺序、章节检索、不可执行 HTML、构建可复现与执行 Skill 排除 |
| 文档工具 Ruff | build_docs.py、check_docs_browser.py、test_build_docs.py 按 Backend 配置 check 与 format --check 通过 |
| 可执行契约 | validate_contracts.py：76 份 Schema、67 份 example 通过；未修改公开形状或导出 OpenAPI |
| 旧引用与历史保留 | 与本轮备份比较，40 篇的 867 个旧锚点全部保留；历史 §1–§59 的原始 218855 字节前缀不变 |
| 离线 Chromium | 40 页 × 桌面/窄屏 = 80 次页面布局；16 个关键章节 × 4 宽度 × 2 字号 = 128 次章节布局及 128 次刷新后布局通过 |
| 浏览操作与隔离 | 搜索/高亮、明确章节落点、同章再点击、前进/后退、旧计划锚点、键盘/菜单/打印通过；JavaScript 错误与 HTTP(S) 请求均为 0 |
| 修改范围复核 | 与备份比较，Backend/Web 应用源码与测试、migration、可执行契约、执行 Skill、AGENTS、Make/Compose 和 .env.example 未改变；Contracts README 只同步导航说明 |

初次浏览检查因“准备世代”合法命中同页两个章节而失败：旧测试把结果数固定为一。调整为验证指定章节的唯一链接、跳转标题及再次定位，不删除正常结果或削减搜索内容；完整浏览检查重新通过。工具格式化只调整了新增的 locator 表达式。

生成八张截图，人工抽看新增准备流程桌面与中断表手机。根据截图把流程改为纵向步骤，将多数为“否”的独立判断列合并到处置说明；390px 下中断表两列均可直接阅读，标题与顶栏完整可见。修改后再次执行完整浏览检查并复看这两张截图。

本轮未运行全量 Backend/Web、真实 PostgreSQL 事务/迁移/lease 故障注入、跨存储恢复、部署、业务浏览器或模型/外部 Provider。文档检查成功与 §60.2 的应用调用未同步同时成立；本轮没有修复输入准备执行链，也没有把 R01–R13 判定为完成。

## 61. 阅读导航、流程语义与现状文档续整（2026-09-08）

### 61.1 范围与判断

按最新“继续整理文档”处理，保留此前应用改动。本轮只修改文档、离线浏览模板/验证工具和生成物，不改 Backend/Web 业务源码、应用测试、迁移、公开契约、部署配置或执行 Skill。

- 总索引从专项细节汇总收敛为八类阅读目的；设计、变更、运维各自承接详细入口。首次打开、品牌和未知文档返回使用同一文档导航，原有深链接不改向。
- Task Flow 不再把视图层次画成现行所有权树。区分 required/recommended 与动态活动、语义 checksum 与布局、frozen plan 与实际状态；补充发布到历史重放、旧版缺失/新版损坏的处理和具体例子。这是后续设计，不新增 Schema 或第二个执行器。
- Worker/Tool 的既有接线已超过 §60 的核对时点，相关资源/Runtime/运维/代码 README 与计划同步。局部回归不替代准备期监督、真实 DB 与整链验收，R01–R13 的范围不缩减。
- 浏览器回归扩展到目的导航、Flow 身份/布局、发布关系和事实例子，并从导航真实点击到例子。执行 Skill 和历史正文不因属于 Markdown 被搬动或改写。

### 61.2 当前代码的只读验证

在 PJM/backend 复用外置依赖，使用 PYTHONDONTWRITEBYTECODE=1、pytest -p no:cacheprovider：

```bash
python3 -m pytest -p no:cacheprovider tests/agent/test_workspace_provider.py tests/agent/test_document_materialization.py::test_materialization_uses_selected_subset_not_project_inventory -q
```

结果：18 项 Provider 测试通过，1 项文档物化测试失败。通过项覆盖缺回执、输入改写、额外文件、同字节响应/Evidence、硬链接写入保护与搜索计量等局部路径。失败仍为旧 fixture 构造 WorkspaceMaterializer 时缺必需 input_snapshots，未进入范围断言；没有修改应用或 fixture 来制造绿色结果。

静态核对可见 Worker 已注入 PostgresInputSnapshotStore 与 total limits，.env.example 有对应设置，Provider 已使用可信读取 helper。Executor 仍先完成 ContextBuilder/Brief 再启动 heartbeat/取消 TaskGroup；这是顺序核对，不是故障注入证明。局部测试不证明真实 store transaction、物化世代恢复或配置注入的所有变体正确。

### 61.3 文档与阅读验证

复用已有外置依赖，不创建 venv，不写仓库内 Python/Ruff 缓存。以下为本轮实际结果，不借用 §60 的成功数字：

| 检查 | 结果与限度 |
| --- | --- |
| 文档 build/check | 40 份 Markdown、1 份 ViewSpec 示例通过；标题、代码块、本地文件/锚点与生成物一致 |
| 文档工具回归与风格 | 17 项单元测试通过；三份 Python 文档工具按 Backend 配置 Ruff check / format --check 通过 |
| 可执行契约 | 76 份 Schema、67 份 example 通过；未修改公开形状或导出 OpenAPI |
| 离线 Chromium | 80 次页面布局；20 个关键章节 × 4 宽度 × 2 字号，共 160 次章节布局及 160 次刷新后布局通过 |
| 导航与隔离 | 首次入口、品牌/未知文档返回、具体阅读路径、搜索/高亮、历史分离、前进/后退、键盘、打印与旧章节通过；JS 错误和 HTTP(S) 请求均为 0 |
| 历史与编辑范围 | 对照备份的 608 份文件，40 篇的 878 个旧锚点保留；历史 §1–§60 的原始 224247 字节前缀不变；应用/契约/执行 Skill 文件未改变 |

共更新 18 份 Markdown、三份 Python 文档工具、浏览模板与生成的 index.html。生成十二张截图，人工抽看导航桌面/手机与 Flow 身份桌面/事实例子手机；两列内容在 390px 可直接阅读，标题未被固定栏遮挡。原有章节链接保留，没有移动或删除正式文档与执行资产。

本轮未运行全量 Backend/Web、真实 PostgreSQL/迁移/准备中断、部署/恢复、业务浏览器或模型/外部 Provider。§61.2 的物化测试仍失败，准备期监督仍有缺口；文档阅读检查通过不改变这些应用事实，也不构成 R01、R03 或全项目目标完成的证据。

## 62. 执行边界、计时器与交接文档续整（2026-09-08）

### 62.1 整理范围与设计判断

按“继续整理文档”核对当前工作副本，保留此前应用代码与测试。本轮修改设计/工程入口、计划和文档浏览回归，不实施业务重构，不修改公开契约、migration、部署配置或执行 Skill。

- Runtime 用调用顺序和事实表说明 claim、RUNNING、输入 READY、Brief、启动 gate 与 Session 的区别；把准备监督更新为已有代码，不沿用 §61 核对时的旧顺序。
- 计时器集中在预算设计，明确 lease、ContextBuilder、仓库命令、模型、子分析和 ARQ job 的不同范围。准备 timeout 不包住全部 DB 操作，取消/关停也不等于终态事务永不可中断。
- 补充失效 Worker、用户取消、Provider timeout 与准备 deadline 的不同收尾，明确首事件前取消、真实进程停止及提交结果未知仍需专项验证。输入 READY 不保证后续 Brief/模型成功，也不通过删除现场解决两者不一致。
- Backend README 按监督、SQL gate/配置、实际字节区分测试责任，说明 claim 与 PreparedInput 的消费者要求；Web/Contracts 不把内部回执或 timeout 变成未定义公开进度。运维增加只读分诊，0029 回退说明按源码记录有回执即拒绝的门禁。
- 保留原目录分工和旧章节引用，不为一次接续增加平行设计正本。当前状态只在计划维护，历史 §1–§61 不倒改。

### 62.2 现有代码的针对性回归

在 PJM/backend 复用工作区外依赖，使用 PYTHONDONTWRITEBYTECODE=1 与 pytest -p no:cacheprovider；下列命令没有连接真实 DB 或模型，也未修改被测代码：

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q \
  tests/worker/test_preparation_supervision.py \
  tests/worker/test_agent_run_executor.py \
  tests/worker/test_worker_startup.py \
  tests/runs/test_execution_gates.py \
  tests/core/test_settings.py \
  tests/agent/test_materialization_storage.py \
  tests/agent/test_workspace_provider.py
```

结果：78 项通过。覆盖准备前/准备中的 heartbeat、持久取消、独立准备期限、Brief/启动门禁、失效 lease 与 stream 收束、配置范围/startup 注入，以及局部文件读取/搜索/安全写入。SQL gate 使用 mock 验证语句与锁顺序，不能当作真实 PostgreSQL 锁竞争/事务故障证明；thread 与 stream fake 也不证明真实模型进程停止。

另单独执行：

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q \
  tests/agent/test_document_materialization.py::test_materialization_uses_selected_subset_not_project_inventory
```

结果：1 项失败，既有 fixture 在构造 WorkspaceMaterializer 时缺少必需 input_snapshots，尚未进入范围断言。失败属于消费者未同步的证据，不是越权成功的证明；本轮没有修改 fixture、放宽接口或跳过失败。

本轮未执行全量 Backend/Web、真实 PostgreSQL/迁移/恢复、业务浏览器、部署或模型/外部 Provider；这些范围不能从上述局部通过推导。R01 与 R01–R13 整体范围均未判定完成。

### 62.3 文档与人工阅读检查

文档验证单独记录，不复用 §61 的件数作为本轮结果。依赖、备份与截图使用工作区外目录，不创建 venv、不产生仓库内 Python/Ruff 缓存，也不清理原有应用产物。

| 检查 | 本轮结果与限度 |
| --- | --- |
| 文档 build/check | 40 份 Markdown、1 份 ViewSpec 示例，标题层级、代码块、本地链接/锚点与生成物一致性通过 |
| 文档工具单元测试与风格 | 17 项通过；三份 Python 文档工具按 Backend 配置 Ruff check / format --check 通过 |
| 可执行契约 | 76 份 Schema、67 份 example 通过；未改公开形状或重导出 OpenAPI |
| 离线 Chromium 布局 | 40 页 × 桌面/窄屏 = 80 次页面布局；23 个关键章节 × 4 宽度 × 2 字号 = 184 次章节布局 |
| 刷新与操作 | 另检查 184 次原生 reload 与 184 次恢复字号后的布局；导航、搜索/高亮、旧章节、前进/后退、键盘和打印通过 |
| 浏览隔离 | JavaScript 错误、HTTP(S) 请求均为 0；没有启动业务 Web、API、DB 或模型 |
| 编辑边界 | 对照 679 份备份文件，保留 886 个旧章节锚点；历史 §1–§61 的原始 228435 字节前缀不变；应用/契约/执行 Skill 与部署配置未改变 |

本轮更新 18 份 Markdown、文档浏览模板、浏览回归 script 和生成的 index.html。保留原目录结构和正本分工，未移动或删除文档、执行资产或历史记录。

新增窄屏回归最初对同一 hash 执行 goto，未触发重新定位；改为真实 reload 后仍发现标题落在屏幕外。浏览器自动恢复的旧滚动坐标会覆盖自定义章节定位，因此依据 [HTML History 规范](https://html.spec.whatwg.org/multipage/nav-history-apis.html#dom-history-scroll-restoration)在首个 script 使用 manual 恢复，并由现有文档/章节路由定位。独立导航复测及完整浏览回归通过；测试增加“重新调用 showPage 之前先检查原生刷新”的断言，不用测试补位掩盖问题。

生成十六张截图，人工抽看 Runtime 桌面/手机、timeout 桌面与运维分诊手机。执行流程压成短行的纵向步骤，手机分诊以中文场景为首列，长错误码放在可换行的说明列，减少横向滚动。标题、固定工具栏与导航仍可见；浏览通过不改变 §62.2 的物化失败或未验收范围。

## 63. Skill 生命周期与开发文档续整（2026-09-08）

### 63.1 整理范围与设计判断

按“继续整理文档”处理，仅更新领域规范、代码目录 README、当前计划和文档浏览回归。保留此前应用源码、测试、契约、migration、执行 Skill 与配置；不发布版本、不启停项目、不运行部署或业务数据操作。

- 用阶段判断表区分导入、Preview、gate、PUBLISHED、Project 启用和 task readiness，补充同一版本在不同项目的例子；不把多个对象状态合成“可用”。
- 修正 native 可绕过 Interpreter 的旧描述，以及交互可以重新选择 Run 资源的歧义；当前 parser 只产生确定性输入预览，Run 冻结边界仍独立生效。
- 按 Schema 将 Manifest 版本标识纠正为 projectmind/v1alpha1，区分冻结内容与版本生命周期/门禁元数据。已接受 warning 由版本记录承载，不写进 Manifest 本文。
- 核对现有 SkillRepository：已停用的同版关系不能重新启用，废弃版不能重新发布。原先笼统的回滚说明拆成现有限制与追加式审计恢复目标；不建议删记录或清空 disabled_at 绕过。
- 组合当前只把精确版本归组，跨 Skill 规则求解与扩展配置不再写成现成服务。设计、实现接线、契约和 Backend/Web/Skill README 相互链接，脚本 README 的重复验证过程收回文档维护指南。

### 63.2 代码与契约的核对范围

接续的 Backend 全量基线在未修改应用的工作副本执行，使用工作区外依赖、PYTHONDONTWRITEBYTECODE=1、pytest -p no:cacheprovider。等价命令在 PJM/backend 为：

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q --tb=short
```

结果：1018 passed、46 failed、20 skipped，退出非零。45 项在 WorkspaceMaterializer 的旧 fixture 构造时缺少必需 input_snapshots，尚未进入目标范围断言；1 项是 test_alembic 仍预期 0028_skill_source_file_index，而工作副本 head 为 0029_run_input_snapshots。没有修改测试、降低生产参数要求或把 skip 当作通过；这里不证明越权成功或真实数据库迁移失败。

另对本次文档引用的边界执行：

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q --tb=short \
  tests/skills/test_skill_importer.py \
  tests/skills/test_manifest_gate.py \
  tests/skills/test_skill_repository.py \
  tests/skills/test_resource_binding.py
```

结果：90 项通过。分别覆盖来源解析、蓝图/门禁、部分版本与项目作用域操作、候选/readiness。repository 使用 mock；重新启用拒绝还通过源码核对，不能将这些用例说成已覆盖多次启停历史、真实事务或回滚目标实现。静态核对还发现 resource_binding.py 的旧注释仍提到运行中 CHOICE 选资源，本轮仅修正文档边界，后续代码需对齐。

本轮没有执行真实 PostgreSQL、全量 Web/业务浏览器、部署恢复、实际 Interpreter 模型或外部 Provider 验收。R01 的消费者/迁移断言失败与 R06 的重新启用缺口都保留，全项目目标未判定完成。

### 63.3 文档与阅读验证

文档 build/check、工具回归、契约与离线浏览分别记录，不把文档成功替换成应用验收。依赖、备份、JUnit 与截图保存在工作区外；不创建 venv，不清理用户原有应用产物。

| 检查 | 结果与限度 |
| --- | --- |
| 文档构建与一致性 | 40 份 Markdown、1 份 ViewSpec 示例；标题、代码块、本地链接/锚点与生成物一致 |
| 文档工具 | 17 项单元测试通过；三份 Python 文档工具 Ruff check / format --check 通过 |
| 契约与文档引用 | 76 份 Schema、67 份 example 通过；另按现有 Schema 校验 Skill 文档的 ResourceRequirement 片段、Manifest 版本值与兼容 enum |
| 离线 Chromium | 80 次页面布局；27 个关键章节 × 4 宽度 × 2 字号，共 216 次章节布局、216 次原生刷新、216 次恢复字号布局通过 |
| 导航与网络隔离 | 代码 Skill README 到阶段判断/回滚的真实点击，以及既有搜索、键盘、历史分离、前进/后退、打印等通过；JavaScript 错误和 HTTP(S) 请求均为 0 |
| 编辑范围审计 | 对照 679 份备份文件，保留 894 个旧锚点；历史 §1–§62 的 234033 字节原前缀不变；业务源码/测试、契约、migration、执行 Skill、部署配置未改变 |

本轮更新 15 份 Markdown、文档浏览回归脚本与派生 index.html，未移动或删除文件。生成二十张截图，人工抽看阶段表桌面/手机、回滚限制手机和生命周期接线桌面；窄屏两列表格可直接阅读，章节标题、工具栏与导航完整。浏览验证仅覆盖文档，不证明同版恢复、组合规则求解或业务 UI 已实现。

## 64. 外部效果与恢复文档续整（2026-09-08）

### 64.1 整理范围与设计判断

按“继续整理文档”更新受控写入、相邻设计、Backend/Web/Contracts README、状态与离线阅读检查；没有修改业务源码/测试、Schema、migration、执行 Skill 或部署配置。文档开始前已有输入回执和消费者改动，保留并只做相关验证。

- 把批准、外部写入、回读、PR 与平台保存分开，以 commit 后 PR 超时的例子解释未知和部分成功；补齐三个 DB 阶段与事务外 I/O 的短流程。
- 核对重试实际重进整个 Provider，不再写成现成的“仅恢复 PR”。文件/字段相同只证明期望状态相同，不能证明原 Proposal/Effect 已执行；严格前置条件、原身份回执和历史未知处理写为后续要求。
- SVN checkout 与 read-back 未固定批准/提交 revision、copy 与 commit 分离；Git 预读与普通 push、forge 查询与创建也存在独立竞争边界。以上来自源码推导，未冒充生产故障注入结果。
- Effect Executor 没有贯穿 Provider 的 heartbeat/取消监督，不能从 Run Executor 的准备监督推导它已有相同保证。取消或 lease 过期也不表示远端操作未发生。
- 批准卡片每次点击换键与后端原决策幂等不一致，Workspace 单列原请求确认要求。修正旧文档把 PendingActionsPanel 当作审批提交入口、把 ProjectMember 与 system ADMIN 混写的问题。
- 现有 repository.write request Schema 仍只表达 branch，当前调用却经 change.propose 和内部 ClaimedEffectExecution；标出消费者和版本同步要求，没有扩大 Agent write 权限或悄悄改动契约。

### 64.2 应用核对的范围

使用工作区外已有依赖、PYTHONDONTWRITEBYTECODE=1 和无 pytest cache 设置，在 PJM/backend 执行：

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q --tb=short \
  tests/effects tests/worker/test_effect_executor.py \
  tests/worker/test_effect_recovery_job.py tests/runs/test_effect_decision_service.py \
  tests/api/test_effect_api.py tests/agent/test_workspace_materializer.py \
  tests/agent/test_document_materialization.py tests/agent/test_repository_client.py \
  tests/agent/test_runtime_context.py tests/db/test_alembic.py \
  tests/runs/test_repository_inputs.py
```

174 项通过，无失败/skip。此前物化消费者和 migration head 断言已在这组回归中通过；未重跑全量 Backend，不改写 §63 的旧基线。输入完成确认新分支使用 mock DB/transaction，物化使用独立 test store；不等于真实 commit 响应丢失、数据库锁竞争或完整历史恢复已验收。

在 PJM/web 执行 `node_modules/.bin/vitest run tests/api/controlledEffects.test.ts tests/components/RunResultPanel.test.tsx --no-file-parallelism`，2 个文件、25 项通过。组件/client 的现有测试不覆盖批准响应丢失或真实页面操作；没有据此声称新的恢复设计已完成。

Provider 用例包含临时本地 Git/SVN 与 fake forge/Redmine，Worker/审批仓储包含 mock。未访问真实外部仓库或业务数据，不创建远端 PR、不运行模型、部署或数据库恢复。外部执行的差距仍保留在 R08，其余全项目范围没有缩减。

### 64.3 文档与阅读验证

文档与浏览验证单独记录，不能替代上节未覆盖的运行环境。备份、JUnit 与截图使用新的工作区外目录，不创建 venv、不清理用户原有缓存或构建产物。

| 检查 | 结果与限度 |
| --- | --- |
| build/check | 40 份 Markdown、1 份 ViewSpec 示例，标题、代码块、本地链接/锚点与生成物一致 |
| 文档工具 | 17 项单元测试通过；三份 Python 文档工具 Ruff check / format --check 通过 |
| 现有契约 | 76 份 Schema、67 份 example 通过；旧 branch-only request 的语义差距仍保留，不以形状校验通过代替消费者一致 |
| 离线 Chromium | 80 次页面布局；34 个关键章节 × 4 宽度 × 2 字号，共 272 次章节布局、272 次原生刷新、272 次恢复字号布局通过 |
| 导航与网络隔离 | Backend 入口到事实区分、阶段恢复和审批界面的真实点击，以及搜索/历史分离/键盘/前进后退通过；JavaScript 错误与 HTTP(S) 请求为 0 |
| 范围与历史审计 | 对照 681 份备份文件，保留 908 个旧锚点；历史 §1–§63 的 238872 字节前缀不变，业务代码/测试、契约、执行 Skill 与配置未改 |

共更新 16 份 Markdown、文档浏览回归脚本和派生 index.html，未移动或删除文件。人工抽看事实表、阶段恢复、审批界面的桌面/手机截图；将首屏三列状态表合并为两列，并把恢复长段落拆成按 Git/SVN/forge 区分的短项。浏览检查增加手机上首表无需横向拖动的约束与短事务流程截图；不能只让“整页不溢出”代替关键信息易读。

本轮按照项目技能约定维持 README 日文、设计正文中文、领域设计单一正本与验证范围分开。既有实现缺口写为接续要求，没有弱化批准/scope 或把文档成功标为 R08/全项目完成。

## 65. 调度事实、并发与管理文档续整（2026-09-08）

本轮按“继续整理文档”执行，只修改设计/导航、代码 README、文档浏览检查及派生页面。应用源码/测试、公开 Schema、migration、配置和执行 Skill 保持原样；工作前以工作区外 snapshot 保留现场，不初始化或修改 Git，不创建 venv。

### 65.1 事实与设计修正

- Schedule 先用一条按小时触发的例子区分规则状态、occurrence、关联 Run 与回写摘要。last_run_at 是计划时刻，跳过/失败时 last_run_id 保留旧值，两者不一定属于同次触发；run_count 也不是成功次数。
- 纠正此前当前设计/术语中的“查询同 Task 非终态 Run”：实际 _overlapping_run 仅按本 Schedule 的 last_run_id 查 Run。手动/其他 Schedule 不在检查范围，创建后漏回写时本 Schedule 也可能漏判；修正保留同 Schedule 作用域，不擅自加入全局串行或排队。
- update_definition 先读 row_version 再在 Python 比较，没有条件 UPDATE、行锁或 ORM version mapper。状态更新也先读后写。API fake 的 409 测试不证明原子并发保护；文档单列配置版本、锁内判断与晚到回写要求。
- Web 只加载 Project 前 100 条 Schedule，再按当前 TaskCatalog 挂卡片；条数超出或精确任务失效时可能漏显。设计补充分页管理、失效/归档可见性与原配置编辑要求，不能用重建或 latest 掩盖未知保存结果。
- 持久在途设计拆成认领记录/执行权、配置/暂停、结算/名额、历史/上线。未知 Run 提交先保留名额并查询原键；不虚构旧账本，不让旧 tick 绕过新协议。这些是修正目标，不是已存在的公开字段或运维恢复命令。

以上并发、漏显和中断风险来自当轮源码/契约的只读核对，不冒充真实数据库或业务浏览器故障复现。Backend/Web/契约 README 提供职责与测试入口；Workspace、术语、变更指南和 Runbook 只引用正本，不另建一套恢复规则。

### 65.2 既有应用回归的范围

复用工作区外已有依赖，设置 PYTHONDONTWRITEBYTECODE=1，在 PJM/backend 执行：

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q \
  tests/schedules tests/api/test_schedule_api.py \
  tests/worker/test_schedule_tick_job.py
```

65 项通过，无失败/skip。包括纯时间/DST/状态逻辑、原键重放、API fake 与 tick fake，不包括真实 PostgreSQL 的并发编辑、认领 crash、执行权接管或历史迁移。

在 PJM/web 执行 `node_modules/.bin/vitest run tests/api/schedules.test.ts tests/pages/TasksPage.test.tsx --no-file-parallelism`：2 个文件、10 项通过。覆盖 client、任务卡结合与静态渲染，不是实际调度编辑、分页或跨时区浏览器受入。没有为得到绿色结果修改这些测试或业务实现。

没有重跑全量 Backend/Web、真实数据库、业务浏览器、模型、远端仓库或部署；不沿用之前的局部数字宣称当前全项目通过。R09 及其余 R01–R13 的范围保持在计划中。

### 65.3 文档与阅读验证

文档 build/check、离线浏览与范围审计在本节单独记录，不替代上述未覆盖的应用行为。

| 检查 | 结果与范围 |
| --- | --- |
| build/check | 40 份 Markdown、1 份 ViewSpec 示例；本地链接、旧锚点、标题层级与派生页面一致 |
| 文档工具 | 17 项单元测试通过；build/browser/unit test 三份 Python 文件的 Ruff check 与 format --check 通过 |
| 既有契约 | 76 份 Schema、67 份 example 通过；不据此声称原子 CAS、Schedule 持久记录或分页消费者已实现 |
| 离线 Chromium | 80 次页面布局；45 个关键章节 × 4 宽度 × 2 字号，共 360 次章节布局、360 次原生刷新、360 次恢复字号布局通过 |
| 阅读与网络隔离 | Backend → 示例 → 重叠 → 认领恢复，以及 Web → 管理 → 契约的真实文档点击通过；JavaScript 错误和 HTTP(S) 请求为 0 |
| 范围与历史 | 对照 681 份原文件，保留 922 个旧锚点；历史 §1–§64 的 244055 字节前缀不变；没有新增、移动或删除项目文件 |

共更新 13 份 Markdown、文档浏览回归脚本和派生 index.html。32 份截图存于工作区外本轮验证目录；人工抽看调度例子、摘要表、重叠范围、恢复步骤、管理和契约入口的桌面/手机视图。把管理长段落拆成“当前边界/后续交付”，恢复改成有序步骤；摘要两列表在 390px 下无需横向滚动，并另取全表截图，避免只检查首屏流程而漏掉字段含义。

遵循项目技能约定保留 README 日文、设计正文中文和执行 Skill 的版本/hash 边界；只追加历史记录，不覆盖旧回归结论。所有应用缺口仍是后续实现任务，文档通过不是 R09 或全项目交付完成。

## 66. R01 输入世代隔离与多根不变量收口（2026-09-08）

本轮继续全项目代码目标，不是仅整理文档。开始时保留工作区外 snapshot 并复核源码；原 Backend 全量基线为 1086 项通过、20 项 PostgreSQL skip。前轮留下的 Mypy handle 已确认终态成功，没有因等待超时重启任务。

### 66.1 修正与新增覆盖

- 新回归先复现：空 `.projectmind-inputs` 与另一个 UUID 的孤立目录会被接受，symlink 越界泄漏为未归一的 ValueError；4 个场景中 3 失败、1 通过。不是依靠修改断言假定旧行为已经安全。
- `materialization_storage` 改为从受控 UUID 导出私有路径，并对命名空间执行独占 mkdir，再创建回执指定世代。不同 UUID 的线程竞争只有一个赢家；命名空间创建后 I/O 失败保留空现场，不能用另一 UUID 重新开始。
- `WorkspaceMaterializer` 在首次生成时调用此边界，READY 继续使用既有完成记录；路径越界/loop 等异常统一为 MaterializationError，不放宽 RunWorkspace 或 no-follow 检查。
- 新 `test_input_preparation_protocol.py` 用真实临时文件、受控 inventory/repository 与独立 memory store，覆盖多文档槽位并集、两份 repository 副本、manifest/index/history 计量、逐根/全局的精确文件与字节边界、最后一根超限无 READY、复用时降低限额、共同篡改、伪造 claim/参数、取消/lease 失效、接管及新 Segment 复用。
- 原 unsafe requirement key 测试改为 claim/blueprint/binding 一致后实际命中路径守卫，不再提前因“未声明 requirement”通过；输入回执测试的 unused import 与格式问题一并收口。
- 新 migration 测试比对 0029 与 model 的 PostgreSQL 类型、Run 唯一约束、RESTRICT、状态/完成条件，并检查 retention guard 先于 drop。此项只验证 DDL 和调用顺序，没有运行真实降级。

没有新增公开字段、迁移版本或安全例外；必需 input store、冻结来源、逐根/全局限额、Run → Segment → Attempt 锁顺序和禁止内置 Tool 的边界不变。输入命名空间残留必须保留，不添加删除后自动重新物化的恢复入口。

### 66.2 修改后的验证

复用外置依赖，保持 PYTHONDONTWRITEBYTECODE=1，不创建 venv。Backend 的 pytest 使用 `-p no:cacheprovider -o addopts=`，Mypy cache 和 JUnit 均在本轮工作区外目录；Web bundle 也输出到新的外置目录，没有清空用户原有缓存/产物。

| 检查 | 实际结果 |
| --- | --- |
| Backend Ruff | `ruff check --no-cache .` 通过；7 份修改/新增 Python 文件的 format --check 通过 |
| Backend Mypy | `mypy src` 通过，164 个源码文件 |
| Backend 全量 pytest | 1121 项通过、20 项跳过；本轮新增/扩展 35 个用例，原基线已通过的测试保持通过 |
| Web typecheck | `tsc -b --pretty false` 通过 |
| Web 全量 Vitest | `vitest run --no-file-parallelism`：38 个文件、335 项通过 |
| Web production build | 通过，输出目录外置；保留 532.27 kB 主 bundle 的大于 500 kB 提示，不通过提高阈值掩盖它 |
| 契约 / Compose 静态检查 | 76 份 Schema、67 份 example、8 个 Compose service 与既有 Traefik 边界通过 |
| SDK 离线 probe | SDK 0.2.110 / CLI 2.1.191 的 options、interrupt、MCP、SessionStore 接口检查通过；没有调用模型 |

新增输入协议测试中 DB 是独立 memory authority，migration 测试为 SQLAlchemy DDL/mocked operation；它们不能替代 PostgreSQL 锁竞争或提交响应丢失。完整 pytest 的 20 项 skip 来自 PostgreSQL 不可达，环境也没有 docker/postgres/initdb/pg_ctl，未执行 Compose config/smoke、真实迁移/恢复、业务浏览器、HTTPS 资源或真实模型。没有连接未知业务数据库、修改远端仓库或创建 PR。

### 66.3 接续范围

资源设计、Backend 接线和 Runbook 同步命名空间的保留/拒绝行为，多根本地证据与真实恢复分开记录；旧章节和历史结论保留。文档生成和链接检查不替代代码测试。

R01 仍需真实事务、混合版本与历史恢复和完整执行链；R02 共享预算、R07 首事件前取消、R08 阶段恢复/执行权、R09 持久调度及 R03–R13 其余任务继续保留。Backend/Web 测试全绿不代表所有文档定义的功能已实现，本轮不关闭全项目目标。

## 67. 预算计量、子分析与开发文档续整（2026-09-08）

本轮按“继续整理文档”处理，只改设计、阅读入口和文档浏览回归，不实施共享预算或修复业务代码。开始时把 README/docs/PJM 保存到工作区外 snapshot，保留此前输入世代隔离实现及测试；不把 §66 的应用全量数字改写为本轮验证。

### 67.1 设计与接线核对

预算页补充当前用量链路：mapper 的 USAGE_UPDATED 有多种来源，Result/Session 保存最近报告，子收集器不归集用量，子 Session 只有 branch_key/空 cost。20 turns 的账户例子将已确认、仍占用和可再分配分开；目标协议明确累计/增量去重、预留与启动意图提交、原键确认、主子共同占用、晚到用量和执行权分离、旧 Worker 与回退门禁。账户与公开投影仍待实现。

子分析页更正了两个过度保证：以事件返回的失败不等于抛出异常；Provider 省略 ID 不代表结果能通过 Gateway。目标设计要求成功终端验证，并把结论、Session 审计和预算结算分开，需后续同步实际 Schema/消费者，不能直接放宽验证。Backend/contracts/Web README 分别连接执行、契约和展示入口，术语、设计索引与变更指南只保留各自职责，不复制另一套进度表。

### 67.2 既有回归与问题诊断

在 PJM/backend 使用外置依赖、PYTHONDONTWRITEBYTECODE=1 和 `pytest -q -p no:cacheprovider -o addopts=` 执行：

```text
tests/agent/test_subagent_capabilities.py
tests/agent/test_subagent_provider.py
tests/agent/test_claude_engine.py
tests/agent/test_tool_gateway.py
tests/agent/test_runtime_context.py
tests/contracts/test_contracts.py
```

74 项通过，无失败/skip。Web 的 `vitest run tests/components/RunResultPanel.test.tsx --no-file-parallelism` 为 1 文件、22 项通过；它检验固定 Evidence 的投影，不验证真实模型的终端判断或共享余额。

工作区外另用 5 个只读诊断用例复现当前缺口，全部得到预期的现状结果；这些不是修复回归，也没有加入正式应用测试：

| 输入与观察边界 | 实际观察 |
| --- | --- |
| 连续两次两支 dispatch，父上限 20 turns / USD 0.4 | 每次每支都分配 10 turns，每个子 context 都继承 USD 0.4；不说明实际花费了这些数值 |
| recorder 失败，通过真实 registry/Gateway | 省略 ID 后返回 unavailable，memory audit 没有成功 response/Evidence |
| 分别输入 ENGINE_FAILED、SESSION_INTERRUPTED、空 stream | 3 个场景均被 Provider 标为 COMPLETED，经 Gateway 保存成功 Evidence，recorder 也收到 COMPLETED |

诊断使用 fake engine/recorder 与 memory audit，registry、Schema、Provider 和 Gateway 为当前实现；未调用模型、真实 DB、网络或外部进程。正式测试绿色与这些接线缺口可以同时存在。后续代码变更应把诊断转换为期望正确行为的回归，覆盖 actual mapper 与完整消费者。

### 67.3 文档与阅读验证

浏览检查新增预算数值例、用量来源、子结果/契约的真实点击和窄屏回归；不执行文档里的收费、恢复或配置操作。

| 检查 | 实际结果与范围 |
| --- | --- |
| 文档 build/check | 40 份 Markdown、1 份 ViewSpec 示例；链接、标题/旧锚点及生成物一致 |
| 文档工具 | 17 项单元测试通过；build/browser/unit test 三份 Python 文件的 Ruff check 与 format --check 通过 |
| 契约 | 76 份 Schema、67 份 example 通过；没有修改公开 Schema，不据此推导异常响应已与 Provider 一致 |
| 离线 Chromium | 80 次页面布局；56 个关键章节 × 4 宽度 × 2 字号，共 448 次章节布局、448 次原生刷新、448 次恢复字号布局通过 |
| 阅读与隔离 | 全部既有导线及 Backend → 预算例子 → 晚到结算、Web → 用量来源 → 子结果 → 契约的真实点击通过；JavaScript 错误和 HTTP(S) 请求为 0 |
| 范围审计 | 对照 683 份原文件，保留 942 个旧锚点；历史 §1–§66 的 253408 字节前缀不变，没有新增、移动或删除项目文件 |

首轮浏览回归在预算数值表的窄屏可读性断言处失败：数字列沿用了正文列的最小宽度。随后采用 Markdown 右对齐数字列，并使浏览模板按数字内容收缩宽度；没有缩小全页文字、隐藏列或放宽断言。子结果与用量来源改成两列说明，避免把影响藏在第三列。定点复查通过后重跑全书，得到上表的通过结果。

最终全书回归生成 39 张截图，保存在工作区外本轮目录；人工抽看桌面/手机的预算表、子结果表、用量来源、晚到结算及契约入口。补录验证结果后重新生成浏览版并检查来源一致性。

本轮更新 12 份 Markdown、浏览回归脚本、浏览模板和派生 index.html。按项目技能约定保留 README 日文、设计正文中文和 Skill 执行资产的 version/hash 边界；现有应用源码、正式测试、Schema、配置及历史记录均保留。未重跑应用全量、业务浏览器、真实 DB/模型或部署；这些范围不借用旧数字记为本轮通过。

## 68. 子分析交接与验证状态文档续整（2026-09-08）

本轮继续整理文档。开始时对 README/docs/PJM 保存工作区外快照，保留既有子终端、共享结果校验、stream 清理、Worker 接线、正式测试及 budget Schema 说明的改动。以下“已有代码”不是本轮新实现；未修复业务代码或通过改测试断言制造绿色结果。

### 68.1 设计与阅读入口

子分析页用一次配置/日志评审区分 Tool success、分支 outcome、Session 生命周期和主 Run Result，删除对旧收集器的现状描述，保留 §67 的原始诊断事实。v1 必需 Session ID 的拒绝边界与目标审计降级分开；子 Session 和 Tool/Evidence 两次提交的失败、未知结果与重放也分别说明，不承诺一笔跨 SDK/数据库的大事务。

核对 _derive_child_context、Claude output_format、ResultValidator.validate_context 与 Session recorder 后，补充子指令/输出设计：只替换 prompt/tools 并未重新生成子 Brief；父结果 Schema、Brief/checksum 和 options checksum 的继承不能证明局部指令与产出一致。这是代码关系所推导的风险，不是模型故障注入结果。目标采用共享安全校验、分别定义任务产出，要求实际指令依据、输出契约、SDK/平台验证、审计与汇总同步，不改写父快照或先扩大子权限。

Backend README 连接新收集器、validator、清理与 Session/Gateway 边界，contracts README 说明现行形状与未来语义版本，Web README 说明状态投影。Runtime、预算、设计责任索引与变更指南只保留职责摘要和链接；计划更新当前失败及接续顺序，不复制整份历史测试表。

### 68.2 既有应用回归

在 PJM/backend 使用外置依赖、PYTHONDONTWRITEBYTECODE=1 和 `pytest -q -p no:cacheprovider -o addopts=`，执行以下现有测试，不改源码或 fixture：

```text
tests/agent/test_subagent_lifecycle.py
tests/agent/test_subagent_provider.py
tests/agent/test_claude_engine.py
tests/agent/test_result_validation.py
tests/worker/test_agent_run_executor.py
tests/worker/test_preparation_supervision.py
tests/worker/test_worker_startup.py
```

结果为 **79 项通过、3 项失败，无 skip**。失败都在 test_agent_run_executor.py：

| 用例 | 本轮观察 |
| --- | --- |
| test_validated_result_reaches_success_terminal | 预期 SUCCEEDED，实际 FAILED；测试只为旧 validate 设置 AsyncMock |
| test_invalid_result_is_finalized_as_failed | 预期校验失败事件，实际没有该事件；未到达预设的旧 validator 异常路径 |
| test_review_suspends_session_a_and_forks_session_b_to_result | Review 等待及 fork 接线存在，最终结果未成为 SUCCEEDED；同样使用旧 validate stub |

当前 Executor 调用 validate_context，成功返回还读取 validation.schema_ref；既有成功 fixture 没有此 key。以上是需同步的测试接线，不据此断言生产 ResultValidator 本身必然失败；也不能把测试原因已定位当成修复后通过。

通过部分包含实际 registry/Gateway 的失败/空流/冲突终端、真实 SDK 消息类型和 mapper（fake client），以及 execute/resume/fork 外层关闭后 fake client 的释放。Session 与 Tool 保存使用 fake/memory authority，不能证明真实 DB 事务、身份可查询、未知提交恢复、实际进程停止或模型汇总质量。

另查 tests/agent/test_subagent_capabilities.py、tests/agent/test_tool_gateway.py、tests/contracts/test_contracts.py，29 项通过；Web 的 `vitest run tests/components/RunResultPanel.test.tsx --no-file-parallelism` 为 1 文件、22 项通过。后者只是固定 Evidence 的显示测试，不证明真实分支执行或账本。补查首次命令误列了不存在的 test_subagent_sessions.py，收集阶段退出、没有执行测试；核对实际文件清单后按上述三份文件重跑，不把首次退出记为通过。

### 68.3 文档验证与保留范围

文档浏览回归增加“结果示例 → 子指令/输出 → 提交与版本兼容 → 代码接线”的真实点击，保留旧章节和离线边界。已执行检查如下，浏览器与布局调整后的复查分别举证，不借用 §67 的旧数字：

| 检查 | 本轮结果与范围 |
| --- | --- |
| 文档 build/check | 40 份 Markdown、1 份 ViewSpec 示例；链接、标题/锚点与派生内容一致 |
| 文档工具回归 | 17 项通过；build/browser/unit test 三份 Python 的 Ruff check 与 format --check 通过 |
| 契约 | 76 份 Schema、67 份 example 通过；没有修改公开字段或契约 |
| 首轮离线 Chromium | 80 次页面布局；60 个章节 × 4 宽度 × 2 字号，480 次章节布局、480 次原生刷新及 480 次恢复字号布局通过 |
| 导航与隔离 | 既有及新增阅读路径、搜索/键盘/前进后退通过；JavaScript 错误与 HTTP(S) 请求均为 0 |
| 改动范围 | 对照 686 份原文件，仅修改 12 份 Markdown、文档浏览检查和派生 index.html；没有新增、移动或删除项目文件 |
| 历史保留 | 959 个旧锚点保持可用；历史 §1–§67 的 258617 字节前缀完全不变 |

人工抽看首轮的手机结果表、桌面指令/输出表和手机提交流程。结果表可同屏阅读，但提交流程的末行需要横向滚动，随后改成短纵向步骤，并增加手机下流程不横向溢出的断言。新断言的匹配文本曾触发 Ruff 全角冒号检查，改为匹配稳定内容后已通过静态检查；未放宽布局断言或缩小全页字号。

布局调整后再次运行全书浏览检查：40 页、80 次页面布局、60 个章节的 480 次布局/480 次原生刷新/480 次恢复字号全部通过，新增提交流程不横向滚动的断言及阅读路径也通过；JavaScript 错误和外部请求仍为 0。复查生成 45 张工作区外截图，人工抽看最终手机提交流程、子指令/输出与当前事实表，确认长步骤不再截到右侧。补录此记录后重新生成并检查浏览版，保留来源与生成物一致。

本轮按 ProjectMind 技能保留 README 日文、设计正文中文与执行 Skill 的 version/hash 边界，不移动或改写 SKILL.md/references。业务代码、正式测试、JSON Schema 和配置保持原样，不清理用户已有缓存；未运行应用全量、真实 DB/模型、业务浏览器或部署。

## 69. 执行监督与停止文档续整（2026-09-09）

本轮继续文档整理。开始时保存 README/docs/PJM 的工作区外快照，保留已有真实 validator 测试接线、子 summary / TaskGroup 清理和首事件前取消改动；它们不是本轮新增功能。本轮不实现共享预算、不扩展子协议，也不修改业务代码来对齐文档。

### 69.1 设计与目录调整

新增执行监督正本，集中取消时机、task/client 所有权、原因与 lease 分类、收尾顺序、晚到信息及验收矩阵。Runtime §7.5 保留原标题与入口，详细规则不再维护两份；设计索引、Backend/Web/contracts README、API 使用、状态速查与 Runbook 导向同一责任页。浏览版在 Runtime 后安排执行监督，再进入预算。

修正“Run 终态等于执行已停止”的说明，并以具体例子区分取消意图、业务终态、client 清理和用量确认。只读核对还发现主 Executor 的 interrupted 映射不独立验证用户原因、终态后 close 异常可能未形成停止证据、Engine 移除 active 注册不证明真实进程退出。这些是从当前控制流推导的设计差距，不是本轮注入真实 SDK 故障得到的结论；修正要求与现状分开，不新增未定义的公开状态。

子分析设计同步真实 validator、摘要正文及整组清理的已有改动，移除原样未修复的旧描述；预算账本、子任务独立指令/结果与审计恢复仍未完成。接续顺序明确：可以评审新协议，但必须先满足 Run 共享预算门禁再开放，不以文档整理授予新能力。

### 69.2 工作副本的只读回归

在 PJM/backend 使用外置依赖、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH=src:<外置依赖目录>` 与 `pytest -q -p no:cacheprovider -o addopts=`，执行以下已有测试：

```text
tests/worker/test_agent_run_executor.py
tests/worker/test_first_event_supervision.py
tests/worker/test_preparation_supervision.py
tests/worker/test_worker_startup.py
tests/agent/test_subagent_lifecycle.py
tests/agent/test_subagent_provider.py
tests/agent/test_subagent_capabilities.py
tests/agent/test_result_validation.py
tests/agent/test_claude_engine.py
```

结果为 **124 项通过，无失败、无 skip**。§68 的三项旧 validator fixture 失败本次未再复现。首事件前测试覆盖 connect/receive × 用户取消/job 关停/失效 lease/wall timeout，使用真实 Engine 包装 fake client 与 fake RunService，并控制清理返回时点；子分析测试包含真实 registry/Gateway。它们不证明真实 SDK 子进程停止、DB 事务竞争、共享预算或业务模型质量。

首次命令只设置外置依赖而漏掉 src，导致 projectmind 无法导入，9 个模块收集错误且未执行用例。补齐运行环境后按原测试清单重跑得到上述结果；没有改源码、fixture 或断言来消除这次命令配置错误。

另以相同 Backend 环境运行 test_run_api.py、test_tool_gateway.py、test_contracts.py，**37 项通过**；在 PJM/web 执行 `node_modules/.bin/vitest run tests/lib/agentStream.test.ts tests/lib/runReplay.test.ts --no-file-parallelism`，**2 文件、13 项通过**。它们补查公开边界与 SSE 投影，不是实际业务浏览器、真实取消或完整应用回归。

### 69.3 文档验证与保留范围

文档浏览回归新增 Backend/Web/Runtime 旧入口到监督页的真实点击，并检查取消事实、首事件路径、原因分类、收尾与接续/验收的窄屏、放大文字和原生刷新。已执行范围如下，不复用 §68 的验证数字：

| 检查 | 本轮结果与范围 |
| --- | --- |
| 文档 build/check | 41 份 Markdown、1 份 ViewSpec 示例；本地链接、层级、章节与生成物一致 |
| 文档工具回归 | 17 项通过；build/browser/unit test 三份 Python 的 Ruff check 与 format --check 通过，阅读顺序回归固定 Runtime → 执行监督 → 预算 |
| 契约 | 76 份 Schema、67 份 example 通过；本轮没有修改公开契约 |
| 定点阅读 | 新监督页的 Backend/Web/旧 Runtime 入口真实点击通过，手机事实表与首事件流程无需横向滚动；0 JavaScript 错误、0 HTTP(S) 请求 |
| 首轮离线全书 | 41 页、82 次页面布局；67 个关键章节 × 4 宽度 × 2 字号，536 次章节布局、536 次原生刷新及 536 次恢复字号布局通过 |
| 导航与隔离 | 全部既有与新增路径、搜索/键盘/前进后退通过；JavaScript 错误与 HTTP(S) 请求均为 0 |

首轮全书生成 51 张工作区外截图，另有定点预览 6 张；人工抽看手机事实表、收尾对比、首事件阶段及桌面原因分类。既有历史正文未倒改，Runtime 原章仍可跳转，Skill 执行资产没有移动或改写。随后补齐 Workspace 的终态显示责任和本记录，再生成最终浏览版核对。

最终生成物对 Workspace 执行过程与本记录分别追加四宽度/两字号检查：16 次布局、16 次原生刷新、16 次恢复字号全部通过；监督页的 Backend/Web/旧 Runtime 阅读路径再次通过，仍为 0 JavaScript 错误、0 HTTP(S) 请求。最终定点检查保存 8 张外置截图，人工抽看完整首事件流程及 Workspace 手机正文；补录结果后再次 build/check，避免记录与派生内容脱节。

本轮新增 1 份设计，更新 16 份已有 Markdown、3 份文档工具/测试脚本和派生 index.html。对照开始时的 687 份文件，业务源码、Backend/Web 测试、Schema、配置及执行 Skill 保持原样；968 个旧锚点可用，历史 §1–§68 的 265038 字节前缀不变。没有删除或移动文件，不清理用户已有缓存；外置依赖和验证产物不进入工程。

按 ProjectMind 技能保留 README 日文、设计正文中文和执行 Skill 的 version/hash 边界。真实 DB/模型、业务浏览器、部署与全量应用回归未执行；进程清理、停止原因、晚到结算与剩余 R01–R13 不以本文档检查代替验收。

## 70. 提交边界与开发阅读入口文档续整（2026-09-09）

本轮按“继续整理文档”处理 docs 和正式代码目录的 Markdown。开始时保存工作区外快照；无意图 interrupted 分类、终态事务内取消复查、PRIMARY 查询与对应测试在本轮开始前已存在，不是本轮实现。业务代码、正式应用测试、Schema、运行 Skill 和配备配置不在本轮修改范围。

### 70.1 设计核对与阅读结构

执行监督页不再把已变化的主 interrupted 映射写成原样未修复。新增提交判断点：以同一 Run 锁下的持久事实区分取消先提交、终态先提交、重复取消与失效 lease，不以点击时间或 SDK 事件名判定。终态保存与成功返回、Session 的主/子归属、观测用量与完整账本分别说明。

只读检查发现，提问和提案走独立 suspend transaction，尚未在锁内复查取消意图；终态回归通过不能证明等待提交也已完成。文档将这一竞争窗口标为从控制流推导的风险，登记 checkpoint、待办、Outbox 与预授权 Effect 的后续验证，不冒充真实 DB 故障注入或本轮修复。

Backend README 接到实际原因/事务/Session 测试；Web README 按待办、Skill、输入、创建和取消分节；contracts README 增加取消 response / event / Problem 的入口。API 指南区分 REQUESTED、CANCELLED 与不可取消的 409，没有新增公开状态或臆造进程停止接口。

计划以[证据范围表](../planning/roadmap.md#当前证据怎么用)区分局部、全量、部署与文档验证，压缩已有完整历史的旧摘要并保留锚点。历史索引按执行/数据、文档/运维组织，文档维护约定按证据、目录语言、设计评审分节；领域正本仍各负其责，不迁移运行 Skill。

### 70.2 现有应用的只读验证

在 PJM/backend 使用外置依赖、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH=src:<外置依赖目录>` 与 `pytest -q -p no:cacheprovider -o addopts=`，执行以下既有测试，**157 项通过，无失败、无 skip**：

```text
tests/worker/test_agent_run_executor.py
tests/worker/test_execution_outcomes.py
tests/worker/test_first_event_supervision.py
tests/worker/test_preparation_supervision.py
tests/worker/test_worker_startup.py
tests/runs/test_execution_finalization.py
tests/runs/test_run_repository.py
tests/agent/test_subagent_lifecycle.py
tests/agent/test_subagent_provider.py
tests/agent/test_subagent_capabilities.py
tests/agent/test_result_validation.py
tests/agent/test_claude_engine.py
```

新增纳入核对的既有用例覆盖：没有持久意图的 interrupted、自称 user 的 payload、终态事务内取消覆盖成功/失败、没有意图却请求 CANCELLED、失效 lease、PRIMARY SQL 条件、无 Session 终态、跨 Run/Attempt 事件拒绝。它们使用 mock SQL session / fake RunService / fake client；PRIMARY 测试证明查询构造，不声称运行了真实多行查询或并发事务。

另执行 test_run_api.py、test_tool_gateway.py、test_contracts.py，**37 项通过**。PJM/web 的 `node_modules/.bin/vitest run tests/lib/agentStream.test.ts tests/lib/runReplay.test.ts --no-file-parallelism` 为 **2 文件、13 项通过**；这是既有投影回归，不包含本轮新增业务浏览器场景。PJM 下 `scripts/validate_contracts.py` 校验 **76 份 Schema、67 份 example** 通过。

本轮未执行全量 Backend/Web 回归、真实 DB、SDK 进程、模型、业务 UI 或部署。上述验证不覆盖共享预算、等待提交竞态、持久停止核对及 R01–R13 其余要求；接续状态仍以计划为准。

### 70.3 文档验证与保留范围

在 PJM/ 使用外置文档依赖与 Chromium，不创建 venv、不启动应用、不访问业务 API。先执行 build/check、`python3 -m unittest discover -s scripts/tests -v`，以及文档 build/browser/test 三份 Python 的 Ruff check / format --check；build 校验 **41 份 Markdown、1 份 ViewSpec**，工具回归 **17 项通过**。新增阅读检查中的全角标点匹配和长断言曾触发 Ruff；按原规则调整后复查通过，没有关闭 lint 规则。

首轮 `scripts/check_docs_browser.py` 对 **41 页、82 次页面布局**，以及 **77 个关键章节 × 四宽度 × 两字号**完成 **616 次布局、616 次原生刷新、616 次恢复字号布局**检查。既有导航与新增的提交/等待、Web → 契约 → 设计、历史 → 当轮证据 → 当前范围均通过，JavaScript 错误和 HTTP(S) 请求均为 **0**。

该轮全书检查保存 59 张外置截图，另一次定点阅读保存 16 张；定点的两章节四宽度/两字号检查为 16 次布局、16 次刷新和 16 次恢复字号，亦无 JS 错误或外部请求。人工抽看手机/桌面提交表、手机证据表、主题历史和维护约定，发现三列表格会把限制条件放到手机可视区之外，因而进一步合并为两列，并增加“不横向滚动也能同时看到证据与范围”的断言；自动通过不代替人工阅读判断。

两列表格与本记录加入后的定点复查已通过：停止/提交、Web → 契约 → 设计、历史 → 证据 → 当前范围的真实点击与无横向滚动断言通过；三章节共 **24 次布局、24 次原生刷新、24 次恢复字号布局**通过，仍为 **0 JavaScript 错误、0 HTTP(S) 请求**。该次保存 17 张外置截图，人工复看手机证据/历史两列和取消契约；补录结果后重新生成并检查最终浏览版。

对照开始时的 691 份文件，本轮变化限于 **13 份 Markdown、1 份文档浏览检查脚本和派生 index.html**。没有新增、删除或移动工程文件；原 **982 个锚点**保留，历史 §1–§69 的 **271080 字节**前缀未改。业务源码、Backend/Web 正式测试、Schema、配置和执行 Skill 保持原样，依赖与截图留在工作区外，不清理用户原有缓存。

按 ProjectMind 开发技能保留 README 的日文、设计正文的中文与 Skill version/hash 边界；设计冲突只在本轮权限内标明现状、影响和后续要求，不擅自修改业务实现。

## 71. 等待事务对齐与任务导航文档续整（2026-09-09）

本轮继续整理 docs 与正式代码目录的 Markdown；开始时保存工作区外快照。等待/普通事件的取消检查、Worker 的异常接续和相应测试在本轮前已存在，本轮不修改业务代码、正式应用测试、契约、配置或运行 Skill。

### 71.1 设计与阅读结构

- 更新监督页的旧缺口描述：三个写入路径已经共用取消检查；将“拒绝并回滚等待事务”和“重新验证 lease 后提交终态”绘成两个事务，说明中间崩溃/接管与真实 DB 验收仍未解决。
- 补清事件用量与 Session 投影的区别：取消事件保留已观察的 usage/cost，不代表 SESSION_INTERRUPTED 会更新 Session usage，更不等于 Run 共享账本。同步 Runtime、预算、变更指南与 Backend README 的责任入口。
- 将计划中四列 R01–R13 登记拆为稳定 ID 的独立小节，各保留状态、范围、验收，并增加分组跳转与 Backend → R07 的入口。原父章节/旧引用保留，既有开发范围不缩减。
- 历史索引增加本记录；[当前证据范围](../planning/roadmap.md#当前证据怎么用)分别标明本轮局部、此前公开投影、全量本地与部署。原历史结论保留，不因新核对倒改旧报告。

### 71.2 只读验证与边界

在 PJM/backend 使用已有外置依赖、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH=src:<外置依赖目录>` 与 `pytest -q -p no:cacheprovider -o addopts=`，执行以下既有测试：

```text
tests/runs/test_cancelled_execution_writes.py
tests/runs/test_execution_finalization.py
tests/runs/test_run_repository.py
tests/worker/test_execution_outcomes.py
tests/worker/test_agent_run_executor.py
tests/worker/test_first_event_supervision.py
tests/worker/test_preparation_supervision.py
```

结果为 **93 项通过，无失败、无 skip**。其中等待检查使用真实 repository 与 mock SQL/transaction，Worker 使用 fake service/client；覆盖序号落后/超前、错误 Run/Attempt/lease、service 异常传播、轮询仍为 false 与终态前失去 lease。不把异常传入 mock transaction 称为真实 PostgreSQL 回滚，也不把测试通过称为进程停止。

PJM 下 `scripts/validate_contracts.py` 校验 **76 份 Schema、67 份 example** 通过。本轮没有改变数据形状；未运行全量 Backend/Web、真实 DB、模型/SDK 进程、业务 UI、部署或任何外部写入验收。

### 71.3 文档验证与保留范围

文档浏览检查增加 R01–R13 的 H4 跳转/刷新、每项三段责任与手机不横向滚动断言，以及等待取消两事务流程的宽度检查。使用已有外置依赖与 Chromium，不创建 venv、不启动应用、不访问业务 API。

| 检查 | 本轮结果与范围 |
| --- | --- |
| 文档 build/check | 41 份 Markdown、1 份 ViewSpec；层级、链接、锚点与生成物一致 |
| 文档工具 | 17 项 unittest 通过；build/browser/test 三份 Python 的 Ruff check 与 format --check 通过 |
| 离线全书 | 41 页、82 次页面布局；90 个关键章节 × 四宽度 × 两字号，共 720 次布局、720 次原生刷新、720 次恢复字号布局通过 |
| 阅读路径 | 原有路径与新增 Backend → R07 → 全部任务均通过；JavaScript 错误、HTTP(S) 请求均为 0 |
| 定点阅读 | 等待事务、用量与本记录三个章节的 24 次布局、24 次原生刷新、24 次恢复字号布局通过；任务/停止/证据交接通过，0 JS 错误、0 HTTP(S) 请求 |

全书检查保存 65 张外置截图，定点阅读另有 26 张；人工抽看手机 R07、桌面 R02、两事务流程和手机用量投影。任务的状态/范围/验收可纵向连续阅读，短流程在手机不需横向滚动。补录结果后重新生成并核对最终浏览版，文档检查不替代业务 UI 或真实运行验收。

对照开始时的 693 份文件，变化限于 **9 份 Markdown、1 份文档浏览检查脚本和派生 index.html**。没有新增、删除或移动工程文件；原 **1001 个锚点**保留，历史 §1–§70 的 **277259 字节**前缀未改。业务源码、正式应用测试、契约、配置和运行 Skill 均保持原样，不清理用户已有缓存。

按 ProjectMind 开发技能保留 README 日文、设计正文中文，以及 Skill 的 version/hash 边界；未把运行资产迁入文档目录，也未用修改业务逻辑来对齐文档。

## 72. 预算账本边界与开发入口文档续整（2026-09-09）

本轮按“继续整理文档”处理。开始时保存工作区外快照；预算 DTO、三表/0030、repository/store 和相关测试在本轮前已存在。此次不实现预算接入、不修改业务代码或公开协议，既有 R01–R13 范围不缩减。

### 72.1 设计与阅读结构

- 将“共享账本整体未实现”改为有依据的分层说明：持久组件已有，普通创建、主/子执行、Worker startup 和受信核对方未接入。计划 R02 记为实施中而非完成，旧 §23.3 只保留到现行设计/计划的入口。
- 预算页集中内部载体、固定精度、预留状态和 A/B/C 提交规则，明确归一化 DTO 不会验证原始 SDK 计量，停止引用键/内部 claim 不会建立外部证据或服务授权。完整用量、真实事务与上线门禁仍须独立验收。
- Backend README 分开当前执行路径与账本接续，链接实现、模拟回归与实 DB 测试；同步领域模型、Runtime、子分析、Web/contracts README、AGENTS 和变更指南。Runbook 补齐 0030 的非空账本保护，不添加手工删账或退款命令。
- 保留现有分类和旧锚点，不机械移动文件。新增阅读路径从代码入口进入设计、内部状态、当前计划与迁移限制，设计正文不复制长篇测试日志。

### 72.2 只读验证与边界

在 PJM/backend 使用已有外置依赖、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH=src:<外置依赖目录>` 与 `pytest -q -p no:cacheprovider -o addopts=`，执行以下既有测试，**66 项通过，无失败、无 skip**：

```text
tests/runs/test_budget.py
tests/runs/test_repository_budgets.py
tests/runs/test_budget_store.py
tests/db/test_budget_migration.py
tests/db/test_alembic.py
```

覆盖精确单位、父子占用、锁后执行权、一次启动意图、停止/完整用量双条件、去重/冲突/超支、核对 lease 与提交响应模拟。迁移测试比较 model/DDL、保留检查和迁移图；fake transaction 模拟 commit/rollback，不是 PostgreSQL driver 或真实故障恢复证据。

只读检查了 `tests/db/test_real_budget_ledger.py` 的竞争、回滚、终态后结算用例；其 fixture 会创建/删除专用 DB，本轮未运行。也未执行模型/SDK 进程、业务 UI、全量 Backend/Web、部署或外部写入；不从 66 项局部通过推导 R02 全链路或 R01–R13 完成。

在 PJM 执行 `scripts/validate_contracts.py`，**76 份 Schema、67 份 example** 通过。这里只验证现有公开形状，本轮没有新增预算 API、RunEvent 状态或 Brief 字段。

### 72.3 文档验证与保留范围

使用已有工作区外文档依赖与 Chromium，不启动应用、不访问业务 API。已执行的检查如下；浏览验证不替代业务 UI 或模型执行。

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 41 份 Markdown、1 份 ViewSpec；层级、链接、章节锚点与生成物一致 |
| 文档工具 | 17 项 unittest 通过；build/browser/test 三份 Python 的 Ruff check 与 format --check 通过 |
| 离线全书 | 41 页、82 次页面布局；95 个关键章节 × 四宽度 × 两字号，760 次布局、760 次原生刷新、760 次恢复字号布局通过 |
| 阅读路径 | 新增账本 → 状态 → R02 → 门禁 → 迁移审查，以及既有预算、子分析、停止、证据、任务等路径通过；0 JavaScript 错误、0 HTTP(S) 请求 |
| 最终定点复查 | 账本载体、内部状态、术语与本记录四章节的 32 次布局/32 次原生刷新/32 次恢复字号布局通过；账本、原预算和全部任务交接通过，0 JS 错误、0 HTTP(S) 请求 |

首次定点检查在原有任务导航处失败：旧章新增的合法 R02 链接与主导航同名，整个页面的唯一匹配不再成立。将脚本限定到任务导航列表，并保留唯一性、全部 R01–R13 跳转及三段内容断言后复查通过；没有删除旧链接或放宽布局标准。

全书保存 73 张工作区外截图，最终定点复查另有 21 张。人工抽看手机内部状态/Backend 接线和桌面载体/R02；状态含义与限制可同时阅读，没有隐藏到横向滚动后。全书之后补齐术语页的残留旧描述与本记录，再生成浏览版并完成上表的四章节复查；补录结果后重新 build/check，保持来源与生成物一致。

对照开始时的 **703 份文件**，变化限于 **15 份 Markdown、1 份文档浏览检查脚本和派生 index.html**。没有新增、移动或删除工程文件；原 **1019 个锚点**保持可用，历史 §1–§71 的 **281731 字节**前缀不变。业务源码、Backend/Web 正式测试、契约、配置与执行 Skill 保持原样，不清理用户原有缓存。

按 ProjectMind 开发技能保留 README/AGENTS 日文、设计正文中文和 Skill 的 version/hash 边界。未把本轮文档整理扩大为业务代码改造，也未因存在账本组件而提前放开执行或公开协议。

## 73. 认证、凭据与安全开发入口文档续整（2026-09-09）

本轮按“继续整理文档”的请求处理，不修改应用源码、公开 Schema、配置、迁移、正式应用测试或执行 Skill。认证与 Secret 的设计核对属于文档事实校准，不宣称 R05 或既有 R01–R13 已完成。

### 73.1 设计与阅读结构

- 认证页先区分登录 challenge、session、session CSRF 与外部 Secret，以同会话两页面的短流程说明身份与写凭据不相同。移除“全部工作包已完成”的总括，将未提供的用户生命周期管理、锁前取时、GET 轮换、固定窗口限流及缓存/代理条件分开登记。
- 明确正常页面读取不应相互撤销写凭据；保留 Origin/CSRF、提权后重新认证与失效要求。现有单 hash 不能还原 token，具体可重取的会话级方案与旧会话迁移须先评审，不在文档整理中新增密码算法或公开字段。
- 新建 secret-storage.md 承接原认证 §7，保留全部旧标题作兼容入口。更正“零 at-rest”的范围，以及把主密钥直接 AES-256-GCM 误称为 DEK/KEK 信封结构的表述；既有设置名、字段和密文格式不改动。
- 补清创建时明文的必要接触面、公开 allowlist、FILE 词法检查的限制、key_version/kek_version 区别、active 同版 skipped 不校验解密，以及 API/Worker/CLI 配置切换和旧备份持钥条件。
- 设计索引、领域模型/术语、Backend/Web/contracts README、API 指南与 Runbook 分别负责规则、接线、界面和只读分诊；浏览顺序把领域模型、认证与 Secret 相邻放置。计划仅维护状态和证据入口，不复制完整设计。

以上风险由工作副本调用关系推导，不是本轮对真实 DB、多标签页会话、外部凭据或部署实施故障注入的结果。密码/会话/CSRF 原则参照并核对原有 OWASP 正本链接，信封术语核对 Google Cloud 的 DEK/KEK 定义；未引入新身份服务或 KMS。

### 73.2 只读验证与边界

在 PJM/backend 复用外置依赖，以 PYTHONDONTWRITEBYTECODE=1、PYTHONPATH=src:<外置依赖目录> 和 `pytest -q -p no:cacheprovider -o addopts=` 执行以下既有用例，**78 项通过，无失败、无 skip**：

```text
tests/auth/
tests/core/test_secret_crypto.py
tests/core/test_settings.py
tests/integrations/
tests/api/test_auth_api.py
tests/api/test_project_api.py
tests/api/test_integration_api.py
```

覆盖密码/短期 challenge、设置、加解密/AAD、resolver/domain、fake repository 与 API 身份/公开字段等已有条件。AuthService 测试只用 fake Redis 检查一次性 challenge；route 测试使用 fake service，不覆盖真实会话锁、撤权事务、缓存代理或跨页面竞争。FILE 在本组只核对 locator 的 domain 规则，没有验证实际文件读取/链接竞争；crypto 单测也不证明完整 Secret 轮换或备份恢复。

在 PJM/web 使用现有 node_modules 执行 `vitest run tests/api/authProjects.test.ts tests/api/mutationCsrf.test.ts`，**2 份文件、9 项通过**。验证 client 的登录 challenge、会话解析、Project 请求和 mutation header，不是真实 cookie 或多标签页验收。JUnit/JSON 输出留在工作区外。

在 PJM 执行 `scripts/validate_contracts.py`，**76 份 Schema、67 份 example 通过**。没有重新导出 OpenAPI 或修改公开形状。未运行全量应用、真实数据库、业务浏览器、模型、部署、轮换 CLI 或外部写入；不复用历史通过数字当作本轮证据。

### 73.3 文档验证与保留范围

复用已有工作区外文档依赖和 Chromium，只打开离线阅读版。执行 `build_docs.py`、`build_docs.py --check`、文档 unittest 与 `check_docs_browser.py` 后得到以下结果：

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 42 份 Markdown、1 份 ViewSpec；层级、本地链接、旧锚点与来源/生成物一致 |
| 文档工具 | 17 项 unittest 通过；build/browser/test 三份 Python 的 Ruff check 与 format --check 通过 |
| 离线全书 | 42 页、84 次页面布局；114 个关键章节 × 四宽度 × 两字号，912 次布局、912 次原生刷新、912 次恢复字号布局通过 |
| 阅读路径 | 新增认证 → 会话读取 → 分诊、Web → 契约 → Secret → 轮换/Runbook 以及既有阅读路径通过；0 JavaScript 错误、0 HTTP(S) 请求 |
| 补录后的定点复查 | 本节与 R05 的 16 次布局、16 次原生刷新、16 次恢复字号布局通过；新增凭据页的 path/章节搜索与认证交接通过，0 JS 错误、0 HTTP(S) 请求 |

定点初检暴露了两个实际阅读/定位问题：新增“契約入口”与已有子分析链接同名，改为明确的“認証の契約入口”；加密材料的长类名撑宽手机表格，改用“凭据引用/加密材料”作行标题，把准确代码名保留在说明列。随后定点和全书回归通过；没有删除合法链接、隐藏限制列或放宽无横向滚动的断言。

全书保存 91 张工作区外截图，补录后的定点复查另有 20 张。人工抽看手机四类凭据、Secret 结构、只读分诊、轮换步骤与 R05，以及桌面两页面流程/加密载体；对象含义和限制能同时阅读。补录结果后重新生成并核对浏览版，不把文档 browser 当作业务认证或安全恢复验收。

对照开始时的 703 份文件，本轮新增 1 份设计、更新 14 份已有 Markdown、3 份文档工具/测试脚本和派生 index.html。原 1,027 个锚点保留，历史 §1–§72 的 286776 字节前缀不变。没有删除或移动工程文件；应用源码、正式应用测试、公开契约、配置、迁移与执行 Skill 均保持原样。

按 pjm-project-dev 规范保留 README 日文、设计正文中文与 Skill version/hash 边界，不创建 venv，不清理用户既有缓存/产物。验证依赖、备份和报告留在工作区外；本轮不修改认证实现、凭据值或部署状态。

## 74. 生成模块边界与开发阅读路径续整（2026-09-09）

本轮按用户“继续整理文档”核对 generated FrontendModule；不实施全项目代码登记中的功能，不执行生成源码、builder、真实业务 iframe 或外部写入。设计在原页重排，不增加同义目录或第二套协议正文；当前范围仍见[计划 R04](../planning/roadmap.md#r04-生成模块)。

### 74.1 设计与代码核对

- 区分业务 module(SkillComposition) API、生成展示版本、禁脚本文档 preview 和平台标准视图；新增“图表坏了，任务没有失败”的纵向例子，说明回退不改变业务规则或运行事实。
- 核对 modules 的 domain/static_analysis/build_plan、DB model 与 0026：检查函数没有构建/投放消费者；发布 helper 只看 BUILT、有 hash、静态未拒绝，不验证 blob、报告或安全响应。CSP 常量仍含 unsafe-inline 等暂定项，现有字符串断言不是浏览器隔离证明。
- 核对 RuntimeManifest 的 frontend_module 只允许 null，runtime_defaults 设为 None；Module API version 常量不是公开 Schema/SDK。没有把内部模型或业务 modules endpoint 写成生成代码入口。
- 补齐完整构建输入与源码身份的区分、两阶段产物提交、历史兼容与独立展示选择。现有 `(skill_version_id, source_hash)` 无法表达仅换依赖/工具链/策略的新版本；不能覆盖旧内容或切换业务 SkillVersion 来修复显示。
- 明确预览同样经过首次执行门禁、传递依赖/真实出口另验、Host 实例与晚到消息失效、单页降级与全局停用分离、缓存/撤回与已下载副本的边界。参考 W3C CSP 与 HTML 消息安全规范，项目协议和威胁模型仍待实现/验收。

上述是源代码核对与设计修订，不是故障注入或运行时修复。同步设计索引、Workspace 旧章、领域模型/术语、变更指南、Backend/Web/contracts README、计划与历史入口，保持现有 API/Schema/migration/业务源码不变。

### 74.2 相关离线回归

复用工作区外 Backend 依赖，在 PJM/backend 运行 `tests/modules`、`tests/db/test_alembic.py::test_frontend_module_version_model_is_registered_in_metadata`、`tests/skills/test_manifest_gate.py`、`tests/api/test_composition_api.py`，**101 项通过**，无失败或跳过。分别证明现有纯函数、模型结构、Manifest gate 和 fake service API 的所列行为，不证明真实 DB 或生成界面可运行。

在 PJM/web 使用已有 node_modules 运行 `vitest run tests/api/modules.test.ts`，**1 份文件、3 项通过**，无失败或跳过。它验证 SkillComposition client 的发送/解析，不是 generated Host 的测试。JUnit/JSON 报告置于工作区外。

补充外置只读探针核对七个具体边界：未批准 host 不在允许名单中核验、异常依赖 section 被跳过、声明的版本范围未被固定、发布 helper 不验证产物、唯一身份只含精确 Skill 与 source、Manifest 拒绝非 null 生成引用、规范化清空该引用。所有断言通过，未安装依赖包、执行生成源码或连接网络；这些观察说明前置检查的限度，不是攻击场景或端到端验收。

在 PJM 执行 `scripts/validate_contracts.py`，**76 份 Schema、67 份 example 通过**。未重新导出 OpenAPI；没有修改 Schema、example、正式应用测试、配置或迁移。未运行全量应用、真实 DB、builder、代理响应、业务浏览器、部署与备份恢复。

### 74.3 文档验证与保留范围

复用工作区外文档依赖和 Chromium，只打开离线阅读版。已有检查与本轮新增生成界面路径得到以下结果：

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 42 份 Markdown、1 份 ViewSpec；标题层级、本地文件与章节链接通过，补录后的来源/生成物一致 |
| 文档工具 | 17 项 unittest 通过；build/browser/test 三份 Python 的 Ruff check 与 format --check 通过 |
| 离线全书 | 42 页、84 次页面布局；137 个章节 × 四宽度 × 两字号，1,096 次布局、1,096 次原生刷新、1,096 次恢复字号布局通过 |
| 实际阅读路径 | Backend → 显示失败 → 独立回退，以及 Web → 概念 → 契约 → 内容身份 → R04 和既有路径通过；0 JavaScript 错误、0 HTTP(S) 请求 |
| 补录后的定点复查 | 本节与 R04 的 16 次布局、16 次原生刷新、16 次恢复字号布局，以及生成界面的交接路径通过；0 JS 错误、0 HTTP(S) 请求 |

新增路径定点检查保存 18 张工作区外截图；全书检查另存 109 张，补录后的定点复查另有 34 张。人工抽看手机概念/限制表、展示选择/全局停用、Host 晚到消息与契约入口，以及桌面失败例子/流水线，关键含义和限制无需横向滚动即可同读。文档 browser 没有执行生成业务界面或测试其安全性。

对照开始时的 705 份文件，本轮更新 13 份 Markdown、1 份文档浏览检查脚本及派生 index.html；没有删除、移动文件或扩大内容扫描范围。原 1,051 个章节锚点保持，历史 §1–§73 的 292810 字节前缀不变。原有设计标题与 Workspace/PLAN 入口继续可用，当前状态没有倒写到旧记录。

按 pjm-project-dev 规范保留 README 日文、设计中文及执行 Skill 的 version/hash 边界；不创建 venv，也不清理用户已有产物。业务源码、正式应用测试、Schema/example/OpenAPI、配置、迁移与 Skill 均保持原样。补录后重新生成并核对浏览版；文档证据不刷新全量应用、真实 builder、业务浏览器或部署基线。

## 75. 会话现状对齐与文档维护指南续整（2026-09-09）

本轮继续整理文档。开始时工作副本已经存在会话 v2、migration 0031、稳定 CSRF、角色快照、锁后取时、no-store 与相关测试；不将这些代码归为本轮实现。原设计已加入 v2 段落，但同页例子、代码 README、API 指南、Runbook 和计划仍有旧 GET 轮换描述，本轮对齐这条阅读链。

### 75.1 设计与阅读结构

- 两页面例子改为同会话稳定 CSRF，并区分另一页面正常读取、重新登录换 cookie、过期/撤销与目标授权失败。保留旧章节锚点，旧行为只从 §73 追溯。
- 以两列表明确 v2 原值、HKDF 输入/参数、用途标记和数据库 hash，区分内部凭据版本与公开 JSON 版本。核对 RFC 5869 的默认 salt/用途分离和 OWASP 的 session 级 CSRF 取舍，不在文档整理中更换算法或增加密钥。
- 明确 User → AuthSession 锁后时效只保护认证判断，认证与具体业务提交仍是独立事务。角色快照不是撤权历史：停用后恢复、角色改回或直接改密码不能代替管理事务中的持久会话撤销与审计。
- Runbook 补 0031 切换与降级审查：旧会话重登、新旧 API 不混跑、保留所有 v2 审计行、失败时停止放行。不是本轮执行迁移、恢复或用户管理的记录。
- 同步 Backend/Web/contracts README、API 利用、领域模型/术语、设计/变更入口、R05 与证据索引；no-store 的保证限于匹配的 auth route，不扩展为所有代理响应。
- 文档维护指南把重复的逐领域浏览步骤整理为“按改动选验证范围 → 自动检查分层 → 阅读场景 → 人工判断”。具体 selector 和序列仍以检查脚本为准，件数和实施记录留在 history，不继续膨胀指南。

本轮没有移动目录或执行 Skill。既有 overview/design/planning/development/operations/acceptance/history 的职责不变，代码 README 负责接线而非复制完整协议。浏览检查新增 v2、缓存/代理、认证事务、切换与维护指南的章节，并真实点击协议到运维入口。

### 75.2 相关离线回归与派生文档

在 PJM/backend 复用外置依赖，用 PYTHONDONTWRITEBYTECODE=1、PYTHONPATH=src:<外置依赖> 和 `pytest -q -p no:cacheprovider -o addopts=` 检查以下现有测试：

```text
tests/auth/
tests/api/test_auth_api.py
tests/api/test_project_api.py
tests/db/test_auth_session_migration.py
tests/db/test_alembic.py
tests/contracts/test_contracts.py
```

初检 **64 通过、1 失败**：test_exported_openapi_is_current。逐项比较只有 `/api/v1/auth/session` GET 的 description 不同，保存快照还写 CSRF rotation，当前 route 已写稳定 token；没有 path、field、required 或 response 形状差异。说明影响后使用既有 export_openapi.py 同步派生文档，没有修改 route、测试、Schema/example 或认证算法。重跑同组 **65 项通过，无失败或跳过**，保留初检失败作为同步问题的证据。

在 PJM/web 使用现有 node_modules 运行 `vitest run tests/api/authProjects.test.ts tests/api/mutationCsrf.test.ts`，**10 项通过，无失败/跳过**。只验证 client 的发送/解析、no-store 和 opaque CSRF 接收，不证明真实 cookie、两个标签页或代理行为。

在 PJM 执行 validate_contracts.py，**76 份 Schema、67 份 example 通过**；它与上面的 OpenAPI 一致性是不同检查。API 字段、required、response 及 Schema/example 内容均未改变。

Backend 的 service 回归使用 fake transaction、时钟与 Redis，迁移回归检查结构和调用，不执行真实 PostgreSQL。未运行 tests/db/test_real_auth_sessions.py 或创建/删除数据库；也未运行部署、登录真实应用、完整用户管理、模型或 Secret 轮换恢复。报告和备份放在工作区外，不从其他轮次的绿色结果推导本次全部应用通过。

### 75.3 文档验证与保留范围

复用工作区外文档依赖和 Chromium，只打开 file 浏览版；没有安装新的运行依赖或创建 venv。

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 42 份 Markdown、1 份 ViewSpec；层级、本地链接与锚点、来源/派生物一致性通过 |
| 文档工具 | 17 项 unittest 通过；build/browser/test 三份 Python 的 Ruff check 与 format --check 通过 |
| 新会话章节定点 | 四章节 × 四宽度 × 两字号，32 次布局/原生刷新/恢复字号布局通过；协议到切换、认证/Secret 交接通过，0 JS 错误、0 HTTP(S) 请求 |
| 离线全书 | 42 页、84 次页面布局；144 个章节 × 四宽度 × 两字号，1,152 次布局、1,152 次原生刷新和 1,152 次恢复字号布局通过 |
| 全书实际阅读路径 | 既有全部路径与新增 v2 → 切换路径通过；0 JavaScript 错误、0 HTTP(S) 请求 |
| 补录后的定点复查 | 会话、切换、维护指南、R05 与本记录共 12 章节，96 次布局/原生刷新/恢复字号布局及认证/Secret 阅读路径通过；0 JS 错误、0 HTTP(S) 请求 |

初次定点保存 28 张截图，全书保存 113 张，补录后的定点复查另存 44 张，均在工作区外。人工抽看手机协议表/切换步骤、认证事务与验证选择，以及桌面两页面例子/阅读场景表；含义与限制能同时阅读。没有用隐藏列、缩小全页字体或执行业务操作来获得通过。补录结果后重新 build/check 并复查记录页，不以旧截图代替新增正文。

对照开始时的 709 份工程文件，变更为 14 份 Markdown、1 份文档浏览检查脚本和两份派生文档（index.html、OpenAPI 的一处 description）。没有新增、移动或删除工程文件；原 **1,068 个锚点**保留，历史 §1–§74 的 **298509 字节**前缀保持不变。业务源码、正式应用测试、Schema/example、配置、迁移与执行 Skill 没有改变。

按 pjm-project-dev 规范保留 README 日文、设计正文中文与执行 Skill 的 version/hash 边界；本轮格式检查生成的 Ruff 缓存移到工作区外保留，不清理用户旧产物。文档和局部回归不刷新全量应用、真实 DB、多标签页认证、部署或安全恢复的验收状态。

## 76. 登录防护设计与开发入口文档续整（2026-09-09）

本轮依照“继续整理文档”核对工作副本。开始时已有 LoginProtection、入口 middleware、429/503、Settings/环境接线和相关测试；没有在本轮实现这些业务变更。认证设计、Runbook 和 R05 仍写旧组合固定窗口与成功清零，现已按实际代码对齐，并保留尚未同步的公开链路。

### 76.1 设计与阅读结构

- 新增[登录入口防护](../design/login-protection.md)，承接认证 §2 的配额、TTL/退避和失败语义；原认证章节与四类凭据、两页面例子、v2/0031 锚点保留。登录前配额、会话/撤销和 Provider Secret 各有正本，不在 README 复制完整算法。
- 先用“一次登录、两次入口请求”解释来源计数，再用请求流程区分正文/Origin、账号/组合、challenge、密码与会话。明确限流错误可能先于请求错误，429 不等于账号停用或密码错误。
- 补清三个容易误用的保证：来源与账号检查是两次 EVAL；账号被拒绝前来源可能已写入；EVAL 的原子执行不等于失败回滚或与 PostgreSQL 一起提交。有限退避也不保证持续攻击无法再次阻塞用户。
- 明确 no-store 在登录路径可先于路由匹配生效，Redis 登录防护失败不自动撤销已有会话；状态损坏/无 TTL 与 key 丢失的行为不同。hash key 不是匿名数据，不靠清空 Redis 排障。
- 补 HTTP status/header 的契约检查与 Web 接续，增加隔离 Redis 验证的前置、作用和限制。同步 Backend/Web/contracts README、设计/变更入口、API 指南、Runbook、计划与历史索引。
- 浏览版将新页放在认证与 Secret 之间，补章节/真实点击路径；不改变 viewer 样式、搜索机制或嵌入范围。可执行 Skill 原文/references 保持原位，不作为普通 Markdown 搬动。

设计依据在新正本就近链接 OWASP Authentication、Redis Lua/SET 和 RFC 9110；固定数值是项目策略，不声称外部标准批准了整套实现。

### 76.2 代码核对与验证边界

在 PJM/backend 复用外置 Python 依赖，以 PYTHONDONTWRITEBYTECODE=1、PYTHONPATH=<外置依赖>:src 和 `pytest -p no:cacheprovider -o addopts= -q` 检查：

```text
tests/auth/test_login_protection.py
tests/auth/test_real_login_protection.py
tests/auth/test_auth_service.py
tests/auth/test_session_service.py
tests/api/test_auth_api.py
tests/api/test_login_protection_api.py
tests/core/test_settings.py
tests/db/test_auth_session_migration.py
tests/contracts/test_contracts.py
```

结果 **93 通过、1 失败，无跳过**。唯一失败为 test_exported_openapi_is_current：逐路径比较仅 login POST 与 login-context GET 的 responses 不同，当前 route 已增加 429/503 与 Retry-After，保存的 snapshot 尚无；components 不变。本轮没有运行 exporter 或修改应用契约来消除失败。它与 §75 当时仅 description 的差异不同，不倒改此前的成功记录。

真实 Redis 部分使用此前保留、来源与 checksum 已核对的 Redis 8.2.9 executable；fixture 启动自有 Unix socket、关闭 TCP 与持久化，只操作自有短期状态，结束后停止自有进程。没有安装系统服务、连接项目 Redis 或执行 FLUSHDB。测试覆盖 Lua、TTL、多个 service 对象并发、退避/安静期和异常状态；长退避通过调整专用测试时间戳模拟，不证明数分钟真实等待、API 多进程、故障切换或部署镜像。

在 PJM/web 执行 `vitest run tests/api/authProjects.test.ts tests/api/mutationCsrf.test.ts`，**10 项通过，无跳过**。源码核对确认当前 ApiProblemError 未读取 Retry-After，LoginPage 未提供 429/503 专用三语反馈；既有 client 测试通过不证明这些缺口已经实现，也不证明重复 submit 与 unmount 后回调的真实交互。

在 PJM 执行 validate_contracts.py，**76 份 Schema、67 份 example 通过**，与上面的 OpenAPI 一致性失败分别记录。本轮不运行真实 PostgreSQL fixture、迁移/恢复、生产登录、Compose、模型、外部写入或 Secret 轮换。当前 R05 保留完整未完成范围，局部通过不等于该领域或全项目交付。

### 76.3 文档验证与保留范围

复用工作区外文档依赖和 Chromium，不创建 venv、不修改应用运行依赖。初检新响应表在 390px 需要横向滚动，完整错误码把首列撑宽；随后改用短状态标签，完整 code 放进含义列，没有隐藏内容、缩小字号或放宽检查。

截图抽查还发现原 Runbook 分诊表的长错误码将处理说明推到屏幕外，同步改为短标签 + 完整 code/处理说明，并加入同屏阅读断言。原 status/code 和安全停止条件不变，不以仅整页不溢出代替实际可读性。

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 43 份 Markdown、1 份 ViewSpec，层级、本地文件/章节链接与来源/生成物一致性通过 |
| 文档工具 | 17 项 unittest 通过；build/browser/test 三份 Python 的 Ruff check 和 format --check 通过。新增读取顺序已纳入原有回归 |
| 全书页面布局 | 43 页 × 390/1440px，86 次检查通过；历史提示按页面类别保留 |
| 修改章节定点 | 25 章节 × 320/390/768/1440px × 16/24px；200 次布局、200 次原生刷新、200 次恢复字号布局通过 |
| 真实阅读路径 | Backend → 登录例子 → Runbook → 计数 → Web → 契约 → 证据，以及既有认证/Secret、搜索/导航路径通过；0 JavaScript 错误、0 HTTP(S) 请求 |
| 补充概览与记录复查 | 架构/术语、响应、契约、R05 与本记录共 7 章节，56 次布局/原生刷新/恢复字号检查通过；全书两宽度和上述阅读路径再次通过，另存 78 张截图。此记录之后的分诊表调整单独复核 |

本轮按正文和新增页面的影响选择范围，没有运行所有既有领域的全量章节矩阵；不复用 §75 的全量数字。本批复查保留 114 张工作区外截图，人工抽看手机响应/配额表和分诊、桌面执行流程，含义与限制能同屏阅读。文档浏览没有调用业务登录、执行正文命令或操作 Redis。

对比开始时的退避文件与源码清单，本轮整理 **17 份 Markdown（16 份更新、1 份新增）**，同步三份文档工具/测试和派生 index.html。保留 **1,078 个原章节锚点**，历史 §1–§75 的 **304796 字节**前缀不变。应用与工具范围的 633 份非 Markdown 文件中，只有三份文档工具/测试发生变化；正式业务源码、应用测试、Schema/example/OpenAPI、迁移与执行 Skill 保持原样。现行 overview/design/planning/development/operations/acceptance/history 的分工不变，只新增一个设计主题并接通入口。

按 pjm-project-dev 约定使用日文 README、中文设计，并保留可执行 Skill 的 version/hash 边界及用户原有改动和产物。文档整理和局部回归不刷新全量应用、真实 DB、生产代理或完整安全验收结论。

## 77. 登录客户端现状与验证入口文档续整（2026-09-09）

本轮依照“继续整理文档”接续，未继续实施全项目代码任务。开始时工作副本已有 Web Retry-After/专用三语/ref 与 abort、challenge 保存确认/标记检查，以及新的 Redis/ASGI/Session 测试；这些既有变更保留，不计为本轮文档工作新实现的功能。

### 77.1 设计与阅读结构

- 对齐[登录防护](../design/login-protection.md)的当前客户端行为。共享 HTTP client 解析语法，loginFeedback 判断登录范围；503 是通用服务不可用提示，不从文案反推 Redis 根因。静态等待不是倒计时、强制锁按钮或自动重试。
- 纠正配额例子：通过账号/组合检查的尝试不必到达密码验证，challenge 拒绝也可能已计数。补清 challenge 保存必须确认成功、异常标记与缺失状态的区别。
- 新增[提交、离页与结果未知](../design/login-protection.md#提交离页与结果未知)的短流程。UI 的 ref/abort 防止重复在途请求和晚到回调，不能撤销已提交 Session；密码请求没有 Run 式的幂等重放，不用自动 logout 补偿。
- Backend/Web README 补实现到测试的接续表和页内动作导航；contracts README 说明 route metadata 缺 Problem content/schema，exporter 一致性通过也不自动补全声明。Workspace、API、Runbook、设计责任索引和变更指南统一引用正本，不复制第二套登录协议。
- 本地指南新增客户端局部回归、ASGI + Redis 的真实/替身范围，以及实 PostgreSQL fixture 的建库/强制删库前置。文档检查不借真实 DB、生产登录或部署操作取得证据。
- 计划概览改为“能力 + 基础与接续”两列，并跳转 R01–R13。手机上可同时读到现状与限制；完整工作范围、旧章节 ID/锚点和历史事实保留。

### 77.2 代码核对与验证边界

在 PJM/backend 使用既有外置 Python 依赖和经核对的 Redis 8.2.9 executable，运行 `pytest -p no:cacheprovider -o addopts= -q`，范围为：

```text
tests/auth
tests/api/test_auth_api.py
tests/api/test_login_protection_api.py
tests/core/test_settings.py
tests/db/test_auth_session_migration.py
tests/contracts/test_contracts.py
```

结果 **116 通过、1 失败，无跳过**。失败仍是 test_exported_openapi_is_current：保存的 login POST 与 login-context GET 缺新增 429/503 响应。本轮未运行 exporter，也没有修改 route 或契约来取得绿色结果。源码另确认 LOGIN_PROTECTION_RESPONSES 只声明描述与 Retry-After，没有两种 Problem body 的 content/schema；这是公开同步后仍需补齐的声明范围，不是另一个已运行的失败测试。

真实 Redis fixture 只启停自己创建的 Unix socket process，禁用 TCP 与持久化，不连接项目 Redis、不执行 FLUSHDB。实际 Lua/TTL 和多个 service 对象并发继续通过；新增 ASGI + Redis 验证错误正文计数、跨来源账号配额和损坏状态拒绝，DB/密码访问被禁止。成功不清零的测试使用真实 Redis 和密码校验，但 Session DB 是 fake。challenge 未确认/损坏/超时/断连的 service 检查也使用替身。上述结果不证明真实 PostgreSQL 提交、HTTPS、多个 API 进程或 Redis 故障切换。

在 PJM/web 运行[登录局部回归清单](../development/local-development.md#ログイン-client-の局部回帰)，**104 项通过，无跳过**：HTTP 元数据 48、auth/Project 14、mutation CSRF 3、三语纯反馈 39。证明 mock fetch 下 header、失败停止点、abort 和反馈映射；没有挂载 LoginPage，不证明 ref 的实际 DOM 重复 submit、卸载回调、语言切换、键盘、窄屏或 cookie 行为。

在 PJM 运行 validate_contracts.py，**76 份 Schema、67 份 example 通过**。它与 OpenAPI 一致性失败分别记录；本轮未重新执行全量 Backend/Web、真实 PostgreSQL、迁移/恢复、业务浏览器、Compose、模型或外部写入。当前接续从已存在组件继续，R05 与全项目均未完成。

### 77.3 文档验证与保留范围

复用工作区外依赖和 Chromium，没有创建 venv 或改动应用依赖。主轮检查结果如下；这是文档浏览，不执行正文中的登录、迁移或恢复命令。

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 43 份 Markdown、1 份 ViewSpec；层级、本地链接/锚点和生成物一致性通过 |
| 文档工具 | 17 项 unittest 通过；build/browser/test 三份 Python 的 Ruff check 与 format --check 通过 |
| 全书页面 | 43 页 × 390/1440px，86 次布局通过；历史提示保持正确 |
| 修改章节 | 31 章节 × 320/390/768/1440px × 16/24px，248 次布局、248 次原生刷新、248 次恢复字号布局通过 |
| 实际阅读路径 | 登录/认证/Secret、搜索导航、R01–R13 工作项和 Backend/Web 页内入口通过；0 JavaScript 错误、0 HTTP(S) 请求 |

主轮保留 142 张工作区外截图，抽看手机计划概览、PostgreSQL 前置和登录请求流程。概览的基础与限制能同时阅读，但新流程图虽然整页布局通过，分支仍需要局部横向滚动；随后收窄为纵向流程，并在真实点击路径增加“完整分支不被裁出视口”的检查，没有修改 viewer 样式或隐藏分支。修订后的流程与本节结果另做定点复查。

修订后对响应、流程、验收、DB 前置、计划和本记录共 8 个章节复查：64 次布局、64 次原生刷新、64 次恢复字号布局通过；43 页两宽度与上述阅读路径再次通过，0 JS 错误、0 HTTP(S) 请求，另存 96 张截图。人工确认手机流程的拒绝与离页分支完整可见，并抽看桌面流程和手机证据表。本轮按正文影响选择范围，未运行其余既有领域的全量章节矩阵；不复用旧轮全量数字。

来源比对确认本轮修改 **16 份 Markdown**、一份文档浏览检查脚本和派生 index.html，没有新增/移动/删除文档。原 **1,093 个章节锚点**保留，历史 §1–§76 的 **311916 字节**前缀不变；核对的 660 份非 Markdown 工程文件中只有文档浏览检查脚本改变。正式业务源码、应用测试、Schema/example/OpenAPI、迁移、配置与执行 Skill 未在本轮修改。

遵循 pjm-project-dev 的 README 日文、设计中文、正本分工和先验证再收尾约束；未覆盖的应用功能与真实环境验收继续保留在计划。已有工作副本和用户产物原样保留，文档回归不替代业务浏览器、实 DB 或全项目交付。

## 78. 验收文档分层与开发导航续整（2026-09-09）

本轮依照“继续整理文档”工作，保留开始时已有的业务代码、契约和测试。认证错误的 OpenAPI 定义、示例及 LoginPage browser runner 均是本轮开始前已有；本轮只核对其证据并更新文档，不将这些功能归为本轮新实现。

### 78.1 设计与阅读结构

- 将 JAF 的样本、Gold、Rubric、计分与报告移入 [Benchmark 正本](../acceptance/jaf-benchmark.md)。原验收页负责迁移/资源/运行/失败，原 §10–13/§17 的全部章节锚点保留为跳转入口。
- 修正责任混淆：由评价负责人冻结 case 与评分条件，不写进 Skill 输入；主质量分母、额外 Provider 配对和人工重跑分开记录。补充 24/30、2/30 的具体例子，保留原 30 case 分层与质量阈值。
- 核对现有 Evaluation 只能追加单个 Result 的 rating/verdict/comment/revisions，不是五维 Rubric 或整轮 benchmark 管理。失败且没有 Result 的 case 在评价侧保留；报告发布结论不冒充 EvaluationVerdict 或 SkillVersion 发布操作。
- 变更指南按运行/数据、Skill/界面、身份/交付分组，把长三列表和重复进度改为两列阅读入口与稳定核对方法；精确状态仍由计划 R01–R13 负责。
- Backend README 将低频的生成 module 细节放到常用链路之后，保留原锚点；补齐 Backend/Contracts 页内入口。Skills、项目入口与总索引接通 JAF 运行/评价分工，不移动或改写任何执行 Skill。
- 对齐当前认证错误声明与真实 LoginPage + mock API 的验证范围，替换计划中 §77 时点已经过时的缺口；实会话、用户生命周期、跨页面、代理和恢复残项继续保留。

### 78.2 工作副本核对与验证范围

复用已有外置依赖，在 PJM/backend 运行 `pytest -p no:cacheprovider -o addopts= -q`，范围为 tests/auth、tests/api/test_auth_api.py、tests/api/test_login_protection_api.py、tests/core/test_settings.py、tests/db/test_auth_session_migration.py 和 tests/contracts/test_contracts.py，**123 项通过，无跳过**。未运行真实 PostgreSQL fixture，也没有创建/删除测试 DB。

另以 exporter 的排序、缩进、UTF-8 与末尾换行计算当前 app 的 OpenAPI，和保存文件 **294728 字节完全一致**；只读比较，未执行 exporter 写回。认证错误 schema mirror、实际 Problem body/media type/header 与声明的测试通过；这不表示所有业务 API 已逐字段审计。validate_contracts.py 检查 **76 份 Schema、69 份 example 通过**。

评价边界另核对 evaluations 的 domain/service/repository、公开 route 与 Schema，并运行 tests/evaluations、tests/api/test_evaluation_api.py，**17 项通过，无跳过**。该检查使用现有局部测试，不证明实 DB、五维评分服务或真实业务质量。

在 PJM/web 运行完整 vitest，**40 个文件、430 项通过，无跳过**。随后在专用 loopback Vite 5192 上运行既有 check_login.py，**62 个场景通过**：54 个三语/两宽度反馈场景，以及 context 拒绝、重复提交/手动新 challenge、语言切换、native abort 和忽略 signal 的晚到结果共 8 个交互场景。使用真实 LoginPage/StrictMode 与测试专用 cookie，API 全部由 runner 替代；未登录真实账号，未接触真实 Backend/DB/Redis。截图保留在工作区外。

Backend 的 Redis 场景使用独立 Unix socket process 与既有受控 executable，禁用 TCP/持久化，只清理自身资源。ASGI 拒绝与成功 Session 的 fake 范围沿用[设计说明](../design/login-protection.md#开发接续与验收)，不扩大为实 DB、HTTPS、多 API 进程或 Redis 故障切换的证据。

本轮没有运行全量 Backend、typecheck/build、真实 PostgreSQL/migration、Compose/恢复、模型质量、真实外部资源或 effect write。已有外部报告中的全量数字不转记为本轮结果；文档分层不代表已经构造真实 Gold 或完成 JAF benchmark。

### 78.3 文档验证与保留范围

复用工作区外文档依赖与 Chromium，不创建 venv。文档工具同步阅读顺序、旧入口到新正本的实际点击，并保留既有检查范围和 HTTP(S) 阻断规则。

| 检查 | 结果与范围 |
| --- | --- |
| build/check | 44 份 Markdown、1 份 ViewSpec；本地链接/锚点、标题层级、示例和派生物一致性通过 |
| 文档工具 | 17 项 unittest 通过；三份 Python 文档工具/测试的 Ruff check 和 format --check 通过 |
| 全书页面 | 44 页 × 390/1440px，88 次布局通过，历史提示正确 |
| 本轮重点章节 | 44 章节 × 320/390/768/1440px × 16/24px，352 次布局、352 次原生刷新、352 次恢复字号布局通过 |
| 真实点击 | JAF 运行/评分、Skill 与代码 README、旧章节往返、搜索/键盘导航、登录/认证/Secret、R01–R13 和证据索引通过；0 JS 错误、0 HTTP(S) 请求 |

以上是按正文/阅读路径影响选择的定点章节检查，并非所有既有章节的全量矩阵。保留 **174 张文档截图**，人工抽看手机的数据隔离表、执行/评价流程、开发入口、Evaluation/发布边界，以及桌面的评分分母和流程；关键信息可完整阅读。另保留 **37 张登录组件截图**并抽看手机等待提示，两类截图不相互替代。

本轮整理 **15 份 Markdown（14 份更新、1 份新增）**，同步三份文档工具/测试和派生 index.html。原 **1,101 个章节锚点全部保留**；历史 §1–§77 的 **318543 字节**前缀不变。核对的 661 份非 Markdown 工程文件中，仅三份文档工具/测试改变；没有修改业务源码、应用测试、Schema/example/OpenAPI、迁移、配置或执行 Skill。

本轮自己的 Vite 与 Redis fixture 已结束，已有进程和产物不清理。遵循 pjm-project-dev 的日文 README、中文设计、正本分工和验证范围约束；本轮整理不刷新全量 Backend、部署、真实会话或 JAF 模型质量结论。补录文字后再次生成并检查浏览版，对本节、文档维护说明与样本冻结边界做定点复查。

## 79. 运维文档分层与发布边界续整（2026-09-09）

本轮继续整理文档，保留开始时已有的应用代码和未完成改动。正式代码目录只更新 README 与文档工具；没有实现用户生命周期、维护模式或新的发布控制器。

### 79.1 责任与阅读结构

- 运维分为[首次启动](../operations/quickstart.md)、[发布与迁移](../operations/deployment.md)、[备份与恢复](../operations/backup-recovery.md)、[症状排障](../operations/runbook.md)。原 Runbook 的备份、迁移、恢复和回退命令移到唯一正本，旧标题/章节移至尾部作兼容入口。
- 核对 WorkerSettings：dispatch=false 只限制 Run/Effect 的新 Outbox 配送，不能阻止已有 job、Schedule、期限恢复或解释任务。以已入队 Effect 与到期 Schedule 的例子说明停写边界，不把功能设置改称维护模式。
- 修正发布顺序：模型 smoke 需要 Worker，不能作为“任何 Worker 启动之前”的目标环境门禁。独立环境验收、目标只读核对、后台恢复许可、目标验收和普通入口放行分开；只建专用 Project 不隔离旧队列/cron。
- 恢复以时间线说明“旧权限可能回来、远端 commit 不会撤销”；备份清单无 Secret 值不等于 dump/blob 不敏感。目录创建失败和空变量不能继续输出 dump，保留原单事务恢复、错误停止与外部对账限制。
- 核对 export-images.ps1 只导出本地已有 image，不 build/pull；补充应用 image 缺失拒绝、第三方 image 可被排除、旧 archive/实际 image ID 保留，以及 make deploy 不自动回退的边界。
- 同步项目/Backend/Scripts/Images README、设计中的运维引用、开发指南与 R11。关键停写表和恢复点表压成两列，长标识分组后手机可同屏读取条件；未改变 viewer 的样式或应用 UI。

### 79.2 核对与验证范围

复用外置依赖，不创建 venv。Backend 仅运行 tests/worker/test_worker_dispatch.py、test_schedule_tick_job.py、test_effect_recovery_job.py 与 tests/ops/test_preflight.py、test_smoke_client.py，使用 `pytest -p no:cacheprovider -o addopts= -q`，**15 项通过，无跳过**。这些测试含 fake service/relay/transport，不是实际 Worker/Redis/DB、模型 smoke 或停止演练。

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 46 份 Markdown、1 份 ViewSpec；本地链接、锚点、层级、示例与生成物一致性通过 |
| 文档工具回归 | 18 项 unittest 通过；三份文档 Python 工具/测试的 Ruff check、format --check 通过。新增 13 个旧运维章节到正本的对应检查 |
| 相关契约与配置 | 76 份 Schema、69 份 example、8 个 Compose service 的静态检查通过；未运行 OpenAPI exporter，也未修改其快照 |
| 操作示例 | 18 个 Bash/Sh 代码块通过 bash -n 语法检查；没有执行其中的部署、备份、恢复或 smoke 命令 |
| 全书页面 | 46 页 × 390/1440px，92 次布局通过，历史提示正确 |
| 重点章节 | 33 章节 × 320/390/768/1440px × 16/24px，264 次布局、264 次原生刷新、264 次恢复字号布局通过 |
| 阅读路径 | 新旧运维入口往返、代码 README、搜索和键盘，以及既有设计/契约/R01–R13 交接路径通过；0 JS 错误、0 HTTP(S) 请求 |

首次浏览检查发现定点范围依赖数组位置、旧 H3 断言和手机关闭目录后的搜索操作需要同步；随后按实际文档/章节焦点和正常目录操作修正。新增可读性断言还检出长串 Run/Effect/Interaction/Proposal 撑宽表格，已改为分项表述；上述结果来自修正后的完整选定范围，不隐藏初次失败。

保存 **145 张文档截图**于工作区外，人工抽看手机停写边界、恢复时间线、恢复点清单和桌面发布门禁。命令语义另对照 Docker Compose 与 PostgreSQL 官方文档核对，引用放在对应手册；离线浏览器未访问这些外链。

本轮未运行全量 Backend/Web、真实 PostgreSQL/migration、Compose 配备/恢复、PowerShell export、模型或外部写入。这些操作超出本轮文档整理范围，真实恢复/外部写入还需明确获准的专用目标；静态检查不替代这些验收，不将 R11 或整个项目记为完成。

### 79.3 保留与交付

本轮整理 **22 份 Markdown（20 份更新、2 份新增）**，同步三份文档工具/测试和生成 index.html。原 **1,134 个章节锚点保留**；历史 §1–§78 的 **324736 字节**前缀保持不变。核对的 **668 份非 Markdown 工程文件**中，只有三份文档工具/测试改变；业务源码、应用测试、契约、迁移、配置与执行 Skill 均未修改。

遵循 pjm-project-dev 的日文 README、中文设计、正本分工与验证范围要求，未移动或改写 Skill 输入资产。只将本轮生成的 Ruff 缓存移到工作区外保留，不清理既有进程和产物。补录后重新 build/check，并复查当前索引、故障排查标题与本节的浏览入口；不据此刷新应用或部署基线。

## 80. 用户生命周期设计与工程入口续整（2026-09-09）

本轮继续文档整理，保留已有业务源码与未完成改动，不接入新的账户 API、migration 或页面。设计独立为[用户生命周期与安全管理](../design/user-lifecycle.md)，认证页保留原管理章节及三个子章节的兼容链接。

### 80.1 设计修正与代码核对

- 核对已有 users 的 DTO/service/repository、共用会话 validator、model/0032 和 AuthService.admit_password_change。区分这些内部部件与尚未装配的公开 route、契约、Web 及专属回归，不沿用“完全没有实现”或“管理功能已可用”的笼统表述。
- 修正 request ID 的现状：middleware 仍优先采纳客户端 header，服务器 UUID 是待接入的审计要求。格式校验不证明来源可信，request ID 也不是请求幂等键。
- 用停用后再启用的例子说明持久撤销；明确末位活动 ADMIN、Organization → User → Session 锁顺序、授权判定时点和密码计算后的重验，不承诺物理 commit 时间或全部业务/进程的即时撤权。
- 区分版本冲突/no-op、撤销行数/在线人数、密码错误/会话失效/结果未知；email 唯一性不充当完整幂等回放。改密共享配额、匿名 challenge 与会话 CSRF 分开，既有 helper 不自动保护新 route。
- 补清审计与历史：bootstrap 尚无安全事件接线；目标采用新 ADMIN 自身与 CLI 操作 UUID，同事务追加。0032 不补造旧用户事件；唯一键与 downgrade guard 不等于数据库级不可改写保护。
- 同步领域模型、术语、Workspace/登录防护的责任交接、API 用法的现行边界、开发指南、R05 和迁移审查；Backend/Web/contracts README 提供已有部件到目标接口与验收的直接入口，不复制设计正文。

### 80.2 文档验证与保留范围

只使用文档构建、静态契约与离线浏览验证。应用源码、应用测试、公开 Schema/example/OpenAPI、migration、配置和执行 Skill 不在本轮修改范围；不把文档测试刷新为 Backend/Web 或真实环境的验收。

复用工作区外依赖与 Chromium，不创建 venv。首次选定范围的结果如下；这是文档的定点章节检查，并非所有既有章节的全量矩阵：

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 47 份 Markdown、1 份 ViewSpec；本地链接、锚点、层级、示例和派生物一致性通过 |
| 文档工具回归 | 19 项 unittest 通过；三份文档 Python 工具/测试的 Ruff check、format --check 通过。新增四个旧用户管理章节到正本的对应检查 |
| 相关契约 | 76 份 Schema、69 份 example 静态验证通过；没有运行 OpenAPI exporter 或修改公开快照 |
| 全书页面 | 47 页 × 390/1440px，94 次布局通过，历史提示正确 |
| 重点章节 | 35 章节 × 320/390/768/1440px × 16/24px，280 次布局、280 次原生刷新、280 次恢复字号布局通过 |
| 阅读路径 | 用户管理的新旧章节往返、代码 README、目标契约/界面/计划、文件名搜索与既有全部 handoff 通过；0 JS 错误、0 HTTP(S) 请求 |

人工抽看手机的停用/启用、内部部件、错误处理，以及桌面的目标接口截图。自动布局通过后仍发现示意图长行需要横向滚动，随后改为短步骤，并增加流程不溢出的断言；没有为此修改 viewer 样式。补录与图示调整后重新生成浏览版，复查受影响章节和阅读路径。

调整后的第二轮覆盖账户例子/事务/内部接线/界面、本章入口与 §80.2、R05 和文档维护的验证范围，共 8 章节 × 同一四宽度/两字号：64 次布局、64 次原生刷新、64 次恢复字号布局通过。全书 94 次布局及全部 handoff 再次通过，0 JS 错误、0 HTTP(S) 请求；手机流程不溢出断言通过，并重新人工查看两张短步骤截图。共保留 157 张离线文档截图于工作区外；两轮均不属于所有既有章节的全量矩阵。

本轮整理 **18 份 Markdown（17 份更新、1 份新增）**，同步三份文档工具/测试与 index.html。原 **1,163 个章节锚点保留**，历史 §1–§79 的 **329843 字节**前缀不变。与开始时 archive 比较的 **629 份非 Markdown 文件**中，只修改三份文档工具/测试与派生 index.html；未覆盖或清理原有业务改动。

遵循 pjm-project-dev 的日文 README、中文设计、正本分工和验证范围要求。未运行应用全量回归、真实数据库迁移/锁竞争、业务 UI、Compose 配备/恢复、模型或外部写入；没有进行账号创建、改密或会话撤销操作。文档浏览中的“管理”“恢复”均为离线阅读，不能作为 R05 或项目整体完成的证据。

## 81. 用户管理接线与契约交接文档续整（2026-09-09）

本轮按“继续整理文档”推进，不延续业务实现修改。保留开始时已有的用户管理源码、应用测试和契约文件；以下核对说明它们接到哪一层，不将本轮文档更新称为新功能交付。

### 81.1 工作副本与失败证据

源码已不同于 §80 的核对时点：API startup 装配 UserService，users route 增加 9 个管理 HTTP 操作，9 组 Schema/example 已进入两个注册表；本人改密接来源与 actor 账号配额，middleware 生成服务器 UUID，bootstrap 追加首 ADMIN 的 CREATED。尚无专属 Web client、账户页面/hash route；保存 OpenAPI 未收录上述 9 个操作。

复用外置 Backend 依赖，工作目录为 `PJM/backend/`；以下均使用 `pytest -p no:cacheprovider -o addopts= -q`，不运行真实数据库 fixture、CLI、Redis 服务或业务页面：

| 检查 | 实际结果与范围 |
| --- | --- |
| tests/users 与下行 API/契约合跑 | 收集阶段失败：test_user_service.py 的 `from conftest import NOW` 解析到 tests/api/conftest.py。未进入测试执行，不记为部分通过 |
| tests/users 单独运行 | 24 项通过；实 UserService 与 mock repository/transaction，不能证明 SQL 锁竞争或完整回滚 |
| tests/api/test_users_api.py、test_auth_api.py、test_login_protection_api.py 与 tests/contracts/test_contracts.py | 57 项通过、1 项失败，无跳过；失败为 test_exported_openapi_is_current。HTTP 用例使用实际 middleware/route 与 fake services，不证明实际 PostgreSQL/Redis/HTTPS |
| scripts/validate_contracts.py（PJM/） | 85 份 Schema、78 份 example 通过；包含已有的 9 组用户契约。这不改变 OpenAPI 一致性失败的结论 |

本轮未执行 exporter、未回写 OpenAPI，也未修改应用测试来绕过收集失败。分开通过不能累加为一套全量绿色基线；先修合跑，再接齐消费者，真实事务与环境验收保留在 R05。

### 81.2 设计与阅读交接

- 用户设计先展示 API → Schema → 快照/Web 的接线图，保留原章节锚点；不再笼统写成“只有内部部件、没有公开 route”。认证、领域模型、登录配额、Workspace、运维和当前计划同步到同一边界。
- 说明改密与登录共享额度，但使用会话 CSRF、没有匿名 challenge；400 当前密码错误与 401 会话失效分开，三种 409 不能统一靠刷新版本解决。
- 澄清稳定排序不等于跨页一致快照，request ID 不等于幂等键；拒绝/no-op 不必有安全事件，不能由缺少事件推断原请求未到达。bootstrap 已有审计调用，但不是停用管理员的恢复路径。
- Backend 的变更入口由三列收敛为“动作 → 设计与代码”两列；Contracts README 为全部用户 Schema/example 提供用途映射，Web README 从已有契约到 client、页面和三语顺序接续。
- 契约联调指南新增未接齐链路的判断与只读检查，从总索引、API 利用、Contracts/Scripts README 可直接进入。文档维护要求保留工作副本和真实失败，不自动改契约取得绿色。

### 81.3 文档验证与保留范围

文档的链接/生成物、工具回归和离线浏览与上述应用局部核对分别验证。真实 DB/迁移、bootstrap、账号变更、业务 Web、Compose、模型和外部写入不在本轮操作范围；设计门禁不因此降低。

复用工作区外的文档依赖和 Chromium，不创建 venv；所有浏览检查直接打开 file 页面，拦截 HTTP(S)，不执行文档中的命令。

| 检查 | 结果与范围 |
| --- | --- |
| 文档 build/check | 47 份 Markdown、1 份 ViewSpec；本地链接、锚点、标题层级、示例与生成物一致性通过 |
| 文档工具回归 | 20 项 unittest 通过；三份文档 Python 工具/测试的 Ruff check 与 format --check 通过。新增用户 Schema 到人工索引的覆盖检查 |
| 命令示例 | 修改文档中的 20 个 Bash/Sh 代码块通过 bash -n；未执行其中的部署、bootstrap、数据操作或 smoke |

首次浏览矩阵之后的用户交接检查发现：长函数名与错误码撑宽了两列表，390px 下不能同时读到事实和限制。已把错误码放到可换行的说明列、缩短动作列并细分三类 409；调用图也将代码接线与交付面分组，不把 migration 画成运行调用。未改变 viewer 样式或隐藏任何一列。

修正后先单独重验用户/契约两条交接路径（390/1440px），0 JS 错误、0 HTTP(S) 请求；保存 20 张截图，人工抽看其中 6 张的手机接线、错误、Backend 入口、联调判断和桌面/手机契约映射。

第二轮全书与 42 个选定章节的布局/刷新矩阵通过，但 R01–R13 交接检查发现 R05 多出第四项，破坏统一的“状态、范围、验收”结构。已将接续步骤改为指向设计的短段落，不放宽三项结构检查，也不重写其他 R 项。

最后一轮重新检查全部 47 页的 390/1440px 布局（94 次），以及当前证据、R05、本章入口/验证、用户接线、契约联调共 6 章 × 320/390/768/1440px × 16/24px：48 次章节布局、48 次原生刷新、48 次恢复字号布局通过。搜索、导航、所有既有交接路径和新增契约交接全部通过；0 JS 错误、0 HTTP(S) 请求。文档 unittest 再次 20 项通过。

这些是全书页面、选定章节矩阵和全部交接的组合，不是所有既有章节的全量矩阵，更不是业务 Web 的验收。补录后重新生成并检查浏览版，复查补录入口；不会因此刷新应用全量或部署基线。

本轮更新 21 份 Markdown、两份文档工具/测试及派生 index.html。对照开始时 archive，原 1,182 个章节锚点全部保留，没有删除文件；历史 §1–§80 的 334660 字节前缀保持不变。此次没有目录搬迁，也不移动或改写 SKILL.md/references 等执行资产。遵循 pjm-project-dev 的语言、正本分工和验证边界，保留既有应用改动和失败，不清理其他轮次的进程与产物。
