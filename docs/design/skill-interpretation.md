# Skills 解释与发布实现

> 当前实现说明（旧 docs/11）。语义契约见[Skill 契约](skill-contract.md)，运行行为见[Agent Runtime](agent-runtime.md)。本页负责解释到发布的代码路径、确定性门禁和验证，进度只见[实施计划](../planning/roadmap.md#13-当前执行状态)。

## 1. 目标与主流程

```text
文件输入 → Normalize / Static Analyze → immutable SkillSource
                                         ↓
                            冻结解释 identity 与 catalog
                                         ↓
                              模型生成 Blueprint candidate
                                         ↓
                           确定性验证 → Preview / 调整
                                         ↓
                          SkillVersion DRAFT → ADMIN 发布
                                         ↓
                        Project 启用精确版本 → TaskCatalog
```

Interpreter 理解业务目标、所需资源、工作规则、交付物和可能效果。平台不要求每个 Skill 提供业务 Schema，也不把建议步骤编译为必须按顺序执行的工作流。

## 2. 当前基线

Directory/Generic Markdown 导入、不可变来源、结构化模型解释、追加式调整/diff、DRAFT/发布、组织资产与项目启用均已有实现。确定性 parser 不调用模型，返回的初始 Draft 可以没有蓝图；它只是解释输入预览，不是已可发布的运行契约。

ZIP/TAR 自动解包、远程 Git URL 导入、TaskFlowProjection 与生成前端模块执行不是当前已提供的解释链路。真实模型反复、业务规则保真和外部系统验收不由静态验证代替。

模型输出默认走 SDK structured-output；显式开启 prompt JSON 兼容时仍执行完整契约验证，二者需要分别验收。详见[响应边界](skill-contract.md#62-结构化响应)。

## 3. 信任边界

- 来源 Skill、附件、票据、代码、文档都是待解释数据，不能覆盖平台指令。
- Tool/Shell 声明是需求信号，不能授予执行权限。
- 明文 credential 的实际静态发现仍阻止模型解释；不能把“模型会脱敏”当作允许上传/发送的理由。
- Interpreter 不读取项目 Secret、不访问 Integration、不发布版本、不创建 Run。
- required 业务规则不可为凑成可运行任务而删除或降为推荐；可替换的是实现手段。

## 4. 代码责任

| 模块 | 责任 |
| --- | --- |
| [importer.py](../../PJM/backend/src/projectmind/skills/importer.py) | 归一化、文件/引用索引、静态 Draft |
| [interpreter.py](../../PJM/backend/src/projectmind/skills/interpreter.py) | 静态分析、请求/响应 identity 与报告 |
| [model_interpreter.py](../../PJM/backend/src/projectmind/skills/model_interpreter.py) | 模型适配与结构化生成 |
| [interpreter_execution.py](../../PJM/backend/src/projectmind/skills/interpreter_execution.py) | 冻结执行、复用与恢复 |
| [capability_blueprint.py](../../PJM/backend/src/projectmind/skills/capability_blueprint.py) | 蓝图校验与投影 |
| [task_contract.py](../../PJM/backend/src/projectmind/skills/task_contract.py) | 受限动态契约编译 |
| [manifest_gate.py](../../PJM/backend/src/projectmind/skills/manifest_gate.py) | 发布前结构、来源、能力和 warning gate |
| [service.py](../../PJM/backend/src/projectmind/skills/service.py) | 解释、版本 lifecycle 与 Project 可见性协调 |

系统解释 Skill 的正文是[版本化执行资产](../../PJM/skills/projectmind-skill-interpreter/SKILL.md)。修改它及 references 会改变 package hash 和 prompt identity，需要同步版本、example 和回归；不当作普通说明文档搬迁。

这些入口共同守护“蓝图只由 Interpreter 生成”的边界；没有全局 bypass，parse 期的空蓝图不由 importer 补造。发布约束详见 §5.3。

## 5. CapabilityBlueprint

### 5.1 数据结构

[Blueprint v1 Schema](../../PJM/contracts/capability-blueprint/v1.schema.json) 定义真实字段。核心必填为 `blueprint_version`、`identity`、`compatibility`、`capabilities`、`tasks`、`resource_requirements`、`guidance`、`source_traces`；interaction/effect/preferences/assumptions/questions 按 Schema 使用。

自然语言标题和说明跟随 Skill 源语言，标识符与协议 enum 保持原定义。不能笼统要求所有 enum 为小写；大小写由各 Schema 决定。语言保真属于模型质量验收，静态校验只证明字段形状。

### 5.2 确定性规则

| 校验 | 失败或不足的处理 |
| --- | --- |
| identity、source hash、来源 trace | 不一致时拒绝，不引用不存在的文件 |
| schema、业务目标、必需规则 | 无法形成安全目标时拒绝 |
| 领域 capability 名 | 新业务概念允许，不要求进入 Tool catalog |
| Tool capability | 真实调用必须注册；不支持的 requirement 降低 readiness |
| 缺少业务 Schema/ViewSpec/fixture | 不能单独作为硬错误；相关 warning 须显式接受 |
| Assisted compatibility | `assisted_review_required` warning；ADMIN 接受后仍需通过其它发布门禁 |
| 原文中的工具声明 | 作为重表达线索，执行授权不随声明增加 |
| 明文 credential | 解释前阻断，来源清理后再导入 |

就绪度使用实际已安装 Provider，而不只查 catalog 名称。发布不能消除运行前配置与权限校验。

### 5.3 蓝图是唯一来源

CapabilityBlueprint 只能由 Interpreter 生成。parse 期没有蓝图时返回 null；发布 gate 对缺失蓝图报 `capability_blueprint_missing`。Worker 读取冻结 Manifest 中的蓝图，不从旧 workflows/data_sources 反向编造。

Manifest Schema 为保留导入 Draft 形态，不在 JSON required 中强制 blueprint；**发布门禁**进一步要求 blueprint。不能将“Schema 可通过”理解为“已经允许发布”。

### 5.4 过程重表达

| Skill 原始表达 | 当前可用表达 | 边界 |
| --- | --- | --- |
| curl 读取票据 | `issue.read/v1` | 地址/凭据归 Integration |
| svn/git 读取文件 | `repository.read/v1` | 只保留绑定范围内的相对路径与 revision |
| list/grep 发现文件 | 资源物化 + `files.txt` + `workspace.search/read` | 不新增自由 shell |
| log 查看历史 | repository 物化的有界 `history.txt` | 已支持；不表示完整任意历史查询 |
| xlsx/xlsm/docx 解析 | 平台文本化 + workspace 读取 | 已支持格式保留位置；PDF 等仍不支持 |
| 写报告/补丁候选 | `workspace.write/v1` 到 workspace/output | 不修改 input 或原始资源 |
| 更新票据/提交代码 | `change.propose/v1` → 平台批准/效果链 | 不给 Agent issue.update/repository.write 直调权 |
| 无等价能力的步骤 | 明确限制、请求人工处理或 GUIDANCE_ONLY | 不虚构 Tool，不弱化业务必需规则 |

模型重表达由 system Skill 指导；确定性守卫检查 guidance 中版本化 capability 的注册与资源声明。守卫只能核对被显式引用的能力与结构，**不能证明自然语言语义等价，也不能证明所有实际参数正确**；实际调用参数仍由 Tool Gateway 校验，规则保真由 Preview/质量评审检验。

### 5.5 TaskFlowProjection

可选流程投影属于[后续 Task Flow 设计](task-flow.md)。当前不向 Blueprint/Brief 自由添加未经 Schema 校验的节点字段。

## 6. ResourceBinding 与 readiness

### 6.0 与项目启用的区别

先检查精确 SkillVersion 是否已在 Project 启用，再计算资源就绪度。未启用版本不可发现；已启用但缺资源的版本显示缺失原因。启用不授予数据访问或写入权限。

### 6.0.1 唯一资源声明

资源要求仅来自 `capability_blueprint.resource_requirements`。Manifest 没有顶层 `data_sources`。Tool 投影使用同一 requirement key，write capability 不折叠进 Agent 的 allowed_capabilities。

### 6.1 Scope

空 allowlist 不授予任何值。Redmine 的 issue_ids/field_keys 支持显式 `["*"]`；子 scope 不得从枚举扩大为 wildcard。repository 路径 scope 不接受此 wildcard。LOW 预授权必须精确列举范围，不接受 wildcard。

### 6.2 就绪度

`GUIDANCE_ONLY / CONFIGURATION_REQUIRED / RUNNABLE / ACTIONABLE` 是任务投影。requirement 另有 `AVAILABLE / UNAVAILABLE / UNSUPPORTED`。实现见 [resource_binding.py](../../PJM/backend/src/projectmind/skills/resource_binding.py)。

ACTIONABLE 不是有效批准，也不是对真实连接可达性的证明。创建 Run 与 Provider 调用仍重新校验配置、绑定和策略。资源授权、内容快照与 document 联调差距见[资源快照](resource-snapshots.md)。

## 7. AgentTaskBrief 与 ExecutionProfile

### 7.1 Brief 生成

蓝图 guidance、目标、资源、允许工具、checkpoint、deliverables 与 limits 投影到 [AgentTaskBrief v1](../../PJM/contracts/agent-task-brief/v1.schema.json)。每个 Segment 保存 brief_json/checksum；物化路径只取物化器返回值。

required rules 和质量标准必须完整进入 Brief，不能只传摘要。日志只写 identity、checksum 和执行 profile 等审计元数据，不记录包含业务输入的 Brief 正文；必要的受控快照保存在平台记录内。

### 7.2 自主等级

GUIDED/SUPERVISED/DELEGATED 由策略合成，默认 SUPERVISED。任何等级都不能覆盖平台 Tool 硬拒绝、作用域或批准。运行语义由[Runtime](agent-runtime.md#5-自主执行等级)负责。

## 8. RunSegment、多 Session 与交互

### 8.1 数据兼容

新 Run 显式保存 Segment 与 Brief，旧 Run 可按 implicit Segment 只读投影。历史记录不回填虚构业务事实。

### 8.2 执行与续行

用户响应/批准追加 Segment，故障恢复追加同段 Attempt；等待释放 lease，交互使用独立期限。同一 Run 顺序主 Session 与只读并行子 Session 的边界分别见[Runtime](agent-runtime.md#7-持续-run-与多会话)和[子分析](subagents.md)。

### 8.3 API 与 UI

Interaction response 检查 Project actor、版本、期限与幂等键；Effect approval 还限制为 Run 发起人或 ADMIN。Web 从既有 detail/SSE 显示交互和效果，不另建状态机。

## 9. Workspace Tool

workspace read/search/write 已实现，SDK 内置 Read/Bash/Web 等仍拒绝。来源脚本没有通用执行器。文件范围及文本化见[资源快照](resource-snapshots.md)。

## 10. ChangeProposal 与写入

### 10.1 注册能力

Redmine CAS issue.update 与 Git/SVN repository.write。完整契约与失败语义见[受控写入](repository-effects.md)。

### 10.2 策略

默认人工批准；只有允许预授权的能力可在 LOW、精确 scope、ADMIN policy 下自动批准。repository 永不预授权。

### 10.3 再验证

校验 Proposal version/checksum、binding、Integration、前置版本与身份，执行后必须回读。PR/commit 不能宣称跨系统原子完成。

## 11. Outcome 与 Workspace

结果遵守通用包络，task-specific structured_data Schema 可选。缺少业务 Schema 不等于 structured_output_missing。业务语义正确性须独立评估，不由 JSON Schema 保证。

## 12. 验证矩阵

| 层 | 主要验证 |
| --- | --- |
| Parser | 不执行来源、路径/大小/引用、来源 hash |
| Interpreter | frozen identity、结构响应、修复失败、追加式调整/diff |
| 发布 | Blueprint 必需、warning acceptance、精确版本与不可变性 |
| 泛化 | JAF、repository-review、开放式文档任务，不增加平台业务分支 |
| 安全 | 敏感来源阻断、能力不扩权、scope/跨 Project 拒绝 |
| 模型质量 | 固定输入各重复至少三次，人工评估规则保真、证据和调整量 |

测试入口：[skills tests](../../PJM/backend/tests/skills/) 与[契约测试](../../PJM/backend/tests/contracts/)。固定模型测量命令见[本地开发](../development/local-development.md)。

## 13. 后续工作依赖

解释结构或系统 Skill 变更 → 通用契约与版本/checksum → Backend 校验/投影 → Web validator/预览 → example/回归/模型质量报告。实际工作顺序只维护在[计划](../planning/roadmap.md#132-下一步与当前决策)。

## 14. 非目标

任意来源脚本执行、自动扩权、自动升级 PUBLISHED 版本、平台业务 Schema、生成后端服务。调度与只读子分析已由各自模块实现，不继续列为全局未实现能力。

## 15. 完成条件

契约、consumer、版本与验证材料一致；新 Skill 通过普通路径配置和执行；历史版本可读且不可变；不能以本地结构测试替代模型质量或真实系统验收。
