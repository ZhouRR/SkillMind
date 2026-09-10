# Skill 解释与发布实现

本页连接[语义契约](skill-contract.md)与代码，负责解释、确定性门禁和发布；运行协议见[Runtime](agent-runtime.md)，当前缺口见[计划 R06](../planning/roadmap.md#r06-skill-生命周期)。

## 目标与主流程

```text
文件 → Normalize / Static Analyze → immutable SkillSource
  → 冻结 identity/catalog → 模型 Blueprint candidate
  → 确定性验证 → Preview / 追加调整
  → SkillVersion DRAFT → ADMIN 发布
  → Project 显式启用精确版 → TaskCatalog/readiness
```

当前导入、结构解释、调整/diff、发布与项目启用已有实现。parse 不调用模型，初始 Draft 可无蓝图，不能直接发布；ZIP/TAR 自动解包、远程 Git、TaskFlow 和生成模块执行不在这条已接链路中。

## 信任边界

来源/附件/外部内容是数据，不覆盖平台规则。Tool/Shell 声明不授权，静态发现 credential 在解释前阻断；Interpreter 不读取 Project Secret/Integration、不发布、不建 Run。业务 required 不因缺工具而弱化，可重表达实现手段。

## 代码责任

| 入口 | 责任 |
| --- | --- |
| [importer](../../PJM/backend/src/projectmind/skills/importer.py) | 归一化、文件/引用、静态 Draft |
| [interpreter](../../PJM/backend/src/projectmind/skills/interpreter.py) / [model_interpreter](../../PJM/backend/src/projectmind/skills/model_interpreter.py) | 静态发现、identity、报告与结构化模型适配 |
| [interpreter_execution](../../PJM/backend/src/projectmind/skills/interpreter_execution.py) | 冻结执行、复用和恢复 |
| [capability_blueprint](../../PJM/backend/src/projectmind/skills/capability_blueprint.py) / [task_contract](../../PJM/backend/src/projectmind/skills/task_contract.py) | 蓝图校验/投影，受限动态契约编译 |
| [design_validation](../../PJM/backend/src/projectmind/skills/design_validation.py) | 预览与发布共用的原始身份、全部 Task 和来源校验，不依赖 Project 或补写声明 |
| [manifest_gate](../../PJM/backend/src/projectmind/skills/manifest_gate.py) | 复用来源校验，检查 Tool/权限、生成契约、可选资产与 warning |
| [service](../../PJM/backend/src/projectmind/skills/service.py) / [repository](../../PJM/backend/src/projectmind/skills/repository.py) | 解释协调、版本/可见性、引用门禁和 TaskCatalog |

系统 [Interpreter Skill](../../PJM/skills/projectmind-skill-interpreter/SKILL.md)是版本化执行资产，改正文/references 必须同步 package hash、prompt identity、版本/example/回归，不当普通文档搬迁。

### 从候选到项目任务的接线

PREVIEW_READY → create_version_draft 冻结 Manifest/checksum/findings → publish_skill_version 重验原始设计并确认 warning → 显式项目启用 → 按真实资源计算 readiness。

初次冻结仅将 Manifest/Blueprint 中相同的候选 Interpretation UUID 绑定到实际行；不规范化候选、补齐蓝图身份或改写 source/hash/interpreter version。DRAFT 可保存错误供审查；parse 无蓝图仍不可发布。

首次发布锁定精确版本，复核组织、Source/Interpretation/Manifest 关系，再以原索引、文本快照和冻结 Manifest 运行当前门禁。旧报告缺失、损坏或含 hard error 均拒绝；保存时和当前的 warning 都须在本次请求明确接受。仅更新版本/gate metadata，不重新调用模型、修补 Manifest 或改变 checksum。

PUBLISHED 重复发布读取原记录，不补验后撤销历史或改写发布者/时间，但仍须通过下述当前 ADMIN 授权。来源/解释改变需追加解释和新 DRAFT。TaskCatalog 不证明外部可达；创建/Provider 重验。来源重建、模型恢复与发布复用是不同幂等边界。

Manifest checksum 证明冻结内容自身一致；模型 Interpretation 的既有 checksum 是执行复用键，不是候选正文 hash。候选到冻结内容的独立持久绑定仍需定义候选 checksum 和冻结转换版本，不能用今天的 normalizer 推断历史等值或回填旧资产。

### 版本管理的授权事务

草稿、发布、废弃、删除和项目启停都必填原请求的会话/CSRF，组织及首次发布者/启用者从业务事务锁定的 User 取得。入口 ADMIN 认证不代替提交资格。

| 操作 | 额外边界 |
| --- | --- |
| 组织草稿/发布/废弃/删除 | 不依赖 Project 成员或归档状态 |
| 项目启用/停用 | 先锁本组织精确 Project 并检查 ACTIVE；ADMIN 无需成员关系 |

```text
入口认证结束
  → Organization UPDATE
  → User SHARE
  → 原 AuthSession UPDATE
  → 当前 ADMIN / 原凭据 / 期限
  → 项目启停时 Project SHARE
  → 既有 SkillVersion UPDATE
  → 启停关系 UPDATE / 业务门禁
  → 写入前复核
  → 最终 flush / 再复核
  → commit
```

复用[账户的取锁顺序与凭据校验](user-lifecycle.md#事务与并发)；User SHARE 阻止角色/状态变化，同时兼容业务外键 KEY SHARE。草稿父行 flush、来源/引用读取、版本及启停关系等待后均取新时间；idle/absolute 到期即拒绝。领域 404/409 返回前也复核资格，失效先返回共享 401，CSRF 或当前非 ADMIN 返回相应 403，不泄露原凭据或来源正文。项目不存在/越权统一 404，只有已授权的归档项目返回 409。

重复发布/废弃/启用/停用仍经当前授权，保留原发布者、启用者及已有时间；删除成功后再次删除为 404，不补造删除回执。同一或另一位当前有效的本组织 ADMIN 可操作原版，新会话不恢复旧会话。判定点是持锁的最终检查，不承诺物理 commit 瞬间未过期。SQL 失败后只用锁内复制的资格值与新时间分类，不触发过期 ORM 的隐式查询；提交响应未知和取消不转为成功、不自动重试或补偿。

删除在版本锁内检查全部执行/配置引用，拒绝发生在关系和 Manifest 删除之前，范围见[版本边界](skill-contract.md#版本内容与可见性)。导入与解释写入的业务资格仍须接齐；离线 SQL/回滚替身不证明 PostgreSQL 的实际锁竞争、回滚或旧写入者兼容。

## CapabilityBlueprint

字段以 [Blueprint Schema](../../PJM/contracts/capability-blueprint/v1.schema.json)为准；标题/说明保留源语言，协议 enum 大小写按 Schema，不笼统要求全小写。

identity/hash/trace/结构错误拒绝；新领域 capability 允许，实际 Tool 必须注册；缺业务 Schema/ViewSpec/fixture 不能单独硬拒绝，assisted_review_required 须接受。自然语言完整性不能靠 trace/Schema 证明。

### 蓝图是唯一来源

蓝图只由 Interpreter 产生，parse 返回 null 时发布 gate 报 capability_blueprint_missing。Manifest Schema 保留 Draft 形态不等于发布许可；Worker 只读冻结蓝图，不从旧 workflows/data_sources 反推。手工 native fixture 不是绕过路径。

### 过程重表达

| 来源表达 | 平台路径与限制 |
| --- | --- |
| curl 读取业务记录、git/svn 读代码 | issue.read/v1、repository.read/v1；地址/凭据/revision 归绑定 |
| list/grep/log | 资源物化、files.txt、workspace.search/read、有界 history.txt；无自由 shell |
| xlsx/xlsm/docx | 平台文本化后读位置；不宣称支持 PDF/任意格式 |
| 报告/补丁 | 需下载的交付使用显式声明的 workspace.write/v2 写 output/ 并取得已提交的 Artifact 引用；v1 或 workspace/ 中间文件不证明附件发布 |
| 修改外部内容 | change.propose/v1 → 批准/效果链，不给 Agent 直调 write |
| 无等价能力 | 说明限制、人工处理或 GUIDANCE_ONLY，不虚构 Tool/弱化 required |

确定性守卫只检查显式能力引用与结构，不证明语义等价或运行参数正确；Gateway 校验参数，Preview/质量评审检查保真。新增 [Flow](task-flow.md)字段须先冻结 Schema，不随意扩 Blueprint/Brief。

## ResourceBinding 与 readiness

资源唯一来自 capability_blueprint.resource_requirements，无顶层 data_sources；Tool 投影引用相同 key，write 不折入 Agent allowed_capabilities。

空 allowlist 不授权。issue_ids/field_keys 允许显式 ["*"]，子 scope 不能枚举扩 wildcard；repository path 不用该 wildcard，LOW 预授权必须精确 scope。

[resource_binding](../../PJM/backend/src/projectmind/skills/resource_binding.py)先查精确项目启用，再算任务 readiness 与 requirement AVAILABLE/UNAVAILABLE/UNSUPPORTED；依据真实已安装 Provider。候选可用不是选择或实际连通，选择创建前冻结，运行中 CHOICE 不换绑。旧注释若相反按[冻结规则](resource-snapshots.md)修正。

新建 Run、保存调度及恢复 ACTIVE、[组合保存](skill-contract.md#组合保存的授权事务)在写入事务经共享 `require_current_task_binding` 复核精确 PUBLISHED 版和未停用关系。先锁 SkillVersion SHARE，再锁 ProjectSkillVersion SHARE；兼容 occurrence 外键 KEY SHARE，同时与启停/废弃写锁协调。它只固定当前可用性，不重解 Manifest/资源，也不用于[原 Run 确认](run-creation.md#目标创建流程)。

同版停用后不可恢复，发布不可复活 DEPRECATED；目标审计恢复由[生命周期](skill-contract.md#可审计的重新启用与回滚)维护。

## Brief 与运行交接

[AgentTaskBrief](../../PJM/contracts/agent-task-brief/v1.schema.json)按 Segment 保存 brief_json/checksum，完整带 required/quality、目标、资源、权限、交付和限制。物化路径只来自物化器，日志仅 identity/checksum/profile，不记录业务 Brief 正文。

GUIDED/SUPERVISED/DELEGATED 默认 SUPERVISED，不能覆盖硬拒绝。新 Run 显式 Segment/Brief，旧 Run 只读隐式投影；续行/Attempt、Session、workspace 和效果的唯一规则分别在 Runtime、[资源](resource-snapshots.md)、[受控写入](repository-effects.md)，不再维护副本。

结果用通用 Outcome，可选 task-specific structured_data；缺业务 Schema 不等于 structured_output_missing，结构不保证业务正确。

## 验证与接续

- Parser：不执行来源，路径/大小/引用/hash；Interpreter：冻结 identity、结构输出、失败恢复、追加调整/diff。
- 版本管理：蓝图/来源与 warning 门禁；六操作的原会话、锁等待/flush 过期、回滚、取消与提交未知；删除全部引用保护和重复操作保留原值。版本停用与新建/保存竞争不改变旧 Run；目标重新启用另验审计。
- 泛化：资料分析、代码审查、开放文档、缺资源、恶意指令走同一路径，不添平台业务分支。
- 安全：敏感来源阻断、scope/跨 Project 拒绝、能力不扩权；runtime/Provider 再验证。
- 模型质量：固定输入重复采样，独立人工判断规则保真/证据/调整量；prompt JSON 与 SDK structured-output 分别举证。

回归入口：[skills tests](../../PJM/backend/tests/skills/)、[contracts tests](../../PJM/backend/tests/contracts/)。mock repository 不证明真实事务竞争；模型、部署、外部系统另按授权环境验收。

改解释结构/系统 Skill 时同步契约、版本/checksum、Backend validator/projector、Web 预览、example/回归及质量评审。不增加任意脚本、自动扩权、自动升级发布版或生成后端服务。
