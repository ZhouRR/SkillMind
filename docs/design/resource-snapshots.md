# 资源快照与 Run 工作区

本页定义 Run 的读取范围、内容冻结和输入副本。上传/下载/删除属于[文档生命周期](document-lifecycle.md)，请求重放属于[创建协议](run-creation.md)，实施状态见[计划 R01](../planning/roadmap.md#开发任务)。

## 先区分三种“冻结”

| 层次 | 固定事实与边界 |
| --- | --- |
| 授权快照 | Project、资源、capability、scope、Integration 版本；分支名不等于 commit，读取权不保证可达 |
| 内容快照 | 文档 ID/hash/成员，或仓库读取时解析出的 revision；checksum 不是授权，也不是备份 |
| 物化副本 | 实际文件、转换、索引与 skipped；首次准备不等于创建瞬间已取得内容 |

分别记录三个时点：document 用创建清单，repository 首次打开解析版本；issue 按 binding live 读取形成 Evidence，不物化文件树。

## PostgreSQL 与 MCP 接入

资源管理支持注册 `postgres` 和 `mcp` Provider、凭据引用和明确读取范围，沿用公开资源分类 `other`；凭据正文仅经 SecretReference 管理，公开列表仅返回配置 key。连接状态 ACTIVE 表示配置未停用，不证明远端可达；Provider 只有装配到 Worker 后才可标为 installed。

- PostgreSQL 配置主机、整数端口、数据库、只读用户名和 TLS 模式；密码必须引用 SecretReference。范围逐项指定 `schema.table`，不接受任意 SQL、空列表或通配符。[`database.read/v1`](../../SKM/contracts/tools/database.read/v1/request.schema.json) 接受显式表名、列名、等值筛选、排序和有界分页，不接受 SQL 正文。Worker 使用原凭据、只读事务和 PostgreSQL statement/lock timeout，返回最多 100 行、1 MiB JSON。连接目标由项目 ADMIN 预先配置，模型不能指定主机、端口、凭据或扩大表范围；目标数据库及自定义类型/函数属于管理员信任边界。连接前和返回前复验原 binding，取消关闭当前连接，不以本地取消声称数据库故障已恢复。每次读取得到独立 live 结果，以内容 hash 和读取时间生成 Evidence，不承诺跨调用的分页处于同一快照。
- MCP 配置 HTTP(S) Streamable HTTP endpoint、可选 Bearer 凭据引用和明确资源 URI；URL 不含凭据、query 或 fragment，不接受 stdio 命令配置。`mcp.read/v1` 只发起已冻结 URI 的 `resources/read`，不调用远端 tools，不开放 sampling、elicitation 或本地 roots；服务工具调用的需求及授权范围仍待确认。URI 在配置时按 SDK URI 类型规范化，执行时须逐字匹配冻结值，返回其他 URI 的内容拒绝发布。连接目标与服务实现属于项目 ADMIN 信任边界；模型不能传 endpoint/凭据或改变协议方法，远端正文仅作为数据，不取得指令权限。
- MCP 每次读取独立会话，用 SDK 处理初始化与 JSON/SSE；传输层限定原 endpoint、禁跳转和压缩、至多 16 次 HTTP 请求、单响应 1 MiB、累计响应 3 MiB，初始化与读取共用 20 秒 deadline。取消关闭本地会话/连接，不由 disconnect 推断远端已停止；不自动重放资源读取。文本或 Base64 内容最多 20 项、合计 1 MiB，超限整次失败，不静默截断。I/O 前后复验原 binding/凭据，记录读取时间、内容 hash 和 Evidence；第三方传输日志不输出 URL、会话 ID 或正文。
- [`mcp.read/v1`](../../SKM/contracts/tools/mcp.read/v1/request.schema.json) 与 PostgreSQL 一样，经能力目录、可信 Provider、原 binding/撤权检查、Evidence 和 Worker 装配进入 Run；不得把远端 MCP 配置直接交给模型 SDK，或依据远端声明自动授予工具权限。配置或本机合成服务验证不代表真实业务 DB/MCP 验收。

## 文档选择与冻结设计

### 用户选择的是范围，不是 Provider 名称

| 选择 | 冻结与限制 |
| --- | --- |
| 单份/集合 | 同 Project 的原 ID/hash；集合去重校验后排序，不替换同路径重传的新 ID |
| 显式全集 | 固定创建事务当时的成员，后续上传不加入 |
| 可选不选 | 不授予该槽位读取权，不回退全集 |
| 必需未选 | 创建失败，不在后续对话中补授权 |

多槽位共用 Run 级去重并集与 input/documents/，不做槽位隔离；manifest 保留 requirement 关系，重复 ID 元数据必须一致。

### 公开选择与读取投影的实施契约

sources 由客户端编码、服务端校验，界面显示名称与范围。

| 模式 | 编码与数量 |
| --- | --- |
| 单份 | document:&lt;UUID&gt;，恰好一个 |
| 集合 | documents:&lt;UUID&gt;,&lt;UUID&gt;…，同槽位 2–5000 个不同候选 |
| 全集 | project-documents:all，创建时 1–5000 个成员；空/超量拒绝，不截断 |
| 不使用 | 省略可选槽位；空串不是合法选择 |

即时执行/Schedule 共用组件，不自动选首份。候选变化保留草稿并要求修正，不改全集、替换 ID 或接受单成员集合；原请求确认不受草稿影响。

selected_sources_json 保存 Project/requirement、模式、成员 ID/路径/MIME/size/hash 与服务端 checksum。Worker 按原 ID 验字节，后续 Segment/Attempt 不重新枚举；客户端 checksum 不提供信任。

### 用一个例子理解冻结边界

Run 1 全集冻结 A、B 后新增 C：排队、重试、原请求确认仍只有 A、B，新 Run/occurrence 才可包含 C。同路径重传 A′不能替代 A，原内容缺失则失败。

### 读取清单和资源摘要

Run detail 的 document_snapshots 按 requirement_key 提供服务端验证后的清单：

| status | snapshot 与含义 |
| --- | --- |
| FROZEN | 合法 v1 清单；不是当前目录、blob 可达性或准备完成证明 |
| LEGACY_UNAVAILABLE | null；旧记录没有可信清单，不猜测历史范围 |
| INVALID | null；校验失败，不公开未验证成员，其他合法历史仍可读 |

空数组表示未识别文档来源；缺必需 document_snapshots 是 API 契约错误，旧 Run 则用历史状态表达。

detail/history 的 selected_sources 仅公开 provider/capability/resource_kind/access，兼容已知旧字符串，不暴露内部 JSON、scope 或 Secret locator。大清单只在 detail，不作授权输入。[服务端投影](../../SKM/backend/src/skillmind/runs/resource_projection.py)重算 checksum，[Web](../../SKM/web/src/api/runResources.ts)只验形状和 Project/slot/关联，不复制身份算法。

### 失败、缓存与历史

| 情况 | 处理 |
| --- | --- |
| 原 ID/blob 缺失，元数据或实际 hash 不符 | 准备失败；不按路径替换、不伪装普通 skipped |
| 内容合法但不支持/单文件过大 | manifest 记录 skipped，结果说明未覆盖范围 |
| 完整副本合法 | 校验来源、Run/Project、完整文件树后复用，不重新读取全集 |
| 副本缺失、改写或混入文件 | 保留现场并失败，不删除重建或降级 live |
| live Tool 无法取原 ID | 返回不可用；已存副本与 live 可达性分别判断 |
| 旧 Run 无清单/回执 | 终态只读且标未知；非终态不自动授权全集，需新输入则新建 Run |

删除须经[原 Run/调度/occurrence 引用门禁](document-lifecycle.md#删除事务与引用判定)，未知历史拒绝；恢复不得任意删除缓存。

### 创建重放与调度

全集规则是请求意图，首次成员是执行事实，不把动态展开混入请求 hash。重放沿用原快照；Schedule 每 occurrence 独立冻结，原 occurrence 返回原 Run，失效按[调度规则](task-scheduling.md#保存和执行边界)拒绝，不换来源。

## 仓库授权与内容版本

scope.paths 是硬边界，scope.revisions 是允许范围；HEAD/分支可移动，空 allowlist 不限首次版本。[repository_source](../../SKM/backend/src/skillmind/agent/repository_source.py)解析检查后，manifest 记录具体 commit/SVN revision。

复现优先副本；live 读取仍验冻结授权并标实际 revision，跨版本证据分开。要求创建瞬间内容时，须指定固定可达 revision 或先实现创建时解析持久化。

## 产物与访问

```text
input/（只读）
├── documents/               文档或转换文本
│   └── .skillmind/        manifest.json、files.txt
└── <repository requirement>/
    └── .skillmind/        manifest.json、files.txt、history.txt
workspace/                   可写临时工作
output/                      可写报告/补丁
```

Tool 逻辑路径映射到 Run 根下 .skillmind-inputs/&lt;snapshot_id&gt;/，不开放世代目录、不用 symlink 切换，也不搬迁旧 input/ 补签。Brief、read/search、Evidence 必须消费同一 PreparedInput。

workspace.write 只写 workspace/output；v2 output 原 UTF-8 字节另作[不可变附件](results-evaluation.md#可信附件的发布与读取)，v1/中间文件不自动成为附件。xlsx/xlsm/docx 经[统一转换](../../SKM/backend/src/skillmind/agent/binary_text.py)保留源 hash/位置，PDF 未支持；文件、manifest、Evidence、上下文均不含凭据。

## 输入准备与可信缓存

每 Run 一份覆盖全部根的 DB 回执，防止文件与本地 manifest 一起被改；信任 DB/受控 Worker，不替代路径隔离和授权。

[DTO](../../SKM/backend/src/skillmind/runs/input_snapshot.py)/[store](../../SKM/backend/src/skillmind/runs/repository_inputs.py)/[0029](../../SKM/backend/migrations/versions/0029_run_input_snapshots.py)固定 PREPARING/READY、世代、Run/Project/Attempt、来源摘要、全部 path/size/hash、总量和完成时间。它是内部回执，非 Run 状态/API；READY 空集不同于缺回执。

### 一次准备的提交边界

```text
有效 Attempt + heartbeat/取消监督
  → TX A：校验来源/执行权，保存 PREPARING
  → 锁外：独占世代，全部根读取/转换/计量，同步并封闭候选
  → TX B：重验准备身份/lease/取消，提交 READY
  → 确认同世代回执 → PreparedInput
  → 冻结 Brief / 最终启动校验 → Agent
```

按 Run → Segment → Attempt 取锁后验当前 lease/取消。候选落盘不是发布；摘要只取冻结数据，不从旧目录补信任。首次核对回执/候选，复用验完整树/字节/总量/manifest，重复完成不改事实。

后续 Segment/Attempt 只复用原 READY；取消后的慢 I/O 不得提交/启动，准备 timeout 不证明回滚或许可删世代。启动监督见[Runtime](agent-runtime.md#从领取到模型启动的边界)。

### 准备中断与再次使用

| 状态 | 放行条件 |
| --- | --- |
| 无回执且无旧输入/命名空间 | 有效 Attempt 可首次准备 |
| 无回执但存在命名空间/旧输入 | 保留并拒绝；空目录、另一 UUID、文件/symlink 都不能换名绕过 |
| PREPARING，无论文件看似完整与否 | 仅原有效准备继续；接管不替别人补签，暂无自动修复 |
| READY 且所有来源/树/字节/总量匹配 | 当前授权下复用；额外文件/目录、缺失或 hash 不符则失败保留 |
| 完成响应未知 | 只确认原 Run 的同世代 READY；不再次 begin/complete 或新建输入 |
| lease 失效/取消成立 | 禁止完成与模型启动，由合法恢复处理 |

[安全 I/O](../../SKM/backend/src/skillmind/agent/materialization_storage.py)先独占整个 .skillmind-inputs，再建 UUID，防止绕开丢回执现场；中断保留痕迹，READY 不重新创建。

store 仅对 complete 已返回后、事务退出的 DBAPIError/TimeoutError/ConnectionError，另开 session 一次确认原准备者/世代/来源/文件及当前 lease/取消。complete 前错误、CancelledError 不确认；仍未知则失败。

DB、世代文件、transcript 必须来自匹配的[恢复点](../operations/backup-recovery.md)，不以较晚文件补签较早 DB。

### 读取时的完整性边界

read/search 与 Evidence 使用同次回执核验的字节，不先 hash 后重开。描述符锚定 Run 根、不跟 symlink、有界读取，且只接受普通单链接文件。

input search 先验全树再查可信清单；未知文件、hardlink/FIFO、缺失/篡改不得伪装 skipped。结果/扫描截断标 truncated，零匹配仅覆盖实际范围。可变 workspace 不要求回执，仍守路径/读取上限。

### 跨根总量

三层限制为单文件、逐根 max_files/max_bytes、跨根 max_total_files/max_total_bytes。最终存量包含转换物、manifest、索引/history；保留原文另计，未落盘 skipped 不计。共享文档计一次，不同仓库根副本分别计。

必需索引或任一根/总量超限则整批失败，不交付部分根。复用不双计，但按有效限制验证。临时峰值、workspace/output、搜索响应与[模型预算](run-budgets.md)另限，最终存量不保证磁盘不耗尽。

## 已知差距与后续设计

接续 [workspace_materializer](../../SKM/backend/src/skillmind/agent/workspace_materializer.py)与[协议回归](../../SKM/backend/tests/agent/test_input_preparation_protocol.py)，必需 store 不得改为可选。PREPARING/孤立目录尚无自动修复；消费者联调、历史/混合版本及真实事务/恢复/仓库验收见[计划 R01](../planning/roadmap.md#开发任务)。

## 验收条件

验收选择并集、全集新增/同路径重传、跨 Project/伪造 hash、原键并发胜者；再验全树双重篡改、额外文件、校验后替换、慢准备/接管/取消/未知提交，以及多根最后超限时模型未启动。仓库 live 与副本 revision、转换来源和 skipped 必须可追溯；旧记录不补造。
