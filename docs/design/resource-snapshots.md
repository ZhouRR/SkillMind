# 资源快照与 Run 工作区

本页定义 Run 的读取范围、内容冻结和输入副本。上传/下载/删除属于[文档生命周期](document-lifecycle.md)，请求重放属于[创建协议](run-creation.md)，实施状态见[计划 R01](../planning/roadmap.md#r01-资源冻结)。

## 先区分三种“冻结”

| 层次 | 固定事实与边界 |
| --- | --- |
| 授权快照 | Project、资源、capability、scope、Integration 版本；分支名不等于 commit，读取权不保证可达 |
| 内容快照 | 文档 ID/hash/成员，或仓库读取时解析出的 revision；checksum 不是授权，也不是备份 |
| 物化副本 | 实际文件、转换、索引与 skipped；首次准备不等于创建瞬间已取得内容 |

三个时点分别记录。当前 document 使用创建清单，repository 首次打开才解析具体版本，issue 按 binding 逐条 live 读取并形成 Evidence，不物化文件树。

## 文档选择与冻结设计

### 用户选择的是范围，不是 Provider 名称

| 选择 | 冻结与限制 |
| --- | --- |
| 单份/集合 | 同 Project 的原 ID/hash；集合去重校验后排序，不替换同路径重传的新 ID |
| 显式全集 | 固定创建事务当时的成员，后续上传不加入 |
| 可选不选 | 不授予该槽位读取权，不回退全集 |
| 必需未选 | 创建失败，不在后续对话中补授权 |

多槽位文档形成 Run 级去重并集，共用 input/documents/，不是槽位间隔离。Manifest 保留各 requirement 成员关系，重复 ID 的元数据必须一致。

### 公开选择与读取投影的实施契约

沿用 sources 字符串，由客户端编码、服务端独立校验；用户界面显示名称与范围。

| 模式 | 编码与数量 |
| --- | --- |
| 单份 | document:&lt;UUID&gt;，恰好一个 |
| 集合 | documents:&lt;UUID&gt;,&lt;UUID&gt;…，同槽位 2–5000 个不同候选 |
| 全集 | project-documents:all，创建时 1–5000 个成员；空/超量拒绝，不截断 |
| 不使用 | 省略可选槽位；空串不是合法选择 |

不自动选择首份文档；即时执行和 Schedule 共用输入组件。候选变化后保留可解释草稿并要求修正，不偷偷改全集、替换 ID 或把单成员集合当有效集合。原请求确认使用已发送内容，不受新草稿影响。

服务端创建时保存 Project/requirement、模式、ID、路径、MIME、size、hash 与快照 checksum 于 selected_sources_json；客户端 checksum 不提供信任。Worker 按原 ID 校验实际字节，后续 Segment/Attempt 不重新枚举。

### 用一个例子理解冻结边界

全集创建 Run 1 时只有 A、B，随后新增 C：Run 1 的排队、重试和原请求确认仍只有 A、B；明确新建 Run 2 或下一次 Schedule occurrence 才可能固定 A、B、C。A 同路径重传为 A′不替代原 ID；缺失原内容明确失败。

### 读取清单和资源摘要

Run detail 的 document_snapshots 按 requirement_key 提供服务端验证后的清单：

| status | snapshot 与含义 |
| --- | --- |
| FROZEN | 合法 v1 清单；不是当前目录、blob 可达性或准备完成证明 |
| LEGACY_UNAVAILABLE | null；旧记录没有可信清单，不猜测历史范围 |
| INVALID | null；校验失败，不公开未验证成员，其他合法历史仍可读 |

合法空数组表示未识别到文档来源，不等于旧响应缺字段。Web 对缺少必需 document_snapshots 的旧 API 报契约错误；新 API 读取旧 Run 用明确历史状态。

detail/history 的 selected_sources 只公开 provider / capability / resource_kind / access 摘要，兼容已知旧字符串；不暴露内部 JSON、scope、Secret locator 或未来字段。大成员清单只在 detail，不成为 Provider 授权输入。

[服务端投影](../../PJM/backend/src/projectmind/runs/resource_projection.py)重算 checksum；[Web validator](../../PJM/web/src/api/runResources.ts)校验形状、Project/slot 与关联，不复制服务端身份算法。公开变化同步 response、Schema/example/OpenAPI、Web 与三语，不改冻结快照。

### 失败、缓存与历史

| 情况 | 处理 |
| --- | --- |
| 原 ID/blob 缺失，元数据或实际 hash 不符 | 准备失败；不按路径替换、不伪装普通 skipped |
| 内容合法但不支持/单文件过大 | manifest 记录 skipped，结果说明未覆盖范围 |
| 完整副本合法 | 校验来源、Run/Project、完整文件树后复用，不重新读取全集 |
| 副本缺失、改写或混入文件 | 保留现场并失败，不删除重建或降级 live |
| live Tool 无法取原 ID | 返回不可用；已存副本与 live 可达性分别判断 |
| 旧 Run 无清单/回执 | 终态只读且标未知；非终态不自动授权全集，需新输入则新建 Run |

单文档删除已按原 Run 快照、调度及保留 occurrence 接入[引用门禁](document-lifecycle.md#删除事务与引用判定)，未知历史拒绝删除；真实并发和持久 blob 清理仍待补齐。缓存保留不由恢复流程任意删除。

### 创建重放与调度

“全集”是稳定请求意图，首次成员是执行事实；重放查询原身份，沿用原快照，不把动态展开结果混入请求 hash。Schedule 保存选择规则，每个 occurrence 独立冻结；同 occurrence 返回原 Run。失效选择按[调度规则](task-scheduling.md#保存和执行边界)拒绝，不换来源。

## 仓库授权与内容版本

scope.paths 是硬边界，scope.revisions 是允许范围；HEAD/分支可以移动，空 allowlist 不表示只许读首次版本。[repository_source](../../PJM/backend/src/projectmind/agent/repository_source.py)负责解析与检查。

首次物化 manifest 记录具体 commit/SVN revision；复现优先读副本。额外 live 读取仍满足冻结授权，并标实际 revision；跨版本证据不能混为同一事实。若要求创建瞬间内容，须显式固定可达 revision 或先实现创建时解析持久化，分支名不满足要求。

## 产物与访问

```text
input/（只读）
├── documents/               文档或转换文本
│   └── .projectmind/        manifest.json、files.txt
└── <repository requirement>/
    └── .projectmind/        manifest.json、files.txt、history.txt
workspace/                   可写临时工作
output/                      可写报告/补丁
```

这是 Tool 逻辑路径。实际 input_dir 是 Run 根下 .projectmind-inputs/&lt;snapshot_id&gt;/，由平台映射，不开放世代目录或用 symlink 切换；旧 input/ 不搬迁补签。PreparedInput 的新 workspace/resources 必须由 Brief、read/search、Evidence 全部消费。

只经 workspace.write 写 workspace/output；input 与 Project 文档库不自由覆盖。v2 的 output 写入另将原 UTF-8 字节发布为[不可变附件](results-evaluation.md#可信附件的发布与读取)，覆盖文件不改变已发布字节；v1 和 workspace 中间文件不自动成为附件。xlsx/xlsm/docx 经[统一转换器](../../PJM/backend/src/projectmind/agent/binary_text.py)生成文本，保留源 hash/位置；PDF 未支持。凭据不进入文件、manifest、Evidence 或上下文。

## 输入准备与可信缓存

一个 Run 使用一份覆盖全部资源根的数据库回执；各根 manifest 不能证明自己未被连同文件改写。威胁模型允许 workspace 被篡改，但信任数据库与受控 Worker，不替代路径隔离/授权。

[DTO](../../PJM/backend/src/projectmind/runs/input_snapshot.py)、[store](../../PJM/backend/src/projectmind/runs/repository_inputs.py)与 [0029](../../PJM/backend/migrations/versions/0029_run_input_snapshots.py)已有 Run 唯一的 PREPARING/READY 记录，固定世代、Run/Project/Attempt、来源摘要、全部文件 path/size/hash、总量与完成时间。这不是 Run 状态或新公开 API；合法 READY 空集与缺回执不同。

### 一次准备的提交边界

```text
有效 Attempt + heartbeat/取消监督
  → TX A：校验来源/执行权，保存 PREPARING
  → 锁外：独占世代，全部根读取/转换/计量，同步并封闭候选
  → TX B：重验准备身份/lease/取消，提交 READY
  → 确认同世代回执 → PreparedInput
  → 冻结 Brief / 最终启动校验 → Agent
```

锁顺序 Run → Segment → Attempt，获锁后判定当前 lease/取消。候选落盘不叫发布；来源摘要来自冻结数据，不能从未知旧目录重新采样补信任。首次完成核对回执与候选，复用另验完整树/字节/总量/manifest；重复完成只能返回同一事实。

后续 Segment/Attempt 只复用原 READY 世代。慢 I/O 在取消后返回也不能提交或启动；独立准备 timeout 可能发生在提交中，不证明回滚，不许可删世代。监督责任见[Runtime](agent-runtime.md#从领取到模型启动的边界)。

### 准备中断与再次使用

| 状态 | 放行条件 |
| --- | --- |
| 无回执且无旧输入/命名空间 | 有效 Attempt 可首次准备 |
| 无回执但存在命名空间/旧输入 | 保留并拒绝；空目录、另一 UUID、文件/symlink 都不能换名绕过 |
| PREPARING，无论文件看似完整与否 | 仅原有效准备继续；接管不替别人补签，暂无自动修复 |
| READY 且所有来源/树/字节/总量匹配 | 当前授权下复用；额外文件/目录、缺失或 hash 不符则失败保留 |
| 完成响应未知 | 只确认原 Run 的同世代 READY；不再次 begin/complete 或新建输入 |
| lease 失效/取消成立 | 禁止完成与模型启动，由合法恢复处理 |

[安全 I/O](../../PJM/backend/src/projectmind/agent/materialization_storage.py)先独占 .projectmind-inputs 命名空间，再建指定 UUID；只独占 UUID 不足以发现丢回执现场。创建中断保留痕迹，READY 复用不再首次创建。

当前 store 仅在 complete 已返回、事务退出发生 DBAPIError/TimeoutError/ConnectionError 时，另开 session 一次确认原准备者/世代/来源/文件及当前 lease/取消。complete 返回前错误与 CancelledError 不走确认；未知仍失败，不假设自动恢复。

恢复核对同一数据库、世代文件与 transcript [恢复点](../operations/backup-recovery.md)，不能用较晚文件给较早数据库补签。

### 读取时的完整性边界

read/search 返回与 Evidence 引用必须来自对照回执验证的同一份字节，不先 hash 再重新打开。使用锚定 Run 根、不跟随任何 symlink 的描述符、有界读取、普通单链接文件检查。

input search 先验证完整树，再按可信清单搜索；未知文件、hardlink/FIFO、缺失或篡改不伪装 binary skipped。正常结果/扫描截断显式 truncated；零匹配只覆盖实际搜索范围。可变 workspace 不需要伪造回执，但仍受路径和读取上限约束。

### 跨根总量

当前已接入逐根及 max_total_files / max_total_bytes，最终存量按实际字节计，包含转换物、manifest、索引/history；保留原文另计，未落盘 skipped 不计原文字节。共享文档只计一次，不同仓库根的副本分别计。

单文件、逐根 max_files/max_bytes、全部输入总量是三层限制。必需 manifest/索引超限须失败，最后一根或总量失败时不能交付前几根。复用不重复消耗存量，但仍按有效限制验证。临时峰值磁盘、workspace/output、搜索响应和[模型预算](run-budgets.md)各自控制，不从最终存量推导磁盘不会耗尽。

## 已知差距与后续设计

公开选择/清单、回执/物化/ContextBuilder/Tool/Worker 接线、跨根限额与准备监督已有代码。接续核对 [workspace_materializer](../../PJM/backend/src/projectmind/agent/workspace_materializer.py)和[协议回归](../../PJM/backend/tests/agent/test_input_preparation_protocol.py)，不把必需 store 改为可选。

剩余验收是完整消费者联调、真实 DB 并发/提交不明/迁移恢复、旧非终态与混合版本隔离、真实仓库凭据/故障及进程停止。当前无 PREPARING/孤立目录自动修复器；部署和完整恢复不由局部文件测试替代。

## 验收条件

验收选择并集、全集新增/同路径重传、跨 Project/伪造 hash、原键并发胜者；再验全树双重篡改、额外文件、校验后替换、慢准备/接管/取消/未知提交，以及多根最后超限时模型未启动。仓库 live 与副本 revision、转换来源和 skipped 必须可追溯；旧记录不补造。
