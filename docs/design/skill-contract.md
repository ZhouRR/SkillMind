# Skill 兼容、解释与发布

本页是目录式 Skill 的语义契约与生命周期正本；代码调用见[解释实现](skill-interpretation.md)，进度见[计划 R06](../planning/roadmap.md#r06-skill-生命周期)。

## 设计目标

外部 Skill 无需先改为平台专用目录或业务 Schema。Interpreter 理解目标、资源、规则、交付物；业务字段可动态派生，平台只固定跨组件控制协议。

Tool/Shell/network/write 声明只是需求，不能授予权限。必需规则与禁止事项不能为了可运行而弱化；推荐步骤允许在冻结权限内调整。来源、解释与调整保留 trace，发布版本不可原地改写。

## 分层架构

```text
可信 ImportAdapter / Normalizer → 不执行来源的静态分析
  → 版本化 Interpreter 系统 Skill + 模型
  → 确定性 Validator → Preview / 追加调整
  → ADMIN Publisher → Project 启用 → TaskProjector / RuntimeLoader
```

业务层依赖 SkillInterpreter 抽象，不依赖 SDK message。来源不能携带或替换 Adapter；脚本只作为来源与 checksum 对象，自动执行仅可走已注册 Provider。

## 导入兼容层

当前支持本地目录 CLI、内联 JSON、多文件 multipart；Directory/Generic Markdown 读取主说明、metadata、references、scripts 和文本资产，见 [importer](../../PJM/backend/src/projectmind/skills/importer.py)。

不执行脚本、安装依赖、访问来源 URL 或解析 Secret。ZIP/TAR 自动解包与远程 Git URL 尚未提供；新增前必须定义路径穿越/symlink/设备文件、体积/数量与固定 revision 防护。

NormalizedSkillPackage 保存来源身份、adapter version、content hash、文件索引、原文/未知 metadata、静态发现和 source trace。统一索引不能丢弃未理解原文或提升其权限。

## Interpreter 的核心输出

### CapabilityBlueprint

主要产物是 [CapabilityBlueprint](../../PJM/contracts/capability-blueprint/v1.schema.json)，不是业务表单 Schema：

| 内容 | 含义 |
| --- | --- |
| capability/intents/objectives | 能做什么、何时调用、完成什么目标 |
| resource_requirements | 抽象资源、必需性、访问与绑定条件 |
| guidance/success_criteria | 必需规则、建议步骤、质量依据与禁止事项 |
| deliverables/interaction_points/effect_intents | 交付、澄清/审查/批准点与可能效果 |
| source_traces/assumptions/questions | 来源依据和不应伪造的未知 |

新领域 capability 不必预先注册；只有真正 Tool 调用使用 repository.read/v1 等注册版本 ID。

### ResourceRequirement

资源以 kind、required、access、versioned capabilities 和 selection_guidance 表达，例如“必须提供可读 repository，优先用户指定 revision，否则询问”。accepted_providers 是兼容提示，不替代实际 capability/scope 校验。

多个资源独立 requirement；Project/Run 在创建前选定绑定。运行交互只能补充冻结 scope 内信息，不换 Integration/文档或扩权；变化需新 Run，见[资源快照](resource-snapshots.md)。

### Guidance 与约束

required_rules 必须遵守；recommended_steps 可重排；quality_criteria 约束最终证据/完整性；prohibited_actions 与平台硬拒绝独立生效。原文和规范化 guidance 均冻结，Worker 得到 checksum/引用，不能只传丢规则的摘要。

### TaskBlueprint 与动态参数

TaskBlueprint 投影目标、资源槽位、参数、交付与效果策略。稳定输入/机器消费输出可从受限 TaskContractDraft 编译 Schema；开放分析可仅用 OutcomeEnvelope、Markdown/Artifact、Evidence 和 Proposal。缺业务 Schema、ViewSpec 或 fixture 不能单独阻止解释/发布。

## 平台 contracts 的边界

contracts 固定 API/Problem/RunEvent、Manifest/Blueprint/Brief/Outcome、Interaction/Effect/Evidence 与 Tool request/response/error 等通用协议，不预置某类业务结果模型或保存 Skill Gold。业务实例和评价数据在各自资产/测试边界，结构正确不证明推理正确。

## 解释请求与响应

### 冻结输入

冻结 SkillSource/hash/文件索引/adapter、归一化与静态报告、资源/Tool catalog、目标协议、Interpreter Skill/version/prompt checksum、模型参数、父解释及调整指示。不包含 Project credential。

### 结构化响应

响应包含 summary、compatibility、Blueprint、trace/diagnostics 与可选 confidence/assumptions/questions/TaskContractDraft。完整结构失败不能偷包自由文本为已验证 Manifest；调整追加新 Interpretation、parent lineage 和 diff。

默认 SDK structured-output；显式 PROJECTMIND_SKILL_INTERPRETER_ACCEPT_PROMPT_JSON=true 可接收文本中的完整 JSON，但不补字段或绕过 Schema/identity/hash/gate。兼容路径不算 structured-output 已验收。

### 防提示注入

平台规则、发布 guidance、用户输入、Tool 外部数据分别信任。来源文本不得泄露 Secret、修改系统指令或授予未注册 Tool；记录 diagnostic 并按上层边界处理。发现明文 credential 在模型解释前阻断，不等模型脱敏。

## 兼容级别与运行就绪度

### 发布与就绪的判断顺序

| 事实 | 仍需检查 |
| --- | --- |
| parse / Preview | 来源可审查，不代表有合法蓝图或可发布 |
| gate_passed | 无 hard error，warning 仍需 ADMIN 显式接受 |
| PUBLISHED | 精确版已发布，Project 仍须显式启用 |
| Project 已启用 | 资源、真实已安装 Provider 与策略决定 readiness |
| RUNNABLE/ACTIONABLE | 创建重验输入/资源，apply 仍校验精确批准 |

### 兼容级别

native/adapted/assisted 是实际小写标签，不是发布状态。native 不提供绕过 Interpreter 的路径；Adapter 只归一化，手工 native fixture 不构成产品通道。assisted 产生待接受 warning，仍满足其他 gate。

### 运行就绪度

GUIDANCE_ONLY 仅指导/人工清单；CONFIGURATION_REQUIRED 待参数/资源；RUNNABLE 只读/工作区能力就绪；ACTIONABLE 识别可 apply 的资源与 Provider，不表示 Proposal 已批准或远端可达。

SkillVersion 属 Organization，Project 复用精确版并独立计算 readiness。未启用不可发现，新发布不自动增加执行面。

## 校验与发布门禁

硬门禁：来源/hash/解释 identity 一致，蓝图合法，trace 指向真实文件，Tool requirement 版本格式正确，无 credential/路径逃逸/任意宿主命令授权/远程可执行引用，效果正确区分 observe/propose/apply，Manifest checksum/lineage/发布者有效。

来源检查覆盖全部 Manifest/Blueprint Task 的 key/capability 对应，不只验证当前选中的任务。Blueprint target 必须能在原蓝图解析；Blueprint 与动态契约 trace 的文件/行均核对保存的原索引及可用文本，证明范围见[来源核验](task-flow.md#身份来源与失败)。契约 trace 的 field_path 保留现行 Schema 与原文，不将尚未定义的业务字段定位语义当作已经验证，也不据此断言模型理解正确。

未知业务概念、缺可选 Schema/ViewSpec/fixture、Project 未配资源、推荐步骤不完整或 assisted 标签本身不构成 hard error；必要配置仍须创建前补齐，warning 必须明确接受。

蓝图只由 Interpreter 生成；parse Draft 可为 null，发布报 capability_blueprint_missing。Manifest Schema 容纳 Draft 不等于免发布 gate，不能从旧 workflow/data_sources 反推蓝图。

## RuntimeManifest v1alpha1

[Schema](../../PJM/contracts/runtime-manifest/v1alpha1.schema.json)中的 manifest_version 精确为 projectmind/v1alpha1。Manifest 冻结蓝图/任务、Tool/资源、规则、执行建议、效果和可选 Schema；字段以契约为准，不另造语义摘要字段。

从 DRAFT 创建起内容/checksum 固定。发布状态、发布者/时间、接受 warning 属版本/gate metadata，不重写 Manifest。资源唯一声明位于 capability_blueprint.resource_requirements，无顶层 data_sources；具体 Integration/Secret 不进入 Manifest。

## Skill 组合

现行 SkillComposition（Module）只把精确版本任务归组，ProjectComposition 控制展示；无跨 Skill 规则求解器、自动编排或组合授予权限。

### 组合保存的授权事务

创建、更新、删除仅本组织当前 ADMIN 可用；普通成员不能写。三项操作必填原会话/CSRF，业务事务复用[原凭据校验](authentication.md#认证与业务提交不是同一个事务)，不接受调用方另传创建者。先锁 Organization → User SHARE → 原 AuthSession UPDATE → 当前 Project SHARE，复核 ACTIVE；更新/删除再锁同组织精确组合与全部项目关联，关联按 ID 排序。锁、读取及 flush 后用新时间复核，锁保持到提交。

| 操作 | 保存边界 |
| --- | --- |
| 创建/更新 | 所有指定版本经共享 `require_current_task_binding`：同组织 Skill/Source、精确 PUBLISHED、当前项目存在未停用启用关系；按 UUID 排序取 Version SHARE → 启用关系 SHARE，保存仍按原输入去重后的展示顺序 |
| 删除 | 不要求版本今天仍可用；只解除当前项目关联，最后一个关联消失才删除组合及其 items |
| 已有配置读取 | 保留停用/废弃后的配置，不改成 latest；不通过错误跨组织关联返回其他组织组合或版本名称 |

无权/不存在项目或组合统一 404；已授权归档项目 409。绑定不存在、跨组织、未发布或不可用均为静态 `module_rejected` 422，不暴露外组织状态。领域拒绝返回前仍复核资格；SQL 失败只用锁内资格副本分类，不读取已失效 ORM。取消和提交响应未知不转为成功、不自动重试，最终持锁判定不承诺物理 commit 瞬间未过期。

### 共享更新与删除的区别

组合是组织共享对象：经项目 A 更新名称、说明或 items，会影响所有关联项目的展示，包括归档项目 B；当前项目门禁不是逐项目编辑隔离，也不会替 B 启用版本。跨项目提示/采用、并发版本比较、幂等回执与组合历史尚待成套定义；不默默复制为项目私有组合或用行锁宣称已解决覆盖冲突。

整项目删除只清该项目关联，可能留下无关联的组织组合及其版本引用；这不同于上述显式组合删除，不擅自级联清理组织资产。旧 Run、Manifest、启用事实与审计均不随展示配置变化。

未来组合提示/偏好须先定版本/快照：平台安全最高，Project scope 限制，各 Skill required 均保留；冲突要求选择，不直接运行。组合只可调推荐步骤/表达，虚拟角色不是系统角色。

## 版本、回滚与评价

### 版本内容与可见性

| 操作 | 现行边界 |
| --- | --- |
| 发布/废弃 | DRAFT → PUBLISHED → DEPRECATED；重复发布读原行，废弃不可复活 |
| 项目启用 | 本组织精确 PUBLISHED；活动关系重复启用读原行 |
| 项目停用 | 保留记录，禁止新发现/创建，不取消或修改旧 Run |
| 同版重新启用 | 当前拒绝恢复已停用关系，不是普通开关 |
| 删除废弃版 | 独立入口检查执行/配置引用，不作升级/回滚步骤 |

当前不能将已停用 v1 直接重新启用；只能选仍合法可用的精确版，或重新解释发布新版本，后者不是恢复原身份。不删关系/直接改 DB 绕过。组合不复活废弃版，Run/Schedule 不跟随 latest。

删除保护覆盖 RunSkillSnapshot、ChangeProposal、SkillCompositionItem、TaskSchedule、TaskScheduleOccurrence 和 FrontendModuleVersion，状态不影响保留。任一引用存在即 409；仅无引用的废弃版连同其 Manifest/项目启用关系一起删除，不删除来源、解释、资源或审计来解除拒绝。外键 RESTRICT 与版本锁保留，真实并发仍需验收。

版本管理复核[业务事务中的原 ADMIN 会话](skill-interpretation.md#版本管理的授权事务)。新建 Run 和调度保存/恢复在各自事务固定精确版本及启用关系；锁外曾解析成功不保证保存时仍可用。原 Run 确认、调度暂停/归档不因此要求版本重新可用。

### 可审计的重新启用与回滚

目标另定义 ADMIN 对 Project + 精确 PUBLISHED 的显式恢复，并追加 actor/时间/原因/所据状态；不清 disabled_at 抹历史，不复活 DEPRECATED。

重复请求不重复记事实，并发启停按版本冲突，重新读后由用户确认；载体、幂等身份和 Schema 一起定义。仅影响新发现，不改权限/Run/Schedule/组合，不自动恢复 ERROR Schedule。双请求启停不是原子“一键切换”，需独立协议。

### 生命周期验收与质量评价

- parse 无蓝图、hard error、未接受 warning 分别拒绝，不将 gate_passed 当发布。
- 未启用不可发现、缺资源需配置；升级/停用/删除不改旧 Run 与 Manifest。
- 组合验证原 ADMIN/项目、锁等待失效、精确版本与跨组织拒绝、共享两项目更新/末次解绑和完整回滚；真实 DB 竞争不由 SQL 替身或 API fake 证明。
- 目标重新启用保留历次事实，重复不增记，并发不覆盖，不复活废弃版。
- Interpreter 用独立通用样例评估规则保真、资源/Tool/效果映射、trace 和调整量；覆盖资料分析、代码审查、缺资源、写入意图、恶意指令。
- 质量答案与执行输入隔离，Schema exact match 不代替模型/人工质量评审。

实施同步：[解释实现](skill-interpretation.md)、[契约工作流](../development/contract-workflow.md)、[skills 回归](../../PJM/backend/tests/skills/)。现有状态/引用测试与真实并发、模型质量分别举证；旧不可变版本仅审计读取，不恢复为平台预定义业务规则。
