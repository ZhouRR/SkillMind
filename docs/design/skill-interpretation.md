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
| [manifest_gate](../../PJM/backend/src/projectmind/skills/manifest_gate.py) | 来源、结构、能力与 warning gate |
| [service](../../PJM/backend/src/projectmind/skills/service.py) / [repository](../../PJM/backend/src/projectmind/skills/repository.py) | 解释协调、版本/可见性、引用门禁和 TaskCatalog |

系统 [Interpreter Skill](../../PJM/skills/projectmind-skill-interpreter/SKILL.md)是版本化执行资产，改正文/references 必须同步 package hash、prompt identity、版本/example/回归，不当普通文档搬迁。

### 从候选到项目任务的接线

PREVIEW_READY → create_version_draft 绑定真实 Interpretation/Blueprint identity，冻结 Manifest/checksum/findings → publish_skill_version 检查 hard error 与所有 warning → 显式项目启用 → 按真实资源计算 readiness。

DRAFT 可保存错误供审查；发布不重新调用模型或修改 Manifest。来源/解释改变需追加解释和新 DRAFT。TaskCatalog 不证明外部可达；创建/Provider 重验。来源重建、模型恢复与发布复用是不同幂等边界。

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
| 报告/补丁 | workspace.write/v1，仅 workspace/output |
| 修改外部内容 | change.propose/v1 → 批准/效果链，不给 Agent 直调 write |
| 无等价能力 | 说明限制、人工处理或 GUIDANCE_ONLY，不虚构 Tool/弱化 required |

确定性守卫只检查显式能力引用与结构，不证明语义等价或运行参数正确；Gateway 校验参数，Preview/质量评审检查保真。新增 [Flow](task-flow.md)字段须先冻结 Schema，不随意扩 Blueprint/Brief。

## ResourceBinding 与 readiness

资源唯一来自 capability_blueprint.resource_requirements，无顶层 data_sources；Tool 投影引用相同 key，write 不折入 Agent allowed_capabilities。

空 allowlist 不授权。issue_ids/field_keys 允许显式 ["*"]，子 scope 不能枚举扩 wildcard；repository path 不用该 wildcard，LOW 预授权必须精确 scope。

[resource_binding](../../PJM/backend/src/projectmind/skills/resource_binding.py)先查精确项目启用，再算任务 readiness 与 requirement AVAILABLE/UNAVAILABLE/UNSUPPORTED；依据真实已安装 Provider。候选可用不是选择或实际连通，选择创建前冻结，运行中 CHOICE 不换绑。旧注释若相反按[冻结规则](resource-snapshots.md)修正。

同版停用后不可恢复，发布不可复活 DEPRECATED；目标审计恢复由[生命周期](skill-contract.md#可审计的重新启用与回滚)维护。

## Brief 与运行交接

[AgentTaskBrief](../../PJM/contracts/agent-task-brief/v1.schema.json)按 Segment 保存 brief_json/checksum，完整带 required/quality、目标、资源、权限、交付和限制。物化路径只来自物化器，日志仅 identity/checksum/profile，不记录业务 Brief 正文。

GUIDED/SUPERVISED/DELEGATED 默认 SUPERVISED，不能覆盖硬拒绝。新 Run 显式 Segment/Brief，旧 Run 只读隐式投影；续行/Attempt、Session、workspace 和效果的唯一规则分别在 Runtime、[资源](resource-snapshots.md)、[受控写入](repository-effects.md)，不再维护副本。

结果用通用 Outcome，可选 task-specific structured_data；缺业务 Schema 不等于 structured_output_missing，结构不保证业务正确。

## 验证与接续

- Parser：不执行来源，路径/大小/引用/hash；Interpreter：冻结 identity、结构输出、失败恢复、追加调整/diff。
- 发布：蓝图必需、warning 接受、精确版本/不可变、组织/项目启停与引用保护；目标重新启用另验并发/审计。
- 泛化：资料分析、代码审查、开放文档、缺资源、恶意指令走同一路径，不添平台业务分支。
- 安全：敏感来源阻断、scope/跨 Project 拒绝、能力不扩权；runtime/Provider 再验证。
- 模型质量：固定输入重复采样，独立人工判断规则保真/证据/调整量；prompt JSON 与 SDK structured-output 分别举证。

回归入口：[skills tests](../../PJM/backend/tests/skills/)、[contracts tests](../../PJM/backend/tests/contracts/)。mock repository 不证明真实事务竞争；模型、部署、外部系统另按授权环境验收。

改解释结构/系统 Skill 时同步契约、版本/checksum、Backend validator/projector、Web 预览、example/回归及质量评审。不增加任意脚本、自动扩权、自动升级发布版或生成后端服务。
