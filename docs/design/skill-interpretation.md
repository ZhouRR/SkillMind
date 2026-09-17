# Skill 解释与发布实现

本页是解释、调整和发布的实现正本；语义约束见 [Skill 契约](skill-contract.md)，执行见 [Runtime](agent-runtime.md)，当前范围见 [计划](../planning/roadmap.md)。字段以 [Candidate v2](../../SKM/contracts/skills/interpreter/v2/candidate.schema.json) 和对应契约为准，不在文档复制字段全集。

## 目标与主流程

```text
文件 → 确定性解析/静态分析 → 不可变 SkillSource
  → 冻结来源、模型配置和能力目录 → 最小模型候选
  → 确定性编译/校验 → 预览或追加调整 → SkillVersion DRAFT
  → ADMIN 发布 → Project 启用精确版 → 任务发现与资源绑定
```

导入解析不调用模型；静态 Draft 不等于可发布任务。远程 Git、压缩包导入、生成模块和 TaskFlow 执行不因这条链路存在而自动支持。

## 信任边界

Skill 原文保留业务规则，但不是权限。来源命令、附件和外部资源不能覆盖平台规则、执行任意脚本或访问凭据。静态凭据发现仍在模型调用前拒绝。解释器不读取 Project Secret、不建立 Run、不自动发布；缺少工具时报告限制，不能弱化业务要求。

## 代码责任

| 入口 | 唯一责任 |
| --- | --- |
| [importer](../../SKM/backend/src/skillmind/skills/importer.py) | 文件归一化、来源索引与静态 Draft |
| [source_projection](../../SKM/backend/src/skillmind/skills/source_projection.py) | 全文输入投影、来源 ID/行号解析、候选可选 null 归一化 |
| [model_interpreter](../../SKM/backend/src/skillmind/skills/model_interpreter.py) | 冻结身份与模型传输，不承担发布 |
| [direct_candidate](../../SKM/backend/src/skillmind/skills/direct_candidate.py) / [task_contract](../../SKM/backend/src/skillmind/skills/task_contract.py) | 最小候选和输入契约的确定性编译 |
| [design_validation](../../SKM/backend/src/skillmind/skills/design_validation.py) / [manifest_gate](../../SKM/backend/src/skillmind/skills/manifest_gate.py) | 预览/发布共用来源、工具、契约和 warning 校验 |
| [service](../../SKM/backend/src/skillmind/skills/service.py) / [repository](../../SKM/backend/src/skillmind/skills/repository.py) | 用例协调、事务、版本和可见性 |
| [request_service](../../SKM/backend/src/skillmind/skills/request_service.py) | 持久请求、调用许可、成果采用与未知状态 |

系统 [Interpreter Skill](../../SKM/skills/skillmind-skill-interpreter/SKILL.md) 是执行资产，不随文档整理搬迁。修改它时同步版本、来源 hash、prompt identity、示例及测试。

### 调用输入根类型

当前模型只生成标题/说明、调用输入、资源及其操作、任务内工具和诊断；不生成第二份业务执行计划。平台编译一个 `execute` 任务和固定外壳，拥有身份、checksum、批准默认值及 OutcomeEnvelope。

输入根必须为 object，与 Run API 一致；无用户输入使用空对象。嵌套类型、枚举和业务值不为表单裁剪；TaskContractDraft 仍限制最多五层，根及标量叶节点都计层。资源的能力列表必须为非空数组，生成 Schema 与执行编译器使用同一要求，不让模型以 null 表示必需能力。

### 原文保留

冻结请求独立保存全部文本的 path、原始内容和 SHA-256，并与文件索引核对；模型输入仅发送一次带行号的完整来源，二进制只列索引。模型不得重新复制原文来替代平台快照。

`source_ref` 使用来源 ID 和可选行号，例如 `s0:12`。共享解析器按准确候选字段定位错误；未知 ID、非法行及二进制行引用进入原有的一次修复，不猜测或公开输入值。来源 trace 的 target 必须指向声明中的现有位置。

发布再次核对原文、索引及来源关系；运行读取冻结文本，不从当前文件补写。保留原文和结构校验都不证明模型已完整理解业务语义。

### 追加调整与修复

候选经 Schema、输入编译、声明/来源校验和同一 ManifestValidator 全部通过后才为 PREVIEW_READY。项目绑定和外部连通性不属于模型解释。无效 JSON 等生成错误最多重生成一次；provider、超时、连续空白故障不因此自动重跑。

有可定位候选时，修复同时携带原候选和脱敏诊断；准确定位到 component 的错误只允许改变该 component。不能定位或汇总的发布错误不声称局部保护。失败不发布部分修复结果。

追加调整从保存的 Manifest 精确投影完整最小 `launch_contract` 和 checksum；它不是恢复模型历史原始输出。服务层可选 `editable_paths` 最多 32 条，以该声明为根，完整编译后检查增加、删除、类型和数组顺序变化。原声明不变；无显式范围仍依赖模型遵循修改指令。当前 HTTP/Web 尚未接入范围选择，不把自然语言“其他不变”描述为程序保证。

### 实际模型配置冻结

[RuntimeProfile](../../SKM/backend/src/skillmind/skills/runtime_profile.py) 绑定实际引擎、模型、effort、SDK/CLI、操作目录、fallback 和流水线版本。prompt identity 同时包含系统 Skill/Schema、实际提示和 profile；请求受理、Worker 及审计共用同一快照。操作目录在适配器构造时复制，不能调用时替换。

凭据不进入 profile；路由只保留非凭据配置摘要。不支持的非空 per-call 参数在模型调用前拒绝，不静默忽略或降档。流水线语义变化更新对应身份；不为升级重新计算已保存业务快照。

### 导入保存与上传授权

