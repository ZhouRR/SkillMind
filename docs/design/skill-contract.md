# Skill 兼容、解释与发布

本页是目录式 Skill 的语义与生命周期正本；代码见[解释实现](skill-interpretation.md)，缺口见[计划 R06](../planning/roadmap.md#开发任务)。

## 设计目标

外部 Skill 无需改成平台专用目录/业务 Schema；Interpreter 仅识别调用输入、资源和操作要求；平台将完整冻结原文交给 Agent 自主执行，固定跨组件协议与授权边界。

Tool/Shell/network/write 声明不是权限。原文规则/禁止事项不因缺工具而弱化，执行顺序与条件由 Agent 按原文处理；来源/解释/调整保留 trace，发布版本不可原地修改。

## 分层架构

```text
可信 Adapter/Normalizer → 不执行来源的静态分析
  → 版本化 Interpreter 系统 Skill + 模型
  → 确定性 Validator → Preview / 追加调整
  → ADMIN 发布 → Project 启用 → TaskProjector / RuntimeLoader
```

业务依赖 SkillInterpreter 抽象而非 SDK message。来源不能替换 Adapter；脚本只作来源/checksum 对象，执行须走注册 Provider。

## 导入兼容层

[importer](../../SKM/backend/src/skillmind/skills/importer.py)支持本地目录 CLI、内联 JSON、多文件 multipart，读取说明、metadata、references、scripts 和文本资产。HTTP 的[限额/原 ADMIN 授权/跨存储失败](skill-interpretation.md#导入保存与上传授权)由用例负责；CLI 只本地解析，不伪造会话或直接持久保存。

不执行来源脚本、装依赖、访问来源 URL、解析 Secret。ZIP/TAR 自动解包、远程 Git 尚不支持；新增须先定义路径/symlink/设备文件、体积/数量与固定 revision 防护。

NormalizedSkillPackage 保留身份、adapter version、hash、文件索引、原文/未知 metadata、静态发现与 trace，不丢未理解内容或提升权限。

## Interpreter 的核心输出

### 原文执行声明

新导入使用 [SkillExecution v1](../../SKM/contracts/skill-execution/v1.schema.json)：一个任务的标题/说明、资源要求和允许提案的操作。调用输入只在 Manifest 的 `input_contract` 声明一次。内部阶段、批准和恢复不拆为独立任务；业务规则、表名/列名、保存路径、条件、结果格式与恢复说明保留在完整 Skill 原文及引用文件中，不生成第二份业务执行计划或结果 Schema。

### 资源与输入

`resource_requirements` 保留 kind、required、access、capabilities、selection_guidance；accepted_providers 是兼容提示，不代替 capability/scope 校验。每个写入资源的 `operations` 指明实际 capability/operation，写入目标在启动前必需绑定，包括条件写入；绑定可用不等于批准或强制执行。可选只读补充资源仍可省略。

同一连接和授权用途的多张表共用一个资源。文档输入与保存目标分开绑定；文书范围已提供的路径、ID、库标识和 bucket 不重复作为调用参数。只有用户必须选择的业务值进入受限 TaskContractDraft。改 Integration/文档/权限范围须新 Run，见[资源快照](resource-snapshots.md)。

### 旧版本兼容

旧 [CapabilityBlueprint](../../SKM/contracts/capability-blueprint/v1.schema.json) 及 Brief v1 保持原规则、结果契约、effect_intents 和 checksum；不自动转换历史版本或 Run。Manifest 的 `skill_execution` 与 `capability_blueprint` 互斥，未知执行版本拒绝；新解释入口不能返回旧格式绕过候选验证。共享消费者读取原声明，不从 Manifest 逆生成 Blueprint。

## 平台 contracts 的边界

contracts 固定 API/Problem/RunEvent、Manifest/SkillExecution/旧 Blueprint/Brief/Outcome、Interaction/Effect/Evidence 和 Tool 通用协议，不预置业务结果模型或保存 Skill Gold。实例/评价数据留各自资产/测试边界，结构正确不证明推理正确。

## 解释请求与响应

### 冻结输入

冻结 Source/hash/索引/adapter、归一化/静态报告、资源/Tool catalog、目标协议、Interpreter Skill/version/prompt checksum、模型参数、父解释/调整指示，不含 Project credential。

### 结构化响应

响应包含最小执行声明、输入契约、原文和诊断；身份、hash、固定工作流及平台 OutcomeEnvelope 由平台绑定。结构失败不将自由文本包装为已验证 Manifest；调整追加 Interpretation、lineage/diff。

默认 SDK structured-output；显式 SKILLMIND_SKILL_INTERPRETER_ACCEPT_PROMPT_JSON=true 可接文本完整 JSON，仍不得补字段/绕 Schema、identity/hash/gate，且不能当 structured-output 验收。

### 防提示注入

平台规则、发布的 Skill 原文、用户输入、Tool 外部数据分层信任。来源不得泄露 Secret、改系统指令或授未注册 Tool；记录 diagnostic 并受上层边界约束。发现明文 credential 在模型前阻断，不等模型脱敏。

## 兼容级别与运行就绪度

### 发布与就绪的判断顺序

| 事实 | 仍需检查 |
| --- | --- |
| parse / Preview | 可审查，不代表声明合法或可发布 |
| gate_passed | 无 hard error，warning 仍须 ADMIN 接受 |
| PUBLISHED | Project 显式启用精确版 |
| Project 已启用 | 资源、真实已安装 Provider/策略决定 readiness |
| RUNNABLE/ACTIONABLE | 创建重验输入/资源，apply 重验精确批准 |

### 兼容级别

native/adapted/assisted 是小写兼容标签，不是发布状态。native/手工 fixture 不绕 Interpreter；Adapter 只归一化，assisted 仍满足门禁并需接受 warning。

### 运行就绪度

GUIDANCE_ONLY：指导/人工清单；CONFIGURATION_REQUIRED：待参数/资源；RUNNABLE：只读/工作区能力就绪；ACTIONABLE：存在可 apply 的资源/Provider，不代表已批准或远端可达。

版本属组织，Project 分别启用和计算 readiness；未启用不可发现，新发布不自动扩大执行面。

## 校验与发布门禁

硬门禁检查来源/hash/解释 identity、蓝图结构、真实 trace、Tool 版本格式、credential/路径逃逸/宿主命令授权/远程可执行引用、observe/propose/apply 区分、Manifest checksum/lineage/发布者。

来源验证覆盖全部 Manifest/Blueprint Task 的 key/capability，不只所选任务；Blueprint target 须能在原蓝图解析。Blueprint/动态契约 trace 的文件/行按原索引及可用文本核验，见[来源核验](task-flow.md#身份来源与失败)。field_path 保留现行 Schema/原文，不虚称已验证尚未定义的业务字段定位或模型理解。

未知业务概念、缺可选 Schema/ViewSpec/fixture、项目缺资源、推荐步骤不完整、assisted 本身不作 hard error；必要配置须创建前补，warning 必须明确接受。

蓝图仅由 Interpreter 生成；parse Draft 可 null，发布报 capability_blueprint_missing。Schema 容纳 Draft 不豁免发布，不从旧 workflow/data_sources 反推蓝图。

## RuntimeManifest v1alpha1

[Schema](../../SKM/contracts/runtime-manifest/v1alpha1.schema.json)固定 manifest_version=skillmind/v1alpha1。Manifest 冻结蓝图/任务、Tool/资源、规则、建议、效果及可选 Schema，不增平行语义摘要。

任务可声明 `document_prerequisites`：引用原蓝图的 apply intent keys，须有对应 `/tasks/<index>/document_prerequisites` 原文 trace，并必需声明 `document.readiness/v1`。Interpreter 只从“文档处理前必须成功保存”的无条件规则提取，不把建议顺序或条件步骤改成强制前置；原条件保留在 Task、Brief 和审阅投影中，运行与旧 Worker 边界见[资源快照](resource-snapshots.md#excel-取得与转换的扩展边界)。它只增加执行限制，不代替批准或证明业务内容正确。

DRAFT 起内容/checksum 不变；发布状态、actor/时间、warning 接受属于版本/gate metadata。资源只声明于 capability_blueprint.resource_requirements，无顶层 data_sources，不含 Integration/Secret。

## Skill 组合

SkillComposition（Module）仅归组精确版本任务，ProjectComposition 控制展示；无跨 Skill 规则求解、自动编排或组合授权。

### 组合保存的授权事务

三写仅当前本组织 ADMIN。必填原会话/CSRF，不另传创建者；复用[原凭据校验](authentication.md#认证与业务提交不是同一个事务)，锁 Organization → User SHARE → 原 AuthSession UPDATE → 当前 Project SHARE 并复核 ACTIVE；更新/删除再锁同组织组合和按 ID 排序的全部关联。锁、读取、flush 后取新时间复核，持锁至提交。

| 操作 | 边界 |
| --- | --- |
| 创建/更新 | 共享 require_current_task_binding 验同组织 Skill/Source、精确 PUBLISHED、当前未停用启用关系；按 UUID 锁 Version SHARE → 关系 SHARE，展示仍按原输入去重顺序 |
| 删除 | 不要求版本今日可用，只解绑当前项目；末个关联消失才删组合/items |
| 已有读取 | 停用/废弃仍保留配置、不换 latest；错误跨组织关联不泄露名称 |

无权/不存在统一 404，授权归档 409；缺失/跨组织/未发布/不可用绑定统一静态 module_rejected 422。领域拒绝前重验资格，SQL 失败仅以锁内副本分类、不读失效 ORM。取消/提交未知不当成功、不重试；最终持锁检查不承诺物理 commit 瞬间未过期。

### 共享更新与删除的区别

组合是组织共享对象：A 更新名称/说明/items 会影响全部关联项目（含归档 B），但不替 B 启用版本。跨项目提示/采用、版本比较、幂等回执/历史尚待定义；不私自复制为项目私有或用行锁宣称解决覆盖冲突。

整项目删除仅清自身关联，可留下无关联组合及版本引用；不级联清组织资产。旧 Run/Manifest/启用事实/审计不变。

未来组合先定义版本/快照：平台安全优先、Project scope 限制、各 Skill required 保留，冲突须选择而非直接执行。组合只能调整建议/表达，虚拟角色不是系统角色。

## 版本、回滚与评价

### 版本内容与可见性

| 操作 | 边界 |
| --- | --- |
| 发布/废弃 | DRAFT → PUBLISHED → DEPRECATED；重复发布读原行，废弃不复活 |
| 项目启用/停用 | 精确 PUBLISHED；活动关系重启用读原行，停用保留记录、不改旧 Run |
| 同版重新启用 | 当前拒绝恢复已停用关系，不是普通开关 |
| 删除草稿/废弃版 | 独立引用检查，不作升级/回滚步骤 |

停用 v1 不能直接复活；选择仍合法精确版或解释发布新版本，后者不是恢复原身份。不删关系/改 DB 绕过，组合不复活废弃版，Run/Schedule 不跟随 latest。

删除保护全部状态的 RunSkillSnapshot、ChangeProposal、SkillCompositionItem、TaskSchedule、TaskScheduleOccurrence、FrontendModuleVersion；任一引用 409。仅无引用草稿或废弃版可连同 Manifest/启用关系删除，不删来源/解释/资源/审计解除拒绝，保留 RESTRICT/版本锁。 页面将删除/废弃拒绝显示在对应版本行，保留已加载列表；列表刷新失败时保留只读记录并提供重新加载，重新加载只发读请求，不重发删除。

版本六操作须[重验原 ADMIN](skill-interpretation.md#版本管理的授权事务)。新 Run、调度保存/恢复在事务固定精确版/启用关系；原 Run 确认、调度暂停/归档不要求版本重新可用。

### 可审计的重新启用与回滚

目标是 ADMIN 对 Project + 精确 PUBLISHED 显式恢复，追加 actor/时间/原因/所据状态；不清 disabled_at 抹历史或复活 DEPRECATED。

须成套定义载体、幂等身份/Schema：重复不重复记事实，并发启停版本冲突，重读后人工确认。只影响新发现，不改权限/Run/Schedule/组合、不自动恢复 ERROR Schedule；两次启停不等于原子切换。

### 生命周期验收与质量评价

覆盖无蓝图/hard error/未接受 warning、未启用/缺资源、旧 Run/Manifest 不变；组合原授权/精确版/跨组织、共享更新/末次解绑及回滚；目标重新启用另验追加审计与竞争。

Interpreter 用独立通用样例评审规则保真、资源/Tool/效果映射、trace/调整量，覆盖分析、代码审查、缺资源、写意图、恶意指令。质量答案与执行输入隔离，Schema exact match 不代替模型/人工评审。

改动同步[解释实现](skill-interpretation.md)、[契约 workflow](../development/contract-workflow.md)、[skills 回归](../../SKM/backend/tests/skills/)；真实事务/模型质量分别举证，旧不可变版本只作审计，不恢复平台预定义业务规则。
