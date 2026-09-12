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

- PostgreSQL 配置主机、整数端口、数据库、连接用户名和 TLS 模式；密码必须引用 SecretReference。范围逐项指定 `schema.table`，不接受任意 SQL、空列表或通配符。[`database.read/v1`](../../SKM/contracts/tools/database.read/v1/request.schema.json) 接受显式表名、列名、等值筛选、排序和有界分页，不接受 SQL 正文。Worker 使用原凭据、只读事务和 PostgreSQL statement/lock timeout，返回最多 100 行、1 MiB JSON。连接目标由项目 ADMIN 预先配置，模型不能指定主机、端口、凭据或扩大表范围；目标数据库及自定义类型/函数属于管理员信任边界。连接前和返回前复验原 binding，取消关闭当前连接，不以本地取消声称数据库故障已恢复。每次读取得到独立 live 结果，以内容 hash 和读取时间生成 Evidence，不承诺跨调用的分页处于同一快照。
- MCP 配置 HTTP(S) Streamable HTTP endpoint、可选 Bearer 凭据引用和明确资源 URI；URL 不含凭据、query 或 fragment，不接受 stdio 命令配置。`mcp.read/v1` 只发起已冻结 URI 的 `resources/read`，不调用远端 tools，不开放 sampling、elicitation 或本地 roots；服务工具调用尚未实现，不能由资源读取权自动取得。URI 在配置时按 SDK URI 类型规范化，执行时须逐字匹配冻结值，返回其他 URI 的内容拒绝发布。连接目标与服务实现属于项目 ADMIN 信任边界；模型不能传 endpoint/凭据或改变协议方法，远端正文仅作为数据，不取得指令权限。
- MCP 每次读取独立会话，用 SDK 处理初始化与 JSON/SSE；传输层限定原 endpoint、禁跳转和压缩、至多 16 次 HTTP 请求、单响应 1 MiB、累计响应 3 MiB，初始化与读取共用 20 秒 deadline。取消关闭本地会话/连接，不由 disconnect 推断远端已停止；不自动重放资源读取。文本或 Base64 内容最多 20 项、合计 1 MiB，超限整次失败，不静默截断。I/O 前后复验原 binding/凭据，记录读取时间、内容 hash 和 Evidence；第三方传输日志不输出 URL、会话 ID 或正文。
- PostgreSQL 读取可显式指定 `include_schema=true`，为普通表或分区表附带完整列名、SQL 类型、列级 NOT NULL 标记、默认值存在标记、generated/identity 属性和按定义顺序排列的主键，空表同样返回结构。只接受原 binding 内且当前账号拥有完整 SELECT 权限的表；先取得该表的 ACCESS SHARE 锁，再在同一 REPEATABLE READ、READ ONLY 事务内查询结构与数据，结构缺失、权限不足或超限均整次失败。结构最多 100 列、64 KiB，与行数据共用 1 MiB 上限，不返回默认表达式、CHECK/域约束或其他表信息。结构完整不代表行投影完整，也不授予写入权限或替代按精确主键取得的原行/不在场 Evidence。结构一并纳入响应和 Evidence 的内容 hash；省略或关闭此选项时保持旧响应形状和摘要算法，不补写历史结构。旧 Worker 不认识此选项时须拒绝请求，不能把缺失结构当成功。

- [`mcp.read/v1`](../../SKM/contracts/tools/mcp.read/v1/request.schema.json) 与 PostgreSQL 一样，经能力目录、可信 Provider、原 binding/撤权检查、Evidence 和 Worker 装配进入 Run；不得把远端 MCP 配置直接交给模型 SDK，或依据远端声明自动授予工具权限。配置或本机合成服务验证不代表真实业务 DB/MCP 验收。

## Excel 取得与转换的扩展边界

完整 rv-reviewer 使用指定项目文档库取得 Excel，在 Worker 内通过 [`document.convert/v1`](../../SKM/contracts/tools/document.convert/v1/request.schema.json) 显式调用 MarkItDown。转换工具要求 Manifest 和 Run 权限明确包含该能力，并复用文档选择、原 ID/hash 校验及引用保护；不为旧 Run 的 `document.read/v1` 自动增权。

