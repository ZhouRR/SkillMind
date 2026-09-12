# Skill 解释与发布实现

本页连接[语义契约](skill-contract.md)与实现，负责解释、确定性门禁和发布；运行见[Runtime](agent-runtime.md)，缺口见[计划 R06](../planning/roadmap.md#开发任务)。

## 目标与主流程

```text
文件 → Normalize/Static Analyze → immutable SkillSource
  → 冻结 identity/catalog → 模型 Blueprint candidate
  → 确定性验证 → Preview/追加调整 → SkillVersion DRAFT
  → ADMIN 发布 → Project 启用精确版 → TaskCatalog/readiness
```

parse 不调用模型，初始 Draft 可无蓝图、不可直接发布。ZIP/TAR、远程 Git、TaskFlow/生成模块执行不属于这条已接链路。

## 信任边界

来源/附件/外部内容不覆盖平台规则，Tool/Shell 声明不授权，credential 静态发现后在模型前阻断。Interpreter 不读 Project Secret/Integration、不发布或建 Run；可重表达手段，不因缺工具弱化 required。

## 代码责任

| 入口 | 责任 |
| --- | --- |
| [importer](../../SKM/backend/src/skillmind/skills/importer.py) | 归一化、文件/引用、静态 Draft |
| [interpreter](../../SKM/backend/src/skillmind/skills/interpreter.py) / [model_interpreter](../../SKM/backend/src/skillmind/skills/model_interpreter.py) | 静态发现、identity/报告、结构化模型适配 |
| [interpreter_execution](../../SKM/backend/src/skillmind/skills/interpreter_execution.py) | 冻结执行、复用/恢复 |
| [capability_blueprint](../../SKM/backend/src/skillmind/skills/capability_blueprint.py) / [task_contract](../../SKM/backend/src/skillmind/skills/task_contract.py) | 蓝图校验/投影、动态契约编译 |
| [design_validation](../../SKM/backend/src/skillmind/skills/design_validation.py) | 预览/发布共用原 identity、全部 Task/来源校验，无 Project 或补写声明 |
| [manifest_gate](../../SKM/backend/src/skillmind/skills/manifest_gate.py) | 复用来源校验，验 Tool/权限、契约、可选资产/warning |
| [service](../../SKM/backend/src/skillmind/skills/service.py) / [repository](../../SKM/backend/src/skillmind/skills/repository.py) | 协调、版本/可见性、引用门禁、TaskCatalog |

系统 [Interpreter Skill](../../SKM/skills/skillmind-skill-interpreter/SKILL.md)是执行资产；改正文/references 须同步 package hash、prompt identity、版本/example/回归，不当普通文档搬迁。

### 导入保存与上传授权

内联/上传必填原 UserAccess，组织/导入者从事务锁定的当前 ADMIN 取得，不另传 UUID。解析无模型/DB 锁；首次 await 前冻结原会话和文件。保存采用 Organization → User SHARE → 原 AuthSession UPDATE，无 Project 门禁；查询、父行 flush、写前/最终 flush 后取新时间复核，Source/静态 Interpretation 原子保存。

| 阶段 | 边界 |
| --- | --- |
| 接收 | Origin/CSRF/ADMIN 后才读 multipart；仅重复 files 文件字段，filename 保留相对路径，不截 basename |
| 限额 | 共用 HTTP parser：100 文件、单份 1,000,000 bytes、总 5,000,000；正文总 + 128 KiB，header 总 128 KiB、单 part 16 KiB，实际计数不信 Content-Length |
| TX A | 原 ADMIN 下查本组织精确 source hash；已有来源复用静态预览，保留 URI/导入者/时间，不 PUT 或修旧 blob |
| 锁外 PUT | 新来源先验全部 key；每份 PUT 前后短事务重验原资格，核对返回 key/size/MIME/hash，不跨 I/O 持锁 |
| TX B | 所有 PUT 返回后重验原 ADMIN，再保存 Source/Interpretation；撤销/到期/降级不发布元数据 |

超限为 skill_upload_too_large 413，结构/不完整边界为 invalid_skill_upload 422，静态解析拒绝仍 400。不建 spool/写线程，取消/断连直接传播。原资格失效 401，CSRF/非 ADMIN 403，存储故障/回执不符静态 skill_storage_unavailable 503；Web 只认 201，不自动重发未知。

这不是跨存储原子提交：取消不停止线程/远端 PUT，最终拒绝可留字节，不补偿删除共享内容 key。hash 非原请求回执，并发首次仍可能重复 PUT。持久意图/确认、SkillSource 归属、旧 blob 核验/精确清理尚缺，不套用文档协议或重写原 hash。

### 异步解释的交接要求

解释/调整通过持久原请求交接；API 受理与 dispatch Outbox 同事务，Worker 从数据库取得原授权与冻结输入。边界如下：

1. 原请求 ID 与内容 execution key 分开；短事务绑定原 actor/会话引用、冻结输入/Interpreter/model 身份及 dispatch Outbox。队列只传持久 ID，不传 cookie/CSRF。同组织同 execution key 绑定一个原请求；复用/确认读取该请求，显式重新生成才使用新 nonce，不能换请求 ID 重启未知执行。
2. Worker 复用原会话资格校验，锁外模型调用前和结果提交时复核；每次初次/修复调用分别取得原 Worker 一次启动许可。认领取得的随机 owner 只在原 Worker 内存持有，DB 仅存 hash；重投不返回旧 owner，修复须已观察初次调用返回且原请求仍为 RUNNING。return 记录不等于进程停止证明，UNKNOWN 只允许原 owner 核对迟到结果，不再授予模型调用。
3. 原请求只读确认、撤权审计、旧队列拒绝、迁移/恢复一起实现，不补造旧作者/执行事实。
4. SSE 先按持久身份验组织归属，再订阅并复查终态/长连接资格。不能仅把组织写入 key 或凭通知判断终态；超时/断连不证明持久 FAILED 或模型未运行，页面不能据此自动换身份生成。

内部台账入口为 `skills/request_service.py` / `request_repository.py`，原会话资格复用 `auth/sessions.py`；migration `0044_interpretation_requests` 只建新请求/调用表，不补造历史事实，存在任何请求时拒绝删表降级。候选与请求终态在同一授权事务提交；最终 flush 后失效须整体回滚候选，再保存撤权状态。

`SkillService.accept_interpretation_request` / `execute_interpretation_request` 已连接冻结输入核对、逐次许可、复用与结果提交；`ModelSkillInterpreter` 在 prompt 准备/异步预览之后、实际 completion 之前调用 `RequestCallControl`。许可或 return 提交结果未知时，同一 controller 不再申请许可。已确认无任何调用许可的准备失败可记 FAILED；存在许可则保留 UNKNOWN，不补造 return 或停止。控制路径须同时装配调用、成果保存和复用门禁。生产 API/Worker 使用该路径；旧 actor/参数队列任务明确拒绝，API 不再直接入队。过期 RUNNING 经原资格和锁内状态复核后转 UNKNOWN 或 REVOKED；恢复不创建调用许可或新 Outbox。

受理入口是 `POST /skill-sources/{id}/interpretation-requests` 与 `POST /skill-interpretations/{id}/adjustment-requests`，body 必填非零 `request_id`。同内容的新 ID 返回原请求只读状态，不接管原会话。`GET /skill-interpretation-requests/{request_id}` 只返回状态、来源、execution key、结果指针及错误码；冻结输入、actor/session 引用与 owner 不公开。SSE 使用该请求的 `/events`，每次等待后重验当前会话；终态从 DB 读取，Pub/Sub 只提供进度。

页面在 POST 前于 tab 保存原 UUID；断连、超时、404 或不可读回执保留未知状态，人工确认和刷新只做原 GET。停止查看不取消模型；已确认终态才清理回执，结果再核来源/执行键/状态。接口路径与旧版分离，版本切换和存量队列见[部署兼容](../operations/deployment.md#迁移与回退审查)。

### 从候选到项目任务的接线

PREVIEW_READY → create_version_draft 冻结 Manifest/checksum/findings → publish_skill_version 重验设计/确认 warning → 显式启用 → 真实资源 readiness。

首次冻结只把 Manifest/Blueprint 相同候选 Interpretation UUID 绑定实际行，不规范化候选、补蓝图身份或改 source/hash/interpreter version。DRAFT 可保存错误供审查，parse 无蓝图仍不可发布。

首次发布锁精确版本，验组织及 Source/Interpretation/Manifest 关系，再用原索引、文本、冻结 Manifest 跑当前门禁。旧报告缺损/hard error 拒绝；保存时与当前 warning 都须本次明确接受。只改版本/gate metadata，不调用模型、修 Manifest/checksum。

PUBLISHED 重复发布仍验当前 ADMIN，但返回原记录，不追溯撤销/改 actor/时间。来源/解释变化须追加解释和 DRAFT；TaskCatalog 不证明外部可达。来源重建、模型恢复、发布复用是独立边界。

Manifest checksum 证明自身内容一致，Interpretation 既有 checksum 是执行复用键而非候选正文 hash。候选持久绑定尚须候选 checksum/冻结转换版，不用当前 normalizer 推断历史等值或回填。

### 版本管理的授权事务

草稿、发布、废弃、删除、项目启停必填原会话/CSRF；组织、首次发布者/启用者从锁定 User 取得。组织资产不受 Project 成员/归档限制；项目启停须同组织精确 ACTIVE Project，ADMIN 无需 membership。

```text
入口认证结束 → Organization UPDATE → User SHARE
  → 原 AuthSession UPDATE / 当前 ADMIN、凭据、期限
  → 项目启停时 Project SHARE → 既有 SkillVersion UPDATE
  → 启停关系 UPDATE / 业务门禁 → 写前复核
  → 最终 flush / 复核 → commit
```

复用[账户锁序/凭据校验](user-lifecycle.md#事务与并发)；User SHARE 阻止角色/状态修改且兼容 FK KEY SHARE。父行 flush、来源/引用读取、版本/关系等锁后取新时间验 idle/absolute；领域 404/409 前也复核，失效优先共享 401，CSRF/非 ADMIN 403。Project 越权/不存在统一 404，授权归档 409，不泄漏原凭据/来源。

重复发布/废弃/启停保留原 actor/时间仍重验资格，删后再删 404，不补删除回执。同组织其他有效 ADMIN 可操作原版，新会话不恢复旧会话。最终持锁判定不承诺物理 commit 瞬间未过期；SQL 失败仅以锁内副本和新时间分类，不隐式读失效 ORM。取消/提交未知不转成功、不重试/补偿。

删除在版本锁内验[全部引用](skill-contract.md#版本内容与可见性)，拒绝先于关系/Manifest 删除；真实锁、回滚及旧 writer 兼容另验。

## CapabilityBlueprint

以 [Schema](../../SKM/contracts/capability-blueprint/v1.schema.json)为准；标题/说明保留源语言，enum 大小写按契约。identity/hash/trace/结构错误拒绝，新业务 capability 允许，真实 Tool 须注册；缺业务 Schema/ViewSpec/fixture 不单独硬拒绝，assisted_review_required 须接受。结构/trace 不证明自然语言完整性。

### 过程重表达

| 来源表达 | 平台路径与限制 |
| --- | --- |
| curl 业务读取、git/svn 读代码 | issue.read/v1、repository.read/v1，地址/凭据/revision 来自绑定 |
| list/grep/log | 物化、files.txt、workspace.search/read、有界 history.txt，无自由 shell |
| xlsx/xlsm/docx | 平台文本化后定位读取，不宣称支持 PDF/任意格式 |
| 报告/补丁 | 显式 workspace.write/v2 写 output/ 并取得已提交 Artifact；v1/workspace 中间文件不是已发布附件 |
| 外部修改 | change.propose/v1 → 批准/效果链，不给 Agent 直调 write |
| 无等价能力 | 限制、人工处理或 GUIDANCE_ONLY，不虚构 Tool/弱化 required |

确定性守卫只验显式能力/结构，不证明语义等价或参数正确；Gateway 验参数，Preview/质量评审验保真。[Flow](task-flow.md)新字段先冻结 Schema。

## ResourceBinding 与 readiness

资源仅来自 capability_blueprint.resource_requirements，无顶层 data_sources；Tool 投影用相同 key，write 不进入 Agent allowed_capabilities。

空 allowlist 不授权；issue_ids/field_keys 可显式 ["*"]，子 scope 不枚举扩 wildcard；repository path 不用该 wildcard，LOW 预授权须精确 scope。

[resource_binding](../../SKM/backend/src/skillmind/skills/resource_binding.py)先验精确项目启用，再按已安装 Provider 算任务及 requirement AVAILABLE/UNAVAILABLE/UNSUPPORTED。候选不是选定/实际连通，创建前冻结，运行 CHOICE 不换绑，见[资源规则](resource-snapshots.md)。

业务资源必须绑定真实 Integration 或显式选择的 Project 文档；裸 Provider 名称不代表来源。缺少绑定或客户端时拒绝创建/执行，不回退到模拟数据。合成 Provider 与 Skill 仅保留在 Backend tests，不随镜像部署。

新 Run、调度保存/恢复 ACTIVE、[组合保存](skill-contract.md#组合保存的授权事务)在事务共用 require_current_task_binding，锁 SkillVersion SHARE → ProjectSkillVersion SHARE，固定精确 PUBLISHED/未停用关系；兼容 occurrence FK 并协调启停/废弃。它不重解 Manifest/资源，也不用于[原 Run 确认](run-creation.md#目标创建流程)。

同版停用不可恢复、DEPRECATED 不复活；目标审计恢复见[生命周期](skill-contract.md#可审计的重新启用与回滚)。

## Brief 与运行交接

[AgentTaskBrief](../../SKM/contracts/agent-task-brief/v1.schema.json)按 Segment 存 brief_json/checksum，完整携带 required/quality、目标、资源、权限、交付/限制。物化路径仅来自物化器，日志只记 identity/checksum/profile，不记 Brief 正文。

GUIDED/SUPERVISED/DELEGATED 默认 SUPERVISED，不能覆盖硬拒绝。新 Run 显式 Segment/Brief，旧 Run 只读隐式投影；续行/Attempt/Session、workspace/效果分别遵循 Runtime、[资源](resource-snapshots.md)、[受控写入](repository-effects.md)，不另存规则副本。

结果用通用 Outcome、可选 structured_data；缺业务 Schema 不等于 structured_output_missing，结构不证明业务正确。

## 验证与接续

- Parser/导入：不执行来源，路径/限额/hash，未授权不收正文，原会话/等锁/flush、旧来源不重写、PUT 前后撤权/错误/取消/未知。
- 解释/发布：冻结身份、结构输出、追加调整、来源/warning 门禁、六操作授权/回滚/引用保护；模型恢复、候选绑定、目标重新启用分别验证。
- 泛化/安全：分析、审查、开放文档、缺资源、恶意指令走同链路，敏感阻断、scope/跨项目拒绝、不扩权。
- 模型质量：固定输入重复采样，独立人工评审保真/证据/调整量；prompt JSON 与 structured-output 分别举证。

按改动选择 [skills](../../SKM/backend/tests/skills/)、[contracts](../../SKM/backend/tests/contracts/)与受影响消费者；真实事务、存储恢复、模型质量和部署分别举证，不为错误文案修改调用模型。系统 Interpreter 的版本/hash 按上文同步，公开结构变化按[契约流程](../development/contract-workflow.md)处理；不引入任意脚本、自动扩权/升级或生成后端服务。