导入使用原 UserAccess；组织与导入者取自事务中核定的当前 ADMIN。解析在锁外进行，首次 await 前复制原文件和资格。保存锁序为 Organization → User SHARE → 原 AuthSession UPDATE；锁等待及最终 flush 后以新时间复核，Source/静态 Interpretation 原子保存。

HTTP 接收前验证 Origin/CSRF/ADMIN。保留上传相对路径；当前限制为 100 文件、单份 1,000,000 bytes、合计 5,000,000 bytes，额外正文/header 上限按共享上传 parser。实际计数，不信任 Content-Length。

相同来源 hash 复用原记录，不 PUT 修复旧 blob。新来源先验证全部存储 key，每次锁外 PUT 前后短事务复核原资格，并验证返回的 key/size/MIME/hash；全部成功后再保存元数据。此流程不是跨存储原子事务，取消不证明远端 PUT 停止，未知结果不自动重发或删除共享 key。

现有错误分类和 HTTP 状态保持不变：结构/限额、资格失效、权限及存储故障分别由共享边界处理。上传持久意图和精确清理的缺口见计划，不在每次局部优化中增加新业务流程。

### 异步解释的交接要求

API 将持久请求和 dispatch Outbox 一并提交，队列只携带请求 ID。原请求 ID 与内容 execution key 分离；相同内容复用原请求，显式重新生成才使用 nonce，不能换 ID 重启未知调用。

Worker 绑定原会话资格和一次性 owner；每次模型调用前取得许可、返回后记录、成果采用时再次核验。修复需要初次调用已返回且仍有执行资格。确认没有启动许可的准备失败可终结；已有许可而返回/停止不明时保留 UNKNOWN，不能补造 return、授予新调用许可或自动换模型。

受理路径为 `POST /skill-sources/{id}/interpretation-requests` 和 `POST /skill-interpretations/{id}/adjustment-requests`，请求包含非零 `request_id`。原 GET/SSE 核组织与当前会话；Pub/Sub 只作显示，终态以 DB 为准。停止查看不等于取消执行；断连/超时不自动生成新请求。

进度通知使用有界、短期待避的共享 Pub/Sub 发送器。Redis 不可用不阻塞模型或改变持久请求；丢失的最终通知由原状态查询恢复，不把此发送器用于 Outbox 或审批消息。

公开 `validation_attempts` 只含最多两条脱敏诊断，每条最多 4096 字符，不公开模型正文、自由约束值和内部 execution。Codex JSON 字符串外连续 4096 空白触发原 turn 中断与 CLI 清理，不修复重跑；本地退出不证明上游计费已停止。

### 从候选到项目任务的接线

PREVIEW_READY → 冻结 DRAFT → 发布校验/确认 warning → 精确项目启用 → 资源 readiness。首次发布锁定版本并核 Source、Interpretation、Manifest 的关系和原内容。重复发布重验权限但不改首次发布者、时间或 Manifest。来源变化创建新候选，不在发布时规范化原候选。

Manifest checksum 与内容执行复用键不是同一概念。错误 DRAFT 可供审查，但不能据此发布；发现可用任务也不证明外部连接当前可达。

### 版本管理的授权事务

草稿、发布、废弃、删除和启停使用原会话/CSRF。组织资产不额外要求项目成员；项目启停必须是同组织 ACTIVE Project。锁序及复核遵循 [用户事务](user-lifecycle.md#事务与并发)：Organization → User → AuthSession → 必要的 Project → SkillVersion/启停关系。

等待、读取引用和最终 flush 后重新验证资格；失效优先共享认证错误，越权和不存在统一处理。删除先检查 [全部引用](skill-contract.md#版本内容与可见性)；取消或提交未知不补偿、不自动重试。

## 过程重表达

来源中的 curl、git、搜索或转换需求仅映射到已有、授权的 versioned Tool；外部写入统一通过 change.propose 与批准/回读链。没有等价工具就报告限制，不生成自由 Shell、凭据或替代业务规则。报告使用通用结果；只有明确保存到 output/ 并获得提交回执的成果才是可下载 Artifact。

## ResourceBinding 与 readiness

任务直接使用原资源 key；同一连接、同一用途的多张表不重复定义资源。输入文档和输出文档库分别绑定。可选只读补充保持可选；写入资源必须声明操作并完成必需绑定，条件仍决定是否真正执行，不能由“资源已就绪”推导无条件写入。

空 allowlist 不授权。真实资源必须绑定 Integration 或明确 Project 文档，缺失不能退回模拟数据。readiness 只描述已安装能力与绑定情况；创建、调度和 Worker 复用共享验证。详情见 [资源快照](resource-snapshots.md)，不另建准备审批流程。

## Brief 与运行交接

每个 Segment 冻结 Brief/checksum，业务指令来自原 Skill；资源路径只来自已验证物化结果，权限不因 Brief 扩张。执行 profile 默认 SUPERVISED，不覆盖硬拒绝。续行、效果、结果分别使用 Runtime、[受控写入](repository-effects.md)和[结果设计](results-evaluation.md)，不在解释模块复制运行状态机。

## 验证与接续

按改动运行 [skills](../../SKM/backend/tests/skills/)、[contracts](../../SKM/backend/tests/contracts/)及相关消费者回归：来源/hash、输入编译、出典错误定位、局部调整、原请求交接、撤权、取消和发布引用。模型质量用固定输入及独立预期另验，不用 fixture 或结构正确代替业务保真。

## 部署和验证边界

API/Worker 共用装配与身份，按 [部署指南](../operations/deployment.md)同批升级。检查只核当前配置，不重写已存在的请求或运行记录。真实事务、存储、模型和部署证据分别报告；尚未实现的能力只列入计划，不追加每轮进度文档。