新建 Run 的转换、单份元数据观察或目录分页来源明确冻结 `preparation_policy: on-demand/v1`，由完整来源摘要保护。准备阶段只将这些文档的原 ID、相对路径和 hash 写入 v2 文档 manifest 的 `deferred` 清单及索引，不下载或转换本文；同一文档与普通读取槽位重叠时，按需规则优先。Brief 的可选 `deferred_files` 与提示区分未取得、读取失败及不存在，实际取得仍须使用已授权 Tool 并验证原 bytes。没有该策略的旧来源和 v1 manifest 保持原物化方式；不在重试时补策略、改写 READY 或重取原集合。未知策略拒绝，按需清单仍受总量、当前执行权及完整文件回执校验。

转换仅接收已冻结文档的 `.xlsx` / `.xls` 原 bytes，输入至多 8 MiB、Markdown 至多 1 MiB；ZIP 展开上限沿用共享解析器。Worker 固定 MarkItDown 0.1.7 的 Excel converter，不使用 URL 自动判定、远端服务或插件。专用子 process 不继承凭据环境，限制 CPU/地址空间，拒绝 socket 与再次启动子 process；30 秒期限或取消后终止并回收。该 process 边界用于资源与停止控制，不代表通用不可信代码沙箱。失败及超限整次报错，不静默截断。返回完整 Markdown、原文 hash、Markdown hash、转换器版本和 Evidence，并说明表格结构、图形及公式可能丢失，不推测原 Excel 坐标。

