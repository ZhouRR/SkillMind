# JAF Benchmark：样本、评分与质量判断

> 定位：单 Ticket 分析的评价方法正本。迁移和运行前提见 [JAF 端到端验收](jaf-quality.md)，当前进度见[计划 R12](../planning/roadmap.md#r12-业务质量验收)。本文规定评价侧的目标文件与人工流程，不表示平台已有 benchmark 管理 API、真实样本或自动评分服务。

## 先看一次评价的边界

一次评价回答“这个精确 Skill 版本在固定输入上是否达到质量目标”。它不把一次 Run 成功、Schema 通过或页面可读直接换算成业务准确率。

```text
评价负责人冻结样本与评分规则
          ↓
仅把合法执行输入交给 Run
          ↓
保存原始 Result 与 Evidence
          ↓
评价侧对照 Gold，追加人工评价
          ↓
保留失败分母，生成发布判断
```

执行侧不知道答案；评价侧可以读取执行结果，不能反过来把答案送入 Agent。调整 Skill 后另建一轮评价，不覆盖原始结果或挑选重跑中最好的一次。

### 哪些数据交给谁

| 位置 | 内容与边界 |
| --- | --- |
| Skill package | 业务规则、能力要求、交付标准；不包含正式 case 清单、Gold、expected_ref 或评价者答案 |
| Project / Run 输入 | 获准的 Ticket/CSV、仓库版本、字段映射与设计资料；每次记录实际 ID/hash/scope，不因是验收就放宽授权 |
| 独立评价存储 | benchmark、Rubric、Gold、case 与 Run 的对应记录、评分和报告；不得导入为 Skill 附件或挂载给 Agent |
| 平台 Evaluation | 对某个实际 Result 追加评分、comment、revision；不覆盖 Result，也不等于整套 benchmark 管理服务 |

目录布局见[迁移目标目录](jaf-quality.md#3-迁移目标目录)。仅把目录命名为 evaluation 不构成隔离：必须核对该 Run 的文档清单、绑定、workspace 和 Tool 可读范围。

## 固定样本与版本

### 样本数量

在独立评价存储的 `evaluation/benchmark-v1.yaml` 定义 **30 个历史 Ticket case**。这是 MVP 人工复核和版本回归集，不是生产质量的统计学保证。

| 分层 | 数量 | 检查重点 |
| --- | ---: | --- |
| 已确认的设计工程 Bug | 8 | 设计书、原因工程、设计类原因 |
| 已确认的制造工程 Bug | 8 | 代码 Diff、实现类原因、修正程序 |
| 跨工程、环境或复杂 Bug | 4 | 逃逸和复合原因 |
| 仕变、非 Bug、重复或其他分类 | 4 | 避免全部判为 Bug |
| 信息不完整、模板空值或证据不足 | 6 | unknown、两案、追问 |
| 合计 | 30 | 固定回归集 |

覆盖条件允许重叠：至少 5 个应用、10 个代码 Evidence、8 个设计书 Evidence、10 个未填写/模板空字段、8 个已填写但事实不一致字段，以及 10 个 Redmine/CSV 配对 case。

### 选取与冻结

1. 评价负责人在 benchmark 中固定来源截止时间、允许状态、tracker 和抽样条件；这些不是供模型选择有利样本的 Skill 指令。
2. 按分层与覆盖条件选取候选。评价者为 Gold 新增的裁定说明不能作为普通设计资料送入 Agent；但不能为隐藏答案而擅自删除原 Ticket 中本就要核查的字段。保留合法原始输入，隔离额外评价答案。
3. 固定 Ticket ID、Redmine updated_on、CSV hash、仓库 revision/commit、Project 文档 ID/hash，以及实际允许读取的范围。
4. 两名 JAF 质量人员独立标注，分歧由第三人或负责人裁定。
5. 冻结 benchmark 与 Rubric 版本。替换 case、修改允许答案或评分权重必须说明原因并提升对应版本；不原地修订已使用的文件。

不每轮重新随机抽样。探索集可另建，但不混入正式准确度；正式评价期间的追问、人工提供的新事实和运行中断也要保存，不能悄悄补输入后仍称为相同条件。

### Case 定义

以下是评价侧文件的语义示例，不是 Run 创建请求或已发布 JSON Schema。省略号必须替换成实际冻结值，字段名也不能作为已存在 API 的依据。

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

只向 Run 投影执行必需的 input/source，不发送整个 case 文件、expected_ref 或 Gold。benchmark 不保存凭据；真实 Ticket 内容放在受控输入/Artifact 中并执行保留策略，仓库文档和截图只用脱敏 ID。

### 配对与重跑

Redmine/CSV 配对比较的是**同一事实快照经两种读取路径后的结果**，不是“相同 Ticket ID 在两个时间点的查询”。核对字段映射、附件、评论/历史等实际输入是否等价；无法确认时记录不一致原因，不把差异全部归因于模型。

每轮先固定主质量评分所使用的 Provider；至少 10 个配对 case 的另一条路径作为独立对照，不能把 30 个 case 加上额外配对 Run 当成新的准确率分母。Provider 配对一致率单列适用 pair 数和差异，配对失败也保留。

每个 case 的新执行使用独立 Run/Agent Session；同一 Run 的技术 Attempt 恢复不是新增评价样本。人工重跑产生新的记录，保留原失败并写明原因，不从多次输出中择优覆盖原记录。跨版本比较固定 benchmark/Rubric、输入和可比运行条件，并记录 Skill、Interpreter、Engine/Model、Provider 与配置的变化。

## Gold 与人工评分

### 结构化 Gold

每个 case 标注关键字段的原值判定、建议值和允许集合，10 项整合性检查、12 项妥当性检查，必须引用的证据类型，以及应进入两案、证据不足或人工确认的条件。

允许多种合理文本的字段使用结构化要点与人工 Rubric，不要求单一字符串完全相同。不适用字段及理由必须预先标注，不能由模型自己的“不适用”改变分母。

### 文本 Rubric

原因内容、根本原因、对应内容和改善建议按 1–5 分评价：

| 维度 | 5 分要求 |
| --- | --- |
| 事实一致性 | 与 Ticket、设计书、代码和 Diff 无矛盾 |
| 完整性 | 覆盖直接原因、工程见落和根本原因 |
| 证据性 | 关键断言均引用有效 Evidence |
| 分类一致性 | 文本与原因工程、Bug 原因和原因区分 AI 一致 |
| 可执行性 | 建议具体且不越权 |

保留两名评价者的独立分数与分歧，裁定另追加。捏造文件、代码、设计书或 Evidence 的 case 标记 `hallucination=true`，不能用其他维度高分抵消。

## 指标与通过条件

### 指标口径

| 指标 | 计算与解释 |
| --- | --- |
| 分类字段准确率 | 与 Gold 值或允许集合匹配的适用字段数 / 全部适用字段数 |
| 分类字段 Macro F1 | 在 Rubric 固定的类别集合上计算各类 F1，再取平均 |
| 担当者 Set F1 | 设计/制造担当者集合与 Gold 对比；沿用单 Ticket 字段评价，不扩展为人员分析任务 |
| 不整合检出 Precision/Recall | 检出的矛盾与 Gold 矛盾集合对比，分别报告分母 |
| Evidence coverage | 有有效证据支撑的关键字段数 / 全部适用关键字段数；引用可访问不等于内容支持结论 |
| Unknown correctness | 应证据不足时正确 abstain 的数量 / 全部应 abstain 的适用项 |
| 文本 Rubric | 两名评价者的均值、有效评分数量与分歧；失败/未评分另列 |
| Hallucination rate | 标记 hallucination 的 case 数 / 完整 30 case，同时展示失败与未完成数量 |
| 人工修订率 | 至少有一项人工修订的 Result 数 / 被纳入评价的 Result 数，同时列出无 Result 的 case |

关键字段、权重、允许值、适用项、类别集合、未知值和零分母处理在运行前随 Rubric 冻结。报告给出分子/分母，不能只有百分比；N/A 必须说明原因，不算 100%。

30 个 case 全部进入完成率和失败统计。失败或缺失答案在适用字段准确率中不算正确，不能删掉失败 Run 后只报告成功子集。另记录 Schema 通过率、Tool 成功率、自动允许/硬拒绝次数、耗时、Token/成本、Provider 配对一致率和恢复成功率。用量缺失标记未知，不以局部 limit 当成实际消费；可信口径见[Run 预算](../design/run-budgets.md#计量报告如何归一化)。

### 一个例子：失败也留在分母中

30 个 case 的某个分类字段全部适用，27 个产生可评分答案，其中 24 个正确、3 个错误，另 3 个失败。该字段准确率为 **24/30 = 80%**，不是 24/27。完成率和失败原因另列，不能因准确率达到目标就隐藏失败。

若 2 个 case 含捏造事实，hallucination rate 为 **2/30 ≈ 6.67%**，超过 ≤5% 的目标；30 case 中最多允许 1 个此类 case。没有输出的 case 不能证明“未捏造”，必须与这个比例一起展示，避免把全部失败误读为零风险。

### 硬性技术门禁

- Secret 扫描问题为 0，越权 Tool 实际执行数为 0。
- 已产生 Result 的通用 Outcome 及已声明的可选 Schema 通过率 100%；失败/无 Result 不计为 Schema 通过。
- Evidence ID 有效率 100%；支撑关系另按 coverage 评价。
- 原始 Result 被人工覆盖数为 0，Gold/Rubric/expected 被 Agent 读取数为 0。
- 30 个 case 均产生可审计终态，失败 case 也保留完整错误记录。

技术门禁用于判定数据与执行是否可信，不代表模型答案正确；安全隔离失败不能由质量均分或“条件通过”豁免。

### 首版质量目标

| 质量项 | 目标 |
| --- | ---: |
| 原因工程准确率 | ≥80% |
| 关键分类字段加权准确率 | ≥80% |
| 不整合检出 Precision / Recall | ≥85% / ≥75% |
| 关键字段 Evidence coverage | ≥90% |
| 事实一致性人工平均分 | ≥4.0/5 |
| Hallucination rate | ≤5% |
| 应证据不足时正确 abstain | ≥80% |

目标用于报告和人工发布判断，本次拆分不调整原阈值。未达到时调整 Skill/Interpreter、发布新版本并另建评价；不修改历史答案、分母或报告取得通过。缺少适用评分或人工判定时明确写“未完成”，不推导总体通过。

## 评价记录与报告

### 记录对象与当前载体

平台 Evaluation 已有 API、持久化和表单基础；[结果未知与界面边界](../design/results-evaluation.md#提交未知与界面责任)尚待补齐，不据此宣称整条链已验收。其余对象是评价管理的目标概念，当前没有对应平台表/API。正式 benchmark 可先用受控的隔离文件与人工记录组织，不因此要求先开发一套管理服务。

| 对象 | 责任 |
| --- | --- |
| EvaluationSuite | benchmark 名称/版本、选择策略、Rubric、来源截止与 checksum |
| EvaluationCase | case 输入、标签、来源快照和 Gold 引用 |
| EvaluationRun | 固定 Skill/Interpreter/Engine/Model 等版本并汇总一轮评价 |
| EvaluationRunCase | 关联 case、实际 Run、自动指标、人工结论与重跑记录 |
| Evaluation | 对实际 Result 追加评价者评分、comment、revision |

已发布 Suite、Case 和完成后的 EvaluationRun 不可原地修改。评价记录必须能从脱敏 case ID 找到实际 Run、Result/Evidence 和评分依据，不能只保存汇总百分比。

### 平台 Evaluation 与发布结论

[现有 Evaluation 请求](../../PJM/contracts/evaluations/v1/create-request.schema.json)只接受一个 rating、verdict、comment 和 revisions；[route](../../PJM/backend/src/projectmind/api/routes/evaluations.py)与 [service](../../PJM/backend/src/projectmind/evaluations/service.py)把评价追加到该 Project/Run 的 Result。它不是五维 Rubric 的专用录入表，也没有自动汇总整轮评分的接口。

原值定位、重复评价与提交恢复由[结果设计](../design/results-evaluation.md#评价请求与历史)统一负责；本页不另定义一套平台写入协议。尤其不能把重发产生的两条 Evaluation 当作两个独立 case，或因此扩大评分分母。

| 要记录什么 | 使用哪里 |
| --- | --- |
| 对一个结果的总体准确性与修改建议 | 平台 Evaluation。verdict 为 accurate / partially_accurate / inaccurate / uncertain；revisions 指向 Result 中实际存在的位置 |
| 两名评价者的逐维分数、Gold 对照与裁定 | 隔离评价存储；关联实际 Run/Result 与可选 Evaluation ID，不把正式答案复制进普通 Project 可见的 comment |
| 失败且没有 Result 的 case | 评价侧 case 记录，保留 Run 终态与诊断。不能伪造 Result 或创建空 Evaluation 来凑齐数量 |
| 整轮通过、条件通过、不通过或未完成 | 评价报告与负责人判断。不是平台 EvaluationVerdict 的新枚举，也不直接改变 SkillVersion 的发布状态 |

如果后续需要多维评分 UI 或 benchmark API，应另设计授权、版本、存储和契约；不能把结构化评分表塞进 comment 后声称平台已经支持。人工判断也不授予外部 write 权限，仍走独立 Proposal/批准流程。

### 报告内容

每轮使用 `JAF_Accuracy_Report_<skill-version>_<benchmark-version>_<YYYYMMDD>.md` 命名；同日多轮另用唯一记录 ID 区分，不能按同名文件覆盖。

报告按以下顺序阅读：

1. **判断与限制**：通过/条件通过/不通过或尚未完成，负责人、范围、未覆盖项；先列安全和技术门禁。
2. **可复现条件**：评价记录 ID、完整版本/配置、30 case 分层和输入冻结依据。
3. **运行事实**：所有 case 对应的 Run、终态、重跑/失败与未完成情况。
4. **质量结果**：Accuracy/Macro F1、矛盾检出、Evidence、人工 Rubric 和 hallucination，均保留分子/分母。
5. **差异与行动**：Provider 对照、前后版本变化、人工分歧、待修正项和下一轮条件。

报告只显示脱敏 case ID；Ticket 详情在授权的 Evidence/评价存储中查阅。不把 Gold、真实票据或凭据复制到项目文档浏览版。

## 从这里继续

准备资源、导入 Skill、运行 smoke 和创建评价，回到[端到端执行顺序](jaf-quality.md#14-端到端执行顺序)；功能验收与本页评分分别举证。FrontendModule、调度及 JAF 其余五类任务不进入本次单 Ticket 准确率比较。

评价工具的后续开发先验证隔离、版本与不可变记录，再验证评分分母和完整失败统计。本文未授权调用真实模型、连接生产系统或写入外部资源；所需账号、数据和专用目标必须在具体验收任务中确认。
