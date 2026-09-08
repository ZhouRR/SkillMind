# ProjectMind 通用 Skill 兼容、解释与发布规范

> 定位：Skill 的语义契约与生命周期规则。代码接线见[解释与发布实现](skill-interpretation.md)，当前完成度见[计划](../planning/roadmap.md#13-当前执行状态)。本页不把兼容标签、发布状态和执行权限合成一种“可用”状态。

本文定义目录式 Skill 如何成为可配置、可执行、可审计的任务。先看[发布与就绪的判断顺序](#发布与就绪的判断顺序)理解各阶段，再按需要阅读导入、蓝图和[升级回滚](#11-版本回滚与评价)。

## 1. 设计目标

- 宽松导入：外部 Skill 不需要先改写为 ProjectMind 专用目录或携带业务 Schema。
- 语义优先：Interpreter 首先回答“能做什么、需要什么、目标是什么、按什么规则做、产生什么结果”，而不是先拼装固定表单。
- 契约分层：平台只固定通用控制协议；Ticket、代码评审等业务字段由 Skill 语义和运行上下文决定。
- 权限独立：Skill 的 Tool、Shell、网络或写入声明只是需求证据，不能直接授予权限。
- 可见后调整：首次解释直接生成 Preview；用户通过追加式指示重新解释，不覆盖旧版本。
- 发布与可运行性分离：安全的能力可以先发布，再由 Project 完成资源绑定；信息不足时可降级为指导型能力。
- 执行策略自适应：Skill 的步骤默认是指导和验收依据，Agent 可以在硬边界内调整顺序、补充检查和进行多轮会话。
- 来源可追溯：SkillSource、解释输入、模型、系统 Skill、输出、调整和发布版本均不可变或追加式保存。

## 2. 分层架构

| 层 | 职责 | 信任边界 |
| --- | --- | --- |
| `ImportAdapter` | 探测目录结构、读取元数据、收集文件和引用 | 当前为平台内置代码；不提供来源携带 Adapter 的执行入口 |
| `PackageNormalizer` | 生成统一 NormalizedSkillPackage 并保留未知扩展字段 | 平台可信代码 |
| `SkillStaticAnalyzer` | 提取工具、资源、命令、外部地址、敏感项和引用关系 | 不执行来源内容 |
| `SkillInterpreter` | 理解能力、目标、资源、指导、交付物、交互点和效果意图 | 版本化系统 Skill + 模型 |
| `InterpretationValidator` | 验证结构、source trace、危险声明和能力蓝图一致性 | 平台可信代码 |
| `SkillPublisher` | 冻结 SkillVersion、CapabilityBlueprint 和 RuntimeManifest | ADMIN 显式发布 |
| `TaskProjector` | 从能力蓝图投影项目任务和资源要求 | 不写入凭据，不扩大权限 |
| `RuntimeLoader` | 只加载精确 PUBLISHED 版本并创建不可变 Run 快照 | 平台可信代码 |

首个系统级 Interpreter Skill 名为 `projectmind-skill-interpreter`。其版本、prompt checksum、模型参数、输出协议和评价集必须可追溯。业务服务依赖 `SkillInterpreter` 抽象，不依赖某个 SDK 的 message 类型。

兼容包装不意味着拥有通用脚本执行器。脚本可以作为解释来源和 checksum 校验对象；实际自动执行仍只能经过已注册 Provider。

外部 Skill 不能携带或替换 ImportAdapter。新增来源格式由平台增加可信 Adapter；业务语义差异优先由 Interpreter 规则理解。

## 3. 导入兼容层

### 3.1 支持的来源

当前提供本地目录 CLI、内联文件 JSON 与 multipart 多文件上传。Directory/Generic Markdown adapter 读取 SKILL.md、AGENTS.md、metadata、references、scripts 和文本资产。实现入口见 [importer](../../PJM/backend/src/projectmind/skills/importer.py) 与 [导入 API](../../PJM/backend/src/projectmind/api/routes/skills.py)。

ZIP/TAR 解包和远程 Git URL 导入尚无对应公开入口，不列为已支持能力。未来新增时必须先实现路径穿越、symlink/设备文件、压缩体积、解压文件数和固定 revision 校验。

现有导入不执行脚本、不安装依赖、不访问来源中声明的 URL，也不解析 Secret。归档文件作为上传资产被保存，不等于平台会自动解包或执行。

### 3.2 NormalizedSkillPackage

NormalizedSkillPackage 至少包含：

- 来源身份、adapter version、内容 hash 和文件索引。
- 主说明文档、引用文档、脚本元数据和可选例子。
- 原始 frontmatter/metadata 及未知扩展字段。
- 静态分析发现的 Tool、命令、URL、文件访问和潜在敏感项。
- 每个提取项的 source path、section 或 line trace。

原始正文是不可变证据。Normalizer 可以增加统一索引，但不能把未理解内容悄悄丢弃或当成授权。

## 4. Interpreter 的核心输出

### 4.1 CapabilityBlueprint

Interpreter 的主要产物是 `CapabilityBlueprint`，而不是业务 input/output Schema。一个蓝图可包含多个能力和 TaskBlueprint，每项至少表达：

| 维度 | 含义 |
| --- | --- |
| `capability` | Skill 能完成的领域能力；允许出现平台此前未知的业务能力名 |
| `intents` | 用户会在什么情况下调用该能力 |
| `objectives` | 要解决的问题和期望达成的目标 |
| `success_criteria` | 如何判断工作完成或证据不足 |
| `resource_requirements` | 所需 Ticket、代码库、文档、文件或其他资源及读写需求 |
| `guidance` | Skill 中的分析标准、判断规则、建议步骤和禁止事项 |
| `deliverables` | 报告、结构化结论、补丁、变更提案或其他产物 |
| `interaction_points` | 何时需要澄清、选择、Review 或批准 |
| `effect_intents` | 可能更新 Redmine、提交代码等外部效果；只表示意图 |
| `execution_preferences` | 推荐自主程度、可并行项、会话切分和停止条件 |
| `source_traces` | 每项结论对应的原始 Skill 位置 |
| `assumptions/questions` | Interpreter 无法确定但不应伪造的信息 |

`capability` 是领域语义，不等于 Tool capability。`projectmind.development.readiness` 这类新能力无需先加入 Tool catalog；只有真正读取仓库时才要求已注册的 `repository.read/v1`。

### 4.2 ResourceRequirement

资源要求使用抽象类型和能力表达，不写死 Provider：

```json
{
  "key": "target_repository",
  "kind": "repository",
  "required": true,
  "access": "read",
  "capabilities": ["repository.read/v1"],
  "selection_guidance": "优先选择用户指定的代码版本；未指定时询问",
  "accepted_providers": ["git", "svn", "project-files"]
}
```

Project 或 Run 再把它绑定到具体 Integration。`accepted_providers` 是兼容提示，不应强制平台只支持已列举产品；满足 capability 和 scope 的新 Provider 可以参与绑定。

若 Skill 同时需要 Redmine Ticket 与代码，Interpreter 应产生两个独立 requirement。Agent 可以推荐绑定，用户在创建 Run 前确认或修改选择。创建后，交互只能在已经冻结的资源和权限内补充业务信息，不能更换 Integration、增添文档或扩大 scope；需要改变这些边界时创建新 Run。具体冻结时点由[资源快照](resource-snapshots.md)负责。

### 4.3 Guidance 与约束

Interpreter 必须区分：

- `required_rules`：业务正确性或安全性必需，Agent 不得跳过。
- `recommended_steps`：经验流程，Agent 可按上下文重排、合并或补充。
- `quality_criteria`：最终结果必须满足的证据、完整性和表达标准。
- `prohibited_actions`：Skill 自身明确禁止的业务动作；平台硬拒绝仍独立生效。

原始 Skill 说明与规范化 guidance 都随版本冻结。Worker 得到二者的 checksum 和引用，避免 Interpreter 摘要丢失关键判断规则。

### 4.4 TaskBlueprint 与动态参数

TaskBlueprint 描述用户可触发的目标、资源槽位、可选参数、交付物和效果策略。参数定义用于生成通用表单或对话提问，可以动态生成；它不要求平台为每种业务预先维护 Schema。

Task-specific JSON Schema 是可选派生产物：

- 输入结构稳定且适合表单时，可以由受限 TaskContractDraft 确定性编译。
- 输出需要机器消费时，可以附加受限 Schema。
- 开放式分析可只返回通用 OutcomeEnvelope、Markdown/Artifact、Evidence 和 ChangeProposal。
- 缺少业务 Schema、ViewSpec 或 fixture 不能单独成为解释失败或发布 hard gate。

## 5. 平台 contracts 的边界

`contracts/` 只定义跨 Skill 通用、需要多组件稳定协作的协议，例如：

- API、Problem、SSE RunEvent 和认证包络。
- RuntimeManifest、CapabilityBlueprint、AgentTaskBrief 和 OutcomeEnvelope 元协议。
- UserInteraction、ChangeProposal、EffectExecution 和 Evidence 引用。
- Tool capability 的 request/response/error 协议。
- 可选 TaskContractDraft 的受限元模型。

`contracts/` 不保存 JAF Ticket、repository-review findings 等业务 Schema，也不作为 Skill source fixture 仓库。业务实例、示例 Skill 和评价数据分别放在 `skills/examples/` 与测试/评价目录。

这类 contract 类似边界层的运行时校验：它保证各组件交换的数据形状和安全属性，但不替 Agent 预先规定所有业务推理内容。

## 6. 解释请求与响应

### 6.1 冻结输入

一次解释至少冻结：

- SkillSource ID、source hash、文件索引和 adapter version。
- NormalizedSkillPackage 与静态分析报告。
- 平台支持的资源类型和 Tool capability catalog；不包含 Project credential。
- CapabilityBlueprint/RuntimeManifest 目标协议版本。
- Interpreter Skill version、prompt checksum、model 和生成参数。
- 可选父 Interpretation 与用户追加调整。

### 6.2 结构化响应

响应至少包含：

- summary、compatibility level 和 CapabilityBlueprint。
- resource requirements、guidance、deliverables、interaction points 和 effect intents。
- source traces 与确定性 diagnostics。
- 可选 confidence、assumptions、questions 和动态 TaskContractDraft。

模型没有返回完整结构化响应时，本次解释失败或进入可调整 Preview，不能把自由文本偷偷包装成已验证 Manifest。用户追加调整必须生成新的 Interpretation，并保留 parent lineage 和结构化 diff。

默认只接受 SDK 的结构化响应。现有兼容开关 `PROJECTMIND_SKILL_INTERPRETER_ACCEPT_PROMPT_JSON=true` 可显式接收文本中的完整 JSON；它不补造字段，也不绕过 Schema、identity、source hash 与发布门禁。该降级路径应单独记录验收，不能用它宣称 SDK structured-output 已通过。设置说明见[本地开发](../development/local-development.md)。

### 6.3 防提示注入

来源 Skill、references、Ticket、代码和文档都属于不可信内容。Interpreter 与 Worker 必须明确区分：

- 平台系统规则。
- 已发布 Skill 指导。
- 项目配置与用户输入。
- Tool 返回的外部数据。

低信任层不能要求泄露 Secret、改变权限、调用未注册 Tool、修改系统指令或把数据内容提升为平台策略。检测到此类文本时记录 diagnostic，并继续按上层边界处理。

## 7. 兼容级别与运行就绪度

### 发布与就绪的判断顺序

| 已观察到的事实 | 能说明什么，下一步检查什么 |
| --- | --- |
| 导入或 parse 成功 | 来源可读取并已归一化；初始 Draft 可无蓝图，还需 Interpreter 解释 |
| 有 Preview | 可以审查一次解释；不能据此跳过来源、蓝图和发布 gate |
| `gate_passed = true` | DRAFT 没有 hard error；仍需检查 warning 并由 ADMIN 显式接受后发布 |
| 版本为 `PUBLISHED` | 该精确版本已发布；Project 尚须显式启用，不自动替换旧版 |
| Project 已启用 | 允许该项目发现版本；资源和已安装 Provider 决定任务 readiness |
| `RUNNABLE / ACTIONABLE` | 当前配置可达到的能力投影；新 Run 仍验证输入/资源，外部 apply 仍检查具体批准 |

例如同一版本已发布、A 项目已启用且有仓库、B 项目已启用但缺仓库：版本身份相同，两个项目的就绪度可以不同。另一个未启用的项目不应看到该版本的任务。这不是按项目复制三份 Skill，也不是在发布时访问三个项目的 Secret。

### 7.1 兼容级别

- `native`：为原有结构已符合平台协议的资产保留的兼容标签；不是一条绕过 Interpreter 的新导入/发布入口。
- `adapted`：Interpreter 从自然语言和资产中生成可验证蓝图。
- `assisted`：仍有较多假设、未决资源或人工步骤，需要额外审查。

以上是 Schema 中的实际小写值，展示名可本地化。兼容级别不直接决定能否发布；`assisted` 会产生待接受的 warning，不因此免除其它 gate。

当前可信 Adapter 只归一化来源，确定性 Draft builder 不生成业务蓝图。新发布版本的蓝图仍只能由 Interpreter 产生；手工 `native` fixture 只用于离线契约/历史回归，不证明存在“原生直接发布”产品通道，也不能恢复旧业务 seed。实现边界见[蓝图唯一来源](skill-interpretation.md#53-蓝图是唯一来源)。

### 7.2 运行就绪度

“已经解释”由 Interpretation 状态表达，不是 TaskReadinessLevel 的枚举值。当前就绪度仅有以下四项：

- `CONFIGURATION_REQUIRED`：需要用户补充参数或绑定资源。
- `RUNNABLE`：只读/工作区内执行所需资源和 Tool 已就绪。
- `ACTIONABLE`：投影识别到可执行的 apply 能力；不代表具体 Proposal 已批准，执行时仍校验 Integration、scope 和策略。
- `GUIDANCE_ONLY`：当前只能作为说明、对话或人工清单使用。

就绪度按 Project/Task 动态计算。同一 SkillVersion 在一个项目可为 RUNNABLE，在另一个项目可能仍需配置。
这要求 SkillVersion 归 Organization 而非单个 Project（`docs/04` §2）：Project 通过显式启用精确
PUBLISHED 版本来复用同一份资产，就绪度再按各自的资源独立计算。未启用的版本不参与就绪度判定，
它对该 Project 不可发现。

## 8. 校验与发布门禁

### 8.1 必须通过的 hard gate

- 来源 hash、文件索引和解释 identity 一致。
- CapabilityBlueprint 符合通用协议，至少有可理解的目标和依据。
- source trace 指向真实归一化文件。
- Tool requirement 使用合法版本格式；实际调用前必须能映射到平台注册 capability。
- 无明文 credential、路径逃逸、任意宿主命令授权或远程可执行引用。
- effect intent 被标记为 `observe`、`propose` 或 `apply`，且没有把 `apply` 误当作已授权。
- RuntimeManifest checksum、SkillVersion lineage 和发布者身份有效。

### 8.2 不应成为 hard gate 的项目

- 领域 capability 尚未存在于 Tool catalog。
- Skill 未携带 input/output Schema、ViewSpec 或 fixture。
- Project 尚未绑定 Redmine、Git、SVN 或文件 Integration。
- 推荐步骤无法完全确定，或存在可在运行时询问的问题。
- Assisted compatibility 本身。

这些情况降低就绪度或产生 warning，不可仅凭标签拒绝发布。所有 hard error 仍须解决，warning code 须由 ADMIN 显式接受；`gate_passed` 不等于 warning 已接受。运行前缺少的必需资源在创建前补齐，不能靠 Run 内交互扩大冻结范围。

### 8.3 发布流程

```text
SkillSource
  → normalize / static analyze
  → frozen interpretation request
  → CapabilityBlueprint candidate
  → deterministic validation
  → Preview + user adjustment
  → SkillVersion DRAFT
  → ADMIN publish
  → Project 显式启用精确 SkillVersion
  → Project resource binding
  → readiness projection
  → exact Run snapshot
```

系统不因来源变化自动重新解释或切换已发布版本。用户显式重新解释后产生新 DRAFT，比较能力、资源、指导、效果和可选参数差异，再决定是否发布。

## 9. RuntimeManifest v1alpha1

以下是语义内容，不是可直接提交的字段清单。实际顶层字段见 [RuntimeManifest Schema](../../PJM/contracts/runtime-manifest/v1alpha1.schema.json)，`manifest_version` 的精确值为 `projectmind/v1alpha1`。RuntimeManifest 冻结：

- `manifest_version`、Skill/Interpretation identity 和 checksum。
- CapabilityBlueprint。
- TaskBlueprint 列表。
- Tool requirements 与资源要求。
- Skill guidance、质量标准和禁止事项。
- 推荐 ExecutionProfile 与交互点。
- effect intents 和默认 `observe/propose/apply` 策略。
- 可选的动态参数/输出 Schema 及 checksum。
- 解释 source trace 和 assumptions。

Manifest 内容与 checksum 从创建 SkillVersion DRAFT 起固定；发布者、发布时间、状态与已接受 warning 保存在版本记录/门禁报告，不为发布操作重写 Manifest。不要把这些元数据添加成 Manifest 的顶层字段。

资源要求只放在 `capability_blueprint.resource_requirements`，不存在顶层 `data_sources`；Task 与 Tool 投影不得重复定义不一致的资源 key。

RuntimeManifest 不包含 Project Secret、具体 credential 或运行时选中的 Integration。后者在 ResourceBinding 和 Run snapshot 中冻结。

## 10. Skill 组合

当前 SkillComposition（页面中称 Module）保存名称、说明与精确 SkillVersion 集合，ProjectComposition 控制项目展示；它将已有任务归组，不把多个蓝图合成一个新的可执行 Skill。当前 service 没有跨 Skill 业务规则冲突求解器，组合存在也不授予成员版本额外权限。

若后续增加组合级行为提示、默认任务或资源偏好，须先定义契约、版本/快照和以下冲突处理，不将其写成现行字段或自动编排能力：

1. 平台安全与权限规则始终最高。
2. Project 策略限制可用 Integration 和效果范围。
3. 各 Skill 的 required rule 均需满足；冲突时任务不可直接运行并要求用户选择。
4. 组合层可以调整推荐步骤和表达风格，不能删除 Skill 的必需质量标准。

虚拟角色只是 SkillComposition 的呈现方式，不等于系统权限角色。

## 11. 版本、回滚与评价

### 11.1 版本内容与可见性

来源或解释规则变化先产生新的 Interpretation；需要交付时，从其候选创建新的 SkillVersion DRAFT，再审查发布。不是每次模型尝试都自动增加发布版本，也不通过编辑既有 Manifest 完成调整。

| 操作 | 当前边界 |
| --- | --- |
| 发布 / 废弃版本 | `DRAFT → PUBLISHED → DEPRECATED`。重复发布已发布版可读回原记录；废弃版不能重新发布 |
| 为 Project 启用 | 只接受本组织的精确 PUBLISHED 版本；重复启用仍活动的关系返回原记录 |
| 在 Project 停用 | 保留停用记录，关闭该项目的新任务发现/创建；不改写既有 Run 快照，也不等于取消 Run |
| 同版重新启用 | 当前拒绝恢复已经停用的关系；不是清空 disabled_at 的普通开关 |
| 删除废弃版本 | 有独立受限入口，检查 Run/Proposal/Composition 引用；删除不是升级或回滚步骤，不因从列表隐藏就删除审计资产 |

**当前限制**：若 v1 已在项目停用，即便 v1 仍是 PUBLISHED，也不能通过现有启用 API 将它恢复。旧文档笼统的“调整启用关系即可回滚”不成立。只能选择仍可用的精确版本，或把来源重新解释、审查发布为新版本；后者不等于恢复原版本身份。不要删启用记录或直接改数据库来绕过这个限制。

组合也只引用精确版本；改组合不复活已停用/废弃版本。已有 Run 和 Schedule 不跟随新版本，后续 Schedule 的版本失效处理由[调度设计](task-scheduling.md#保存和执行边界)负责。

### 11.2 可审计的重新启用与回滚

这是后续修正设计，当前没有完整 API/审计载体。目标是恢复合法旧版本的项目可见性，同时保留每次停用的事实；既不让“不可变版本”阻止正常配置回滚，也不抹掉停用审计。

1. 重新启用仍是 ADMIN 对“Project + 精确版本”的显式操作，重新检查组织、项目和 PUBLISHED 状态；不允许复活 DEPRECATED 版本或自动采用 latest。
2. 启用/停用/重新启用形成追加式历史，记录操作者、时点、原因与所依据状态。当前可见性可以是投影，但历史不可被覆盖；仅清空现有 disabled_at 不满足要求。
3. 重复请求不追加重复事实；并发启停以受版本约束的状态转换处理，冲突后要求重新读取，不能由晚到响应悄悄覆盖新决定。载体、请求身份和公开字段在实施时与 Schema/migration 一起确定。
4. 重新启用只改变新任务的可发现范围。它不授予资源权限、不重写 Run、Schedule 或组合引用，不自动取消在途执行，也不自动恢复 ERROR Schedule。
5. UI 先显示精确版本差异与影响，再确认启停计划。若需要“一键切换新旧版本”，另行定义原子切换协议；两个独立启停请求不宣称是一个事务。

选择追加历史是为了同时满足日常回滚与审计，不引入可变发布内容。实施需同步 repository/DB、API/契约、Web 启停与冲突提示、历史读取及真实并发回归；接续范围登记在[计划 R06](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)。

### 11.3 生命周期验收与质量评价

| 验收场景 | 应观察到的结果 |
| --- | --- |
| parse 成功但无蓝图 | 可以审查来源，不可绕过解释门禁发布 |
| hard error / 未接受 warning | 前者拒绝发布，后者需显式接受；不把 gate_passed 当作发布状态 |
| 发布但未启用 / 启用但缺资源 | 分别不可发现 / 显示配置不足，不自动创建或扩权 |
| 停用、废弃、删除 | 分别验证作用域和引用限制，既有 Run 快照不变 |
| 重新启用与并发启停（目标） | 原停用历史保留；重复不增记，冲突不覆盖；不得复活废弃版 |
| 升级后回读旧 Run | 原版本、Manifest checksum 与结果不漂移，不用新版解释旧输出 |

Interpreter 质量报告独立评价能力识别、资源前提、目标/交付物、规则保真、Tool 映射、效果识别、source trace 和人工调整量。样例覆盖 JAF、repository review、资源不足、写入意图和恶意指令；Schema exact match 与上述生命周期回归都不能替代模型质量评审。

## 12. 当前迁移边界

动态 TaskContractDraft 与业务 Schema 去预定义化、CapabilityBlueprint、三级 ResourceBinding、
AgentTaskBrief、持续多会话 Run 和首个 controlled effect 已完成本地迁移。旧 JAF/repository-review 业务
Schema、专用 renderer 和活动 seed 不再是新运行规则来源；历史不可变 SkillVersion/Run snapshot 只读
保留。蓝图只由 Interpreter 生成：RuntimeManifest 必须声明蓝图才能发行，平台不从 manifest 反推蓝图。

`ACTIONABLE` 不是“Skill 声明 apply”即成立：Project 必须启用精确 SkillVersion，绑定含注册 write
capability 的 active Integration，并由平台具备相应 Provider 与默认批准/适用预授权路径。已注册写入为 Redmine CAS `issue.update/v1` 与 Git/SVN `repository.write/v1`；后者始终人工批准，direct/branch 规则见[受控写入](repository-effects.md)。真实部署与模型质量验收见[计划 §13](../planning/roadmap.md#13-当前执行状态)。