需要保存转换原文时显式设置 `publish_artifact=true`，沿[可信附件发布](results-evaluation.md#可信附件的发布与读取)将同一 Markdown 字节留作备份来源；响应中的原 Artifact 引用、size/hash 供受控保存提案使用，模型不必重新写入全文。普通转换与输入前置条件不变，选项不意味着 MinIO 备份已完成。

S3 文档 source 可先只 HEAD，取得 LastModified、Version ID、opaque ETag、长度和 MIME；该观察绑定原 Project、文档 ID 和 DB 内容 metadata，不表示已下载、验证 hash 或转换成功。后续取得接收原观察：非 `null` Version ID 直接固定读取该版本，不以当前 HEAD 替换；未启用版本或 `null` version 时先要求当前 HEAD 与原观察一致，再 GET，最后 HEAD。GET metadata 必须与原观察完全一致，实际 bytes 仍须通过原 size/hash 校验。没有先前观察的既有读取则从本次 HEAD 开始核对。任一差异均失败，不换成最新版重试。

未启用版本时只证明这些观测点一致，不保证期间未发生再改回原值的变化。版本读取需要存储端相应权限，失败不能降格普通 GET。普通下载保持原读法；不支持观测的 backend 不补造存储元数据，要求原观察的调用也不能退回无观察读取。

[`document.inspect/v1`](../../SKM/contracts/tools/document.inspect/v1/request.schema.json) 按已冻结文档的相对路径完全匹配，只 HEAD，不读取正文；目录分隔不能通过 `.`、重复斜线、反斜线或尾斜线归一化补救。Manifest 和 Run 必须明确允许该能力，原 read/convert 权限不自动扩张。观察限定 20 秒，失败或取消不产生成功观察。响应给出真实存储 metadata、观察时间、观察摘要及 `content_verified: false`；声明的原文 hash 与观察 JSON 的摘要分开。Gateway 沿用先保存 Evidence 再返回及原成功调用重放，Evidence 保留观察和原存储位置摘要。单份观察成功不代表目录筛选完整。

inspect、list 的文档条目及 convert 响应可返回 `source_object_key`，值只取自原 Project/文档 ID 经授权解析且 namespace 核对通过的原存储引用，用于业务来源登记；逻辑目录、文件名、输出库或模型不能推导该值。它不是任意对象的读取授权，也不包含连接 URL、凭据或签名参数。含该字段的新观察 Evidence 使用 `observation_version: v2`，完整观察摘要覆盖 key，并继续保留原 key/namespace 的引用摘要；按观察取得时同时核对原引用摘要和原 key。旧 v1 观察仍按原字段与原摘要恢复，不补写 key、不升级历史记录；不提供该事实的旧 source 省略字段，业务需要时须明确处理缺失，不能将逻辑路径作为物理 key。

[`document.list/v1`](../../SKM/contracts/tools/document.list/v1/request.schema.json) 在原 Run 已授权的冻结集合内，按相对目录边界、递归、扩展名和排除条件取得稳定路径顺序的候选；空目录参数表示项目根，目录末尾允许一个斜线。它不扫描未注册对象，也不加入创建后上传的文档；当前冻结范围与原 Skill 的实时 MinIO prefix 列举仍有差距，不能据此声称覆盖整个 bucket。每页最多观察 50 个候选，整页 30 秒期限；先分页再按真实 LastModified 筛选，返回明确的冻结数量、候选数量和 `[scan_start, scan_end)`。游标绑定原 Project/Run、完整成员、规范化条件和页宽；成功调用重放原观察，新的调用重新观察时可能得到不同存储状态。

日期条件显式给出日期、IANA 时区及带时区的截止时间，使用当地当天 00:00 至次日 00:00 的半开区间并包含截止时间等值，不假定一天有 24 小时；Skill 自行提供其既定时区与输出排除目录，平台不写入业务默认值。每份候选保留 `matches_filter` 和 metadata Evidence，即使整页没有匹配项也必须沿 `next_cursor` 继续，直到 null；只有完整遍历且没有失败才可判断该冻结候选集合无目标。失败或取消不发布部分成功页；空候选页仍保存范围 Evidence。多页观察不是同一时刻的存储快照，日期筛选也不是额外授权。

转换请求可明确传入 `observation_ref`，由[共享只读查询](../../SKM/backend/src/skillmind/documents/observation_repository.py)连接原 Project/Run、成功的 inspect 或 list ToolCall 和 Evidence，核对原文档、工具成功响应、观察 JSON/hash、未验证标记及存储位置摘要。list 使用 `evidence_refs[entry.evidence_index]`，只接受与该 entry 严格对应的文档观察，不接受页摘要或另一条目的引用。确认后按该观察取得 bytes，并再次核对原内容 hash 和观察；存储位置、版本或内容不符、引用不成立或 DB 不可用均失败，不退回普通读取。查证和取得共用 20 秒期限，MarkItDown 另受既有转换期限控制；本地取消不证明 SDK thread 或远端已停止。转换 Evidence 记录 `observation_ref` 与实际取得信息。同 Run 的后续 Attempt 可以复用已确认观察；新 Run 不能借用。未提供引用的既有调用保持原读取方式，因此流程需要版本绑定时须明确传入引用，不由先前调用顺序推断。

读取与转换的 Evidence 在 `storage_observation` 中记录取得事实和一致性方式，并在 source 提供时以 `source_object_key` 保留原 key；不公开连接或凭据。既有 v1 内容快照和历史 Tool 记录不改写，`document.read/v1` 响应保持原形状。按需准备消除平台的提前下载/转换；任务的 `document_prerequisites` 可将已声明的 apply effect intent 设为所有文档读取、观察、分页及转换的前置条件，规则见下文。目录/文件名不能成为任意 URL、Shell 参数或越界路径；不执行 Excel 宏，不按不可信内容取其他资源。转换失败或信息缺失单独记录，不能冒充原文缺陷。

任务前置条件须有原文 trace，并必需声明 [`document.readiness/v1`](../../SKM/contracts/tools/document.readiness/v1/request.schema.json)。旧任务省略时不补造条件，旧 Worker 因不能解析必需的新能力而拒绝该任务，不能静默忽略规则。新建 context 在准备前核验全部输入文档均按需取得；任何未覆盖的普通物化来源均拒绝，重叠槽位沿用既有按需优先规则。Task Brief、Skill 蓝图预览和精确 Task Flow 投影保留原条件。

[共享正本查询](../../SKM/backend/src/skillmind/runs/document_prerequisites.py)使用原 Task 中的 Skill 快照和共享 Manifest 校验；只认可同 Run/Project、原 intent/operation/write slot、精确批准版本与摘要、APPLIED Proposal/Effect、成功 ToolCall 及对应前后 Evidence。Tool 的登记、调用前及成功保存均在原执行权事务内核验，批准但未应用、失败、未知、他 Run 记录和 checkpoint 自称完成都不放行；DB 查询失败同样拒绝。只读就绪工具返回原要求及已确认项，不执行外部写入、不扩权；成功重放保留当时结果，需要当前状态则新建读取调用。此条件证明指定获批效果已确认，并不自动证明提案中的业务表、`RUNNING` 字段、业务 ID 和其他步骤都忠于原 Skill，也不替代最终结果验收；这些仍须结合原文、提案约束和真实用例核对。

若接入使用 MCP tools，须冻结精确 tool 名、输入/输出 Schema、服务身份和参数范围，并通过注册 Provider 调用；不把任意远端 tool 直接暴露给模型，不相信服务自行声明的只读注解。转换和读取受有界 bytes、deadline、取消与凭据复验约束；远端可能写入的调用进入[受控效果](repository-effects.md#首版所需的存储和数据库写入)，不得借 `mcp.read/v1` 绕过批准。平台保存的成果与项目业务 MinIO 对象分开认定。

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

成果保存使用独立的 `document`/`write` 槽位，显式选择 `project-library:documents`；它不是输入文档的单份、集合或全集。候选复用项目现有文档库，要求 FileStorage 有持久存储归属，空文档库也可列出；候选不公开 bucket、namespace 或连接。前端按 access 区分两种选择，保存目标不自动勾选，候选消失时拒绝原草稿，不替换成输入全集。能力就绪与批准边界见[受控写入](repository-effects.md#minio-条件创建与原结果核对)。

即时执行/Schedule 共用组件，不自动选首份。候选变化保留草稿并要求修正，不改全集、替换 ID 或接受单成员集合；原请求确认不受草稿影响。

selected_sources_json 保存 Project/requirement、模式、成员 ID/路径/MIME/size/hash 与服务端 checksum。Worker 按原 ID 验字节，后续 Segment/Attempt 不重新枚举；客户端 checksum 不提供信任。

保存目标使用独立的 v1 snapshot；新 binding revision 2 固定 v2 对象前缀，旧 revision 1 历史解析仍使用原前缀。snapshot 含原 Run/binding、存储 scope、共享 binding checksum 与覆盖完整 snapshot 的摘要；同创建事务写入，重放不从当前配置重建。历史投影只在完整验证后将保存目标排除出输入清单，损坏或混入 `document_snapshot` 的来源显示 INVALID；删除保护同时核对原选择、Project/Run/slot，不以 `project-library` 标记推断无引用。

ContextBuilder 在工作区准备前验证保存目标的声明、原 Project/Run/slot、完整摘要和 Worker 当前存储配置，同时检查独立能力门禁与提案权限。保存目标不注册直接写入或文档读取 Tool；输入准备器只要求读取槽位的冻结清单，保存目标不扩大输入范围。Brief 按 requirement key 匹配来源，不将同能力的另一槽位视为已选择，也不向保存目标附加输入目录；实际保存仍经[批准与 Effect Worker](repository-effects.md#minio-条件创建与原结果核对)。

新准备的 Brief 在 identity 中传入原 Project ID；已校验的保存槽位另带 `document_library`（库 ID、Project ID、bucket），并在首次模型调用前渲染，供前置业务登记使用。库 ID 是平台逻辑文档库的稳定 UUIDv5：以存储 namespace UUID 为 namespace，以规范 JSON `{"kind":"project-document-library/v1","project_id":原ProjectUUID字符串,"bucket":原bucket}` 为 name；不含 Run、binding 或 slot，所以同库跨运行一致，Project/bucket/存储世代不同则区分。它不代表新建的 Integration 或外部 MinIO ID，不新增库表，也不改写原 binding/snapshot/checksum。成果回执使用同一身份算法。

以上字段是运行内的登记引用，不是对象读取许可；候选和历史来源摘要仍不公开存储 scope。Brief 不传 endpoint、namespace、凭据、内部 prefix 或未配置的目录/时区/输出设置；路径默认值由 Skill 自身规则处理。旧 Brief 缺少新可选字段时不补造原值，依赖这些字段的业务运行需要新版 Worker。

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
