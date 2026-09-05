# ProjectMind 通用 Skill 兼容、解释与发布规范

本文定义 ProjectMind 如何兼容 Codex、Claude Code 及其他目录式 Skill，如何把自然语言能力转化为平台可配置、可执行和可审计的 CapabilityBlueprint，以及如何验证、发布、绑定资源和升级 SkillVersion。

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

若 Skill 同时需要 Redmine Ticket 与代码，Interpreter 应产生两个独立 requirement。Agent 可以依据 Skill 规则推荐绑定，但用户可以在运行前或交互点覆盖选择。

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

### 6.3 防提示注入

来源 Skill、references、Ticket、代码和文档都属于不可信内容。Interpreter 与 Worker 必须明确区分：

- 平台系统规则。
- 已发布 Skill 指导。
- 项目配置与用户输入。
- Tool 返回的外部数据。

低信任层不能要求泄露 Secret、改变权限、调用未注册 Tool、修改系统指令或把数据内容提升为平台策略。检测到此类文本时记录 diagnostic，并继续按上层边界处理。

## 7. 兼容级别与运行就绪度

### 7.1 兼容级别

- `Native`：确定性 Adapter 可完整提取能力蓝图，不需要模型补全核心语义。
- `Adapted`：Interpreter 从自然语言和资产中生成可验证蓝图。
- `Assisted`：能够提供指导或对话能力，但仍有较多假设、未决资源或人工步骤。

兼容级别说明包装方式，不直接决定能否发布。

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

这些情况降低就绪度或产生 warning，并在 Workspace 中要求配置/交互；只有无法形成安全、可理解的能力蓝图时才拒绝发布。

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

以下是语义内容，实际顶层字段见 [RuntimeManifest Schema](../../PJM/contracts/runtime-manifest/v1alpha1.schema.json)。当前版本为 `projectmind.runtime/v1alpha1`，不使用未发布的 vNext 字段名。RuntimeManifest 冻结：

- `manifest_version`、Skill/Interpretation identity 和 checksum。
- CapabilityBlueprint。
- TaskBlueprint 列表。
- Tool requirements 与资源要求。
- Skill guidance、质量标准和禁止事项。
- 推荐 ExecutionProfile 与交互点。
- effect intents 和默认 `observe/propose/apply` 策略。
- 可选的动态参数/输出 Schema 及 checksum。
- 解释 source trace、assumptions 和 accepted warnings。

资源要求只放在 `capability_blueprint.resource_requirements`，不存在顶层 `data_sources`；Task 与 Tool 投影不得重复定义不一致的资源 key。

RuntimeManifest 不包含 Project Secret、具体 credential 或运行时选中的 Integration。后者在 ResourceBinding 和 Run snapshot 中冻结。

## 10. Skill 组合

SkillComposition 可以组合多个 CapabilityBlueprint，并配置名称、展示形态、行为提示、默认任务和资源偏好。组合冲突按以下顺序处理：

1. 平台安全与权限规则始终最高。
2. Project 策略限制可用 Integration 和效果范围。
3. 各 Skill 的 required rule 均需满足；冲突时任务不可直接运行并要求用户选择。
4. 组合层可以调整推荐步骤和表达风格，不能删除 Skill 的必需质量标准。

虚拟角色只是 SkillComposition 的呈现方式，不等于系统权限角色。

## 11. 版本、回滚与评价

- 已发布 SkillVersion、CapabilityBlueprint 和 RuntimeManifest 不可修改。
- 来源、Interpreter、模型、用户调整或规则变化都生成新版本。
- 既有 Run 始终引用精确版本，不自动跟随 latest。
- 回滚通过显式调整 Project 启用/组合引用精确版本完成，不切换到隐含 latest，不改写历史 Run。
- Interpreter 质量报告至少评价：能力识别、资源前提、目标/交付物、规则保真、Tool 映射、效果识别、source trace 和人工调整量。
- 固定样例应覆盖 JAF Ticket 分析、repository review、资源不足、带写入意图和恶意指令等情况；不依赖业务 Schema exact match 作为唯一准确度。

## 12. 当前迁移边界

动态 TaskContractDraft 与业务 Schema 去预定义化、CapabilityBlueprint、三级 ResourceBinding、
AgentTaskBrief、持续多会话 Run 和首个 controlled effect 已完成本地迁移。旧 JAF/repository-review 业务
Schema、专用 renderer 和活动 seed 不再是新运行规则来源；历史不可变 SkillVersion/Run snapshot 只读
保留。蓝图只由 Interpreter 生成：RuntimeManifest 必须声明蓝图才能发行，平台不从 manifest 反推蓝图。

`ACTIONABLE` 不是“Skill 声明 apply”即成立：Project 必须启用精确 SkillVersion，绑定含注册 write
capability 的 active Integration，并由平台具备相应 Provider 与默认批准/适用预授权路径。已注册写入为 Redmine CAS `issue.update/v1` 与 Git/SVN `repository.write/v1`；后者始终人工批准，direct/branch 规则见[受控写入](repository-effects.md)。真实部署与模型质量验收见[计划 §13](../planning/roadmap.md#13-当前执行状态)。
