# ProjectMind JAF 品质分析 Skill 迁移与端到端验收规范

本文定义现有 `Jaf 品質分析` Skill 如何迁移到 ProjectMind，并规定首版历史 Ticket 评价集、人工评价、准确度报告和端到端验收流程。

> 定位：业务迁移与质量验收要求，不是平台实施进度表。先读[Skill 解释与发布](../design/skill-interpretation.md)；当前实现、部署和 benchmark 状态见[计划](../planning/roadmap.md#13-当前执行状态)。

基础执行闭环使用手工冻结的 Native RuntimeManifest、标准 ViewSpec 和固定 fixture 验证平台纵向链路。其历史
产物只用于审计读取；新的 JAF 验收与普通 Skill 一样，由 Interpreter 从说明生成 CapabilityBlueprint，
其中 Ticket/代码是资源前提，分析规则是 Agent guidance，报告和可选 Redmine 更新是交付物/效果意图。
平台不预定义 Ticket 业务 Schema。通用 Redmine read 与首个 CAS `issue.update/v1` Provider 已实现，
但真实 deployment adapter/SVN 联调、正式 benchmark 和其余 5 类任务不会因平台能力存在而自动完成。

## 1. 现状结论

迁移调研的上游 Skill 分为 6 类任务；本验收首先覆盖单 Ticket，其余五类仅保留业务背景，不因此进入当前实现范围：

| 任务 | 现有模式 | 主要输出 |
| --- | --- | --- |
| Ticket 分析 | Step 0–11 | 字段补全、10 项整合性检查、12 项妥当性检查、报告 |
| 功能分析 | F1–F10 | 规模、密度、评审、单测障害、工程逃逸、改善建议 |
| 应用分析 | A0–A10 | 应用趋势、热点功能、质量目标、改善建议 |
| 担当者分析 | P0–P6 | 负责规模、检出倾向、作り込み密度、逃逸率 |
| Ticket 汇总 | CT-0–CT-C | 应用交叉、原因与工程交叉、排名 |
| 评审/单测汇总 | R0–R3 | 评审和单测障害横向汇总、密度、功能排名 |

上表中的 `F1–F10`、`P0–P6`、`R0–R3` 是原 JAF Skill 内部的业务任务/步骤标识，其含义以对应的 JAF 任务定义为准。

迁移调研时的上游包包含 `SKILL.md` 和 20 个 Python 脚本（属于当时的输入清单，不是当前 PJM 同梱数），但没有随 Skill 一起提供完整配置、Schema、fixture 和自动化测试。大多数脚本使用固定 `/workspace` 路径并输出供人阅读的文本，只有少量结构化输出。

完整目录式 Skill 经过 Interpreter 后的兼容级别应为 `Adapted`，不能标记为 `Native`。手工 Native Manifest 只解释早期测试历史；当前新版本须经 Interpreter 生成蓝图与发布门禁，不能重新引入手工业务 seed。

## 2. 安全前提（导入前必须完成）

迁移调研曾发现来源中包含直接连接信息和凭据。这是历史风险记录，不表示当前同梱包已被本次重新扫描，也不能据此认定已完成轮换。每次导入前必须确认：

1. 轮换已经出现在 Skill 文本中的全部凭据。
2. 从新 SkillSource、脚本、配置和测试 fixture 中删除明文凭据。
3. 对 Skill 包和可访问历史执行 Secret 扫描，扫描结果为零才允许导入。
4. Redmine、SVN 等凭据保存为 Project 的 SecretReference。
5. Skill 只声明 `issue.read/v1`、`repository.read/v1` 等 capability，不包含 URL、用户名、Token 或密码。
6. 日志、Evidence excerpt、错误和评价报告必须经过脱敏。

未完成凭据轮换和清理时，不得把现有包原样上传到 ProjectMind Object Storage，也不得创建 PUBLISHED SkillVersion。

## 3. 迁移目标目录

下列是后续迁移的逻辑分区，不是现存目录。当前回归输入见 [skills/examples](../../PJM/skills/examples/)；迁移素材见 [skills README](../../PJM/skills/README.md)。原始业务规则与完整历史保留，不能为目录美观改写 versioned 输入。

```text
可导入的 Skill package
└── jaf-quality-analysis/
    ├── SKILL.md
    ├── references/                 业务定义、规则、字段解释
    └── scripts/                    理解素材；存在不代表可执行

Project 执行资源（由该次 Run 合法选择）
├── Ticket/CSV 快照
├── repository revision
└── 字段映射、规模、设计文档          不含 case 答案

独立评价存储（不导入、不挂载给 Agent）
└── evaluation/
    ├── benchmark-v1.yaml
    ├── rubric-v1.yaml
    └── expected/
```

CapabilityBlueprint、RuntimeManifest、可选 TaskContractDraft/ViewSpec 由 Interpreter 生成并调整后发布。通用 Adapter 不依赖 `schemas/`、`views/` 或 ProjectMind 专用 metadata。

不能把 `evaluation/` 放进 Skill 的 references/tests 附件后再期望 Interpreter 自动忽略；它必须在导入包和执行资源之外。目录分开也不等于权限隔离：评价存储不得成为该 Run 可访问的 Integration、Project 文档或 workspace mount。无 Gold 的开发 fixture 可以单独用于回归，但不得混入正式评价答案。

## 4. 迁移改造规则

### 4.1 SKILL.md

- 保留 JAF 业务定义、字段映射逻辑、工程判断、逃逸判断和报告要求。
- 把 6 类任务拆分为主说明与 references，减少单文件复杂度。
- 删除连接地址、凭据、机器用户目录和固定安装路径。
- 数据源改为 capability requirement，不直接写 curl、svn 命令和认证参数。
- 保留“CSV 不回写”“知识库不自动更新”“设计书无法确认时输出两案”等约束。
- 明确 benchmark 版本、样本数量和人工评价规则。

### 4.2 Python 脚本

以下是将来把确定性计算注册为受控能力时的改造要求。当前保留的源脚本供 Interpreter 理解，不存在“导入脚本后直接执行”的通用 runner；文件存在或 checksum 匹配都不足以授权。

- 输入路径、目标 ID、编码和列映射使用 CLI 参数或 Run workspace 文件。
- stdout 只输出版本化 JSON；日志写 stderr。
- 成功、无数据、输入错误、Schema 错误和内部错误使用稳定 exit code。
- 不直接读取 Secret、调用 Redmine、调用 SVN 或访问未声明网络。
- 不写固定 `/workspace/output`；当前 Agent 通过 `workspace.write/v1` 写到 output，或在 Outcome deliverable 中返回报告。`report.export/v1` 未注册。
- 所有文件访问限制在 Run workspace 内。
- 每个脚本提供 fixture 和回归测试；脚本内部格式可自带局部 Schema，但它不能替代或成为平台 Task
  Contract 的前置条件。
- 数量、密度、交叉表等确定性计算留在脚本；原因推断、证据解释和改善建议由 Agent 完成。

### 4.3 数据访问替换

| 原始需求 | 当前可用路径 |
| --- | --- |
| Python/curl 读取票据 | `issue.read/v1`，Redmine 或固定 CSV Provider |
| SVN/Git 读取已知文件 | `repository.read/v1` |
| 文件发现/提交历史 | repository 物化 + files.txt/history.txt + workspace.search/read |
| 表格/字段映射/设计资料 | Project 文档或已物化文本；xlsx/xlsm/docx 转换范围见资源规范 |
| 连接配置与认证 | Integration + SecretReference，源码不保存实值 |
| 报告/补丁 | Outcome deliverable 或 workspace.write 到 output |
| 修改 Redmine/代码 | change.propose → 平台批准 → 注册 Effect Provider |

issue.search、issue.export_snapshot、repository.search/history/export、tabular.import、knowledge.search 和 report.export 均是旧草案名称，不是当前注册能力；本验收不以它们为前提。批量统计与全文知识索引若不可由现有路径可靠完成，记录能力缺口并另立工作包。

### 4.4 ProjectKnowledge

| Knowledge | 用途 | 更新策略 |
| --- | --- | --- |
| Redmine field mappings | CF ID、类型、选项和标签转换 | ADMIN 上传新版本 |
| App registry | 应用、功能、语言、仓库路径和设计书简称 | ADMIN 上传新版本 |
| CSV schemas | 评审和单测 CSV 列定义 | 随数据格式版本更新 |
| JAF 规模列表 | 功能、规模和担当者 | 定期上传快照 |
| 工程质量目标 | 原因工程 × 检出工程基准 | 标准变更时更新 |
| Knowledge base | 历史问题模式 | 默认只读，明确操作后更新 |

验收需记录实际使用文档的 ID、content hash 和行/字段定位；现行 `ProjectDocument` 不等于另有独立 Knowledge 版本服务。Run 创建时清单冻结与公开展示仍须通过[资源验收](../design/resource-snapshots.md)。Evaluation Gold、Rubric 和 expected 文件不得挂载到普通 Run workspace，也不得被 document/workspace Tool 或未来知识检索能力读取。

## 5. 现有脚本迁移映射

基础执行闭环只迁移 Ticket 分析实际依赖的脚本；其余脚本映射是后续路线图，不作为工程骨架开工或基础闭环验收门禁。

| 任务 | 现有脚本 | 迁移模块 |
| --- | --- | --- |
| Ticket | `parse_issue.py` | `ticket.parse_issue` |
| Ticket | `export_issues_from_api.py` | 单票读取使用 issue.read；批量导出目前无注册能力，另记缺口 |
| 功能 | `f_review_detail.py`, `f_review_mfg.py`, `f_tante_defect.py`, `f_app_position.py` | `function.*` 结构化统计 |
| 应用 | `a_app_info.py`, `a_review_detail.py`, `a_review_mfg.py`, `a_tante_defect.py`, `a_func_breakdown.py` | `application.*` 结构化统计 |
| 担当者 | `p_person_scale.py`, `p_review_detect.py`, `p_design_inject.py`, `p_mfg_inject.py`, `p_bypass.py` | `person.*` 结构化统计 |
| Ticket 汇总 | `step0a_app_tickets.py`, `stepA_all_tickets.py` | `aggregate.ticket_*` |
| 评审/单测汇总 | `r_basic_stats.py`, `r_cross_tab.py` | `aggregate.review_test_*` |

迁移初期可以使用兼容 wrapper 输出 JSON，但发布前必须消除固定路径和直接凭据访问。不能长期依赖 Agent 解析脚本的自由文本 stdout。

## 6. CapabilityBlueprint 预期能力

JAF 验收首先只解释 `jaf.ticket.analyze`。Interpreter 应识别 Ticket 分析能力、资源前提、规则、交付物和效果意图，而不是复制基础闭环的固定业务 Schema；其余 5 个能力在单 Ticket 闭环通过后扩展。

### 6.1 `jaf.ticket.analyze`

资源前提包括 Ticket（Redmine/CSV）、可选 repository（SVN/Git/project files）、项目知识和用户指定的报告语言。Ticket ID、是否验证设计书等可以作为动态参数或运行中澄清，不要求平台预定义字段。

Agent guidance：

1. 读取 Ticket 并转换 Redmine 字段标签。
2. 检查详细内容和模板空值。
3. 按用户选择读取代码、历史和设计书。
4. 查询规模列表、字段定义和历史模式。
5. 执行 10 项字段整合性检查。
6. 执行 12 项基于事实的妥当性检查。
7. 生成缺失字段建议、两案或“证据不足”。
8. 校验 Evidence、限制和 Skill 的质量标准。
9. 生成报告 Artifact；若用户希望更新 Redmine，先生成 ChangeProposal，不直接回写。

### 6.2 其他任务（后续产品范围）

| capability | 输入 | 输出重点 |
| --- | --- | --- |
| `jaf.function.analyze` | 功能 ID 或功能名 | 规模、评审、单测、密度、质量目标、逃逸和改善 |
| `jaf.application.analyze` | 一个或多个应用 | 应用趋势、热点功能、原因结构和改善 |
| `jaf.person.analyze` | 担当者或应用 | 负责规模、检出、作り込み密度、单测流出和排名 |
| `jaf.ticket.aggregate` | 应用范围或全量 | 应用交叉、原因工程交叉、排名和横向展开 |
| `jaf.review_test.aggregate` | 应用范围 | 评审/单测分布、密度、逃逸率和功能排名 |

担当者分析是未启用的后续范围。若启动该工作包，必须先定义成员访问范围、查看审计和人工解释要求；不能把 Project 鉴权当作专门的人员分析审计已经存在。

## 7. 数据源与 Tool 绑定

| 资源 | 当前路径 | 验收关注 |
| --- | --- | --- |
| Ticket | issue.read + Redmine/CSV | 同一事实快照与字段语义 |
| repository | repository.read 或冻结树 + workspace.search/read | Git/SVN revision、scope 和 Evidence |
| 字段映射/规模/目标/应用资料 | document.read 或 Project 文档物化 | 文档 ID/hash、匹配位置、读不了的限制 |
| 报告 | Outcome/Artifact 与 workspace.write | 原始结果不变、证据可追溯 |
| 外部写入 | ChangeProposal + issue.update/repository.write | 精确批准、前置版本、幂等与 read-back |

Provider 来自平台注册与 Project 配置，不由 Skill 任意创建。document 冻结正处于联调阶段，旧部署还可能读取 Project 全集；无论使用哪一版本，专项 Project 都只放合法执行输入，Gold/Rubric/expected 保持独立隔离。实际范围必须依据[资源快照验收](../design/resource-snapshots.md#验收条件)验证，不能仅靠 UI 已勾选一份文档判断隔离成立。

## 8. JAF 结果期待（非平台业务 Schema）

JAF Skill 应在自身 guidance 和评价 Rubric 中要求：

- 对可确认的 Ticket 字段给出原值判断、建议、理由、置信度和 Evidence。
- 执行 Skill 声明的整合性/妥当性检查，并明确未执行或证据不足的项目。
- 对设计书、修正前后代码无法确认的内容标记需要确认，必要时给出两案。
- 生成可读报告；若需要 Redmine 更新，单独生成字段 ChangeProposal。评论追加不纳入已实现的字段更新契约。
- 关键结论没有 Evidence 时明确 abstain，不把推测表示为事实。

这些是 JAF Skill 的业务质量要求和 benchmark 评价项，不进入平台 `contracts/`，也不要求所有 JAF
版本使用固定字段名或嵌套结构。若某个版本需要机器消费，可以在该 SkillVersion 中附加动态结果
Schema；无 Schema 时仍以通用 OutcomeEnvelope、Artifact、Evidence 和 Proposal 完成验证。

## 9. ViewSpec

以下是业务呈现要求，不是当前 ViewSpec 组件枚举。使用现有通用 Outcome/结构化数据组件表达，缺少特定图表时不得把目标 renderer 当成已实现功能。JAF 的 standard profile 建议包含：

- Ticket 分析输入，以及 Redmine/CSV、SVN/Git source-picker；基础闭环 fixture 固定为 CSV/Git。
- 字段级原值、建议值、判定、置信度和 Evidence。
- 10 项整合性检查和 12 项妥当性检查表。
- 设计书确认失败时的两案比较卡片。
- 密度、目标值、热点、排名和逃逸图表。
- RunSegment/Session、用户 Review、原始 Outcome、报告 Artifact、ChangeProposal 和 Evaluation。

JAF 准确度评价使用平台标准视图，避免把分析准确度与生成前端质量混在一起。ViewSpec 是可选契约；不要求为了验收单独生成一份 ViewSpec，更不要求尚未实现的专用图表 renderer。

## 10. Benchmark v1

### 10.1 样本数量

在独立评价存储的 `evaluation/benchmark-v1.yaml` 固定定义 30 个历史 Ticket。这里规定目标文件格式和规模，不表示仓库已经提供真实 case 或平台已实现 EvaluationSuite API。该规模用于 MVP 人工复核和回归，不代表统计学上的生产质量证明。

| 分层 | 数量 | 目的 |
| --- | ---: | --- |
| 已确认的设计工程 Bug | 8 | 验证设计书、原因工程和设计类原因 |
| 已确认的制造工程 Bug | 8 | 验证代码 Diff、实现类原因和修正程序 |
| 跨工程、环境或复杂 Bug | 4 | 验证逃逸和复合原因 |
| 仕变、非 Bug、重复或其他分类 | 4 | 避免把所有问题判为 Bug |
| 信息不完整、模板空值或证据不足 | 6 | 验证 unknown、两案和追问 |
| 合计 | 30 | 固定回归集 |

覆盖要求可以跨分层重叠：至少 5 个应用、10 个代码 Evidence、8 个设计书 Evidence、10 个未填写/模板空字段、8 个已填写但事实不一致字段，以及 10 个 Redmine/CSV 配对 case。

### 10.2 选取和冻结

1. Skill 声明来源截止时间、允许状态和 tracker。
2. 按分层和覆盖条件选取候选，不向模型提供 Gold。
3. 固定 Ticket ID、Redmine updated_on、CSV hash、仓库 revision/commit 和 Knowledge hash。
4. 两名 JAF 质量人员独立标注，分歧由第三人或负责人裁定。
5. 冻结为 benchmark v1；替换 case 必须说明原因并提升 benchmark 版本。

评价集不能每次随机抽样，否则版本间不可比较。可以另建探索集，但不计入正式准确度。

### 10.3 Case 定义

以下为评价侧文件的语义示例，不是 Run 创建请求或已发布 JSON Schema；省略号须换成真实冻结值。只将执行所需 input/source 投影给 Run，不发送 expected_ref 或 Gold 正文。

```yaml
case_id: jaf-ticket-001
ticket_ref: internal-reference
tags: [design, missing-fields, has-source, has-design-doc]
source_snapshot:
  issue_hash: sha256:...
  repository_revision: "..."
  knowledge_hashes: {}
input:
  task: jaf.ticket.analyze
  repository_required: true
expected_ref: expected/jaf-ticket-001.json
```

benchmark 不保存凭据。Ticket 内容保存为受控 fixture/Artifact，并遵循 Project retention policy。

## 11. Gold 与人工 Rubric

### 11.1 结构化 Gold

每个 case 标注：关键质量字段的原值判定、建议值和允许集合；10 项整合性检查；12 项妥当性检查；必须引用的证据类型；应进入两案、证据不足或人工确认的条件。

允许多种合理文本的字段使用结构化要点和人工 Rubric，不做单一字符串 exact match。

### 11.2 文本 Rubric

原因内容、根本原因、对应内容和改善建议按 1–5 分评价：

| 维度 | 5 分要求 |
| --- | --- |
| 事实一致性 | 与 Ticket、设计书、代码和 Diff 无矛盾 |
| 完整性 | 覆盖直接原因、工程见落和根本原因 |
| 证据性 | 关键断言均引用有效 Evidence |
| 分类一致性 | 文本与原因工程、Bug 原因和原因区分 AI 一致 |
| 可执行性 | 建议具体且不越权 |

捏造文件、代码、设计书或 Evidence 时，该 case 标记 `hallucination=true`，不能被其他高分抵消。

## 12. 准确度指标与目标

### 12.1 指标

| 指标 | 算法 |
| --- | --- |
| 分类字段准确率 | 与 Gold 值或允许集合 exact match |
| 分类字段 Macro F1 | 各类别 F1 的平均 |
| 担当者 Set F1 | 设计/制造担当者集合与 Gold 对比 |
| 不整合检出 Precision/Recall | 检出矛盾与 Gold 矛盾集合对比 |
| Evidence coverage | 有效 Evidence 支撑的关键字段数 / 关键字段数 |
| Unknown correctness | 应证据不足时正确 abstain 的比例 |
| 文本 Rubric | 两名评价者平均分和分歧 |
| Hallucination rate | 存在无来源事实或伪造 Evidence 的 case 比例 |
| 人工修订率 | 含 revision_json 的 Result 比例 |

同时记录完成率、Schema 通过率、Tool 成功率、自动允许/硬拒绝次数、耗时、Token、成本、Provider 配对一致率和恢复成功率。

计分口径必须在运行前随 benchmark/rubric 版本冻结：关键字段集合、权重、适用 case、允许值、正类和未知值处理均不可按模型输出临时改变。报告同时给出分子/分母，不只给百分比。

- 30 个 case 全部进入运行完成率和失败统计。缺失答案在适用字段准确率中不算正确；不得删除失败 Run 后只报告成功子集。
- 不适用字段由 Gold 预先标记，排除数量和理由单列。模型自己说“不适用”不能改变分母。
- Evidence ID 有效只证明引用可访问，不证明证据支持结论；coverage 还须人工或受控规则判定支撑关系。
- Macro F1 的类别集合、加权准确率的字段权重在 rubric 中固定；样本为零或分母为零时报告 N/A 及原因，不能写成 100%。
- `hallucination=true` 的 case 数除以完整 30 case 得到 hallucination rate，并同时展示失败/未完成数量。按 ≤5% 的目标，30 case 中最多允许 1 个此类 case；不能用四舍五入掩盖 2/30 超标。

### 12.2 硬性技术门禁

- Secret 扫描问题为 0。
- 已产生结果的通用 Outcome 及已声明的可选 Schema 通过率 100%；失败/无 Result 的 case 另列，不能被算作 Schema 已通过。
- 越权 Tool 实际执行数为 0。
- Evidence ID 有效率 100%。
- 原始 Result 被人工覆盖数为 0，Gold/Rubric/expected 被 Agent 读取数为 0。
- 30 个 case 均产生可审计终态；失败 case 也有完整错误记录。

### 12.3 首版质量目标

- 原因工程准确率 ≥ 80%。
- 关键分类字段加权准确率 ≥ 80%。
- 不整合检出 Precision ≥ 85%，Recall ≥ 75%。
- 关键字段 Evidence coverage ≥ 90%。
- 事实一致性人工平均分 ≥ 4.0/5。
- Hallucination rate ≤ 5%。
- 应证据不足的 case 中正确 abstain ≥ 80%。

质量目标用于报告和人工发布判断。未达到时不修改历史结果，应调整 Skill/Interpreter、发布新版本并重跑。

## 13. 准确度报告

每个正式 EvaluationRun 生成：

```text
JAF_Accuracy_Report_<skill-version>_<benchmark-version>_<YYYYMMDD>.md
```

报告包含版本与配置、30 case 分层、技术门禁、字段 Accuracy/Macro F1、矛盾检出、Evidence、人工 Rubric、Provider 差异、失败和 Hallucination、版本变化，以及人工“通过/条件通过/不通过”结论。

报告只显示脱敏 case ID；Ticket 详情通过权限控制的 Evidence 页面查看。

## 14. 端到端执行顺序

### 14.1 导入前

1. 清理并轮换凭据。
2. 建立 JAF Project、成员、Integration 和 SecretReference。
3. 上传字段映射、应用注册、CSV Schema、规模列表、质量目标和知识库。
4. 导入评审、单测和 issues.csv 快照并记录 hash。
5. 建立 Redmine 和 SVN 只读连接测试。

### 14.2 Skill 导入和发布

1. DirectorySkillAdapter 导入 `skills/examples/jaf-ticket-quality/SKILL.md` 或清理后的完整 SkillSource；
   来源不携带 ProjectMind 业务 Schema。
2. Interpreter 生成 Ticket CapabilityBlueprint、resource requirements、guidance、deliverables、effect intents 与 source trace；需要稳定参数时可选生成 TaskContractDraft。
3. 运行 Blueprint、Tool conformance、permission 和可选动态 Schema/ViewSpec 校验。
4. 用户按 Preview 调整后，ADMIN 发布新的 Adapted SkillVersion，Project 显式启用该精确版本，再绑定 issue/repository/document 资源。
5. 通过通用 TaskCatalog 与 `/task-runs` 创建 Run，并用历史 fixture 比较行为；历史 Native seed 只读，
   不再参与 task discovery 或新 Run 创建。

### 14.3 Smoke 与 benchmark

1. 使用一个非 benchmark Ticket 执行 Redmine + SVN smoke。
2. 验证只读 Tool 自动执行、硬拒绝、AgentTaskBrief、用户 Review、多 Session、ChangeProposal、Result、Evidence、Artifact、Evaluation 和导出；真实 effect apply 仅在 CAS adapter 受入环境验证。
3. 使用同一 Ticket 的 CSV 快照执行并比较。
4. Smoke 通过后在独立评价存储创建 EvaluationRun 记录，固定全部版本和配置；当前没有对应平台管理 API。
5. 30 case 每个创建独立 Run，不共享 Agent Session。
6. 自动计算结构化指标，两名评价者完成人工 Rubric 和 revision。
7. 分歧裁定后冻结 EvaluationRun，生成准确度报告。

## 15. 验收门禁

| 验收层次 | 通过条件 |
| --- | --- |
| Security | 无明文凭据，旧凭据已轮换，Integration 最小只读权限 |
| Capability | 能力、目标、资源、guidance、deliverable、effect intent 和 source trace 可自动校验 |
| 基础执行闭环 | CSV/Git fixture、自动执行、硬拒绝、Evidence、Result 和 Evaluation 成功 |
| Import | 导入选定来源版本的完整、安全文件清单与 hash；附件数量按该版本记录，不把调研时的 20 个脚本当成永远固定的门槛 |
| Interpret | 在基础闭环上识别等价 Ticket 任务，无 JAF 平台硬编码，diagnostic 可解释 |
| Contract | 通用 Blueprint/Outcome/Tool 协议与可选动态 Schema/ViewSpec 通过 |
| Runtime | Redmine/CSV、SVN、自动执行/硬拒绝、Evidence 和报告成功 |
| Workspace | 平台标准视图完成全流程，Evaluation 不覆盖 Result；声明 ViewSpec 时另验契约 |
| Benchmark | 30 case 全部有终态和审计，指标及人工评价完整 |
| Release | 技术硬门禁全部通过，质量负责人记录发布结论 |

FrontendModule、调度和其余 5 类任务不属于本 JAF benchmark 的准确度比较范围。Git Provider 使用通用 repository Tool fixture 做契约测试；除非 JAF Project 提供 SVN 等价 Git 镜像，否则不纳入 30 Ticket 准确度比较。仓库访问与内容冻结独立按[资源验收](../design/resource-snapshots.md#验收条件)检查，客户端存在不等于真实连接验收通过。

## 16. 失败处理

- 数据缺失：标记 unavailable/insufficient，不从未授权来源补齐。
- Redmine 失败：不覆盖已有 CSV；用户选择是否用冻结快照创建新 Run。
- SVN/设计书不可访问：输出两案和未验证状态。
- 未来注册的受控计算失败：保存输入 hash、脱敏诊断和 exit code，不让 Agent 编造统计值。当前无通用来源脚本 runner，不把它作为可直接调用的失败恢复路径。
- 通用 Outcome 或已声明的可选 Schema 失败：Run FAILED，保留脱敏诊断和最后 Artifact 引用。
- 人工分歧：保留两份 Evaluation，裁定另追加。
- Skill 调整：发布新版本，用同一 benchmark 新建 EvaluationRun，不修改旧报告。

## 17. 评价数据模型（目标）

Evaluation 已实现；EvaluationSuite/Case/Run/RunCase 是 benchmark 管理的目标对象，当前没有对应平台表/API。正式 benchmark 先以隔离文件和评价记录组织，不把本节当作已发布数据库设计。

| 对象 | 作用 |
| --- | --- |
| `EvaluationSuite` | benchmark 名称、版本、选择策略、Rubric、来源截止和 checksum |
| `EvaluationCase` | case 输入、标签、来源快照和 Gold 引用 |
| `EvaluationRun` | 固定 Skill/Interpreter/Engine/Model 版本并汇总一次评价 |
| `EvaluationRunCase` | 关联 EvaluationCase、实际 Run、自动指标和 case 结论 |
| `Evaluation` | 评价者对实际 Result 的评分、comment 和 revision |

已发布 Suite、Case 和完成后的 EvaluationRun 不可原地修改。

## 18. 实现顺序

当前状态统一维护在[计划 §13](../planning/roadmap.md#13-当前执行状态)，本节只定义验收依赖顺序，不维护完成清单：

1. **通用基础回归**：使用不含 Gold 的 CSV/Git 输入验证执行、证据和评价；旧 Native seed 只解释历史，不重建 Ticket 平台业务契约。
2. **能力蓝图回归项**：通过通用 Interpreter 导入 JAF 目录 Skill，对比能力、资源、guidance、交付物、效果、Tool 和 source trace；差异必须显式记录，内置 seed 已退役。
3. **交互式 Runtime 门禁**：同一流程还必须通过 repository-review、development-readiness、用户 Review 与外部效果 fixture，JAF 不能是唯一测试输入。
4. **发布验收/后续独立工作包**：在真实 PostgreSQL/Compose 联调 Redmine CAS adapter；Git/SVN 与 workspace.write 已有实现，需要真实系统专项验证；sandbox command 仍未开放。建立 benchmark-v1、rubric-v1 和 30 个隔离 Gold case。首个 write Provider 的本地实现不等于真实系统验收。
5. **继续暂缓**：JAF 其余脚本和 5 类任务只在通用 Skills 主线完成、单 Ticket 回归稳定且产品计划显式更新后启动。
