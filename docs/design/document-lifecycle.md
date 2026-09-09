# 项目文档的保存、读取与清理

本页负责文档资产；[资源快照](resource-snapshots.md)负责 Run 选择、冻结和副本，[项目生命周期](project-lifecycle.md)负责整项目操作。以下区分现状与修正要求，不是完整存储可靠性已交付的声明。

## 一个例子：列表消失不等于清理完成

上传 A 产生文档 ID 与 blob；Run 1 冻结 A 的 ID/hash。删除 A 当前先删元数据、再尝试删 blob；同路径重传 A′ 是新 ID，不能修复 Run 1 的原引用。

必须区分文档目录、存储字节、Run 冻结引用、物化副本：列表消失不证明字节清理，Run 仍显示 A 不证明可下载，删除报错也不证明元数据未提交。

## 身份、目录与公开面

ProjectDocument 元数据在 PostgreSQL，blob 用内部 storage_key 定位。document_id 是身份，folder/name 是展示路径；同 Project/路径唯一，不按内容去重。提供上传、列表、精确元数据读取、下载和删除，无覆盖、移动、改名、版本或恢复 API；目录由 folder 投影，空目录不保存。

| 操作 | 当前语义 |
| --- | --- |
| POST documents | 单 file + 可选 folder 的 multipart，201 元数据；同名 409 document_conflict |
| GET documents | 按 folder/name 排序的完整数组，无查询/分页/Folder 契约 |
| GET document | 按 Project + 原 ID 读取当前元数据；不是原删除或清理回执 |
| GET document content | Project + 精确 ID 授权、核验实际 size/hash 后返回附件，不是公共 blob URL |
| DELETE document | 元数据 commit 后调用 storage，通常 204，不保证完整清理 |

公开数据只含身份、路径、size/MIME/checksum、上传者和时间。读用 ProjectReadActor，写用 ProjectWriteActor 且要求 ACTIVE；越权与不存在统一 404，已处理文档响应使用 no-store。上传和单文档删除另在业务事务复核原会话与当前 Project/成员；这不等于持久上传、字节配额与清理已完成。

## 上传的三个边界

[上传入口](../../PJM/backend/src/projectmind/api/routes/documents.py)先完成 Origin/CSRF、会话与 Project 授权，再读取 multipart；不使用会在 dependency 前解析正文的 File/Form 参数。公开请求仍为一个 file 和可选一个 folder，重复、未知字段、文件/普通字段混用与不完整结束边界返回 422 invalid_document_upload。

正文用现有 multipart 引擎流式解析，不创建临时文件或后台写线程。文件实际字节受配置上限约束；整个请求最多为该上限 + 16 KiB，分块、缺失/低报 Content-Length 与结束边界后的 epilogue 都计数。超限在追加 buffer 前返回 413 document_upload_too_large，不进入存储。header 合计上限 16 KiB，folder 至多 1,024 bytes，再由业务规则限制字符数；filename/folder 使用严格 UTF-8，拒绝歧义参数、扩展参数和压缩 envelope。

接收完成后交付不可变 bytes；取消与断连直接结束接收，不被解释为成功。每请求有界缓冲不等于全进程内存、并发数、慢请求或代理缓冲预算。

### 上传的授权事务

[DocumentService](../../PJM/backend/src/projectmind/documents/service.py)在首次 await 前复制正文，规范化路径并校验大小/MIME/文本策略：

1. TX A：复核原授权、ACTIVE 与当前元数据配额；commit 后释放锁。
2. 锁外 PUT：写新对象，核验回执与原 key/size/MIME/hash。
3. TX B：复核同一原授权、ACTIVE 与最新用量；创建元数据、flush、最终授权，再 commit 并返回 201。

两个事务都采用 Organization UPDATE → 当前 User SHARE → 原 AuthSession UPDATE → Project/成员 SHARE。锁等待、读取和 flush 后重新取时间复核原会话，不另选同用户新登录；归档、成员移除、停用、撤销或项目删除后不得再发布元数据。上传者从原 access 取得，不由调用者另传 UUID。存储 I/O 不持有这些锁。

| 当前检查 | 保证范围与缺口 |
| --- | --- |
| 文件默认 25 MiB，Project 默认 500 MiB | 上传入口限制实际文件/请求字节；Project 配额仍不含孤立 blob/副本/备份 |
| 名称、folder 最多 200 字符 | 名称还须通过共享 storage key 的单段 128 限制；129–200 名称在 PUT 前稳定返回 422 invalid_document_name，不截断 |
| MIME allowlist、指定文本凭据扫描 | 校验申报类型，不证明真实格式、病毒安全或任意二进制已脱敏 |
| 最终发布在共享门禁内重读用量 | 防止遵守该门禁的上传并发发布超额元数据；没有持久配额预留，锁外 PUT 仍可留下未计费对象 |
| blob 先写、元数据唯一约束后验 | 仅精确路径唯一约束映射 document_conflict；其他 DB 异常不冒充同名拒绝。失败仍可能留下孤立 blob |

PUT adapter 回执须与原请求一致，不能让返回的任意 key/大小/hash 决定保存身份；这不是新增远端 GET/read-back 或持久上传确认。拒绝、取消、存储错误和 commit 未知均不立即补偿删除：旧 PUT 可能仍在运行，DB 也可能已提交。上述门禁仅约束本版 writer；旧 API、任意 SQL、真实 PostgreSQL 并发及完整恢复仍需独立验收。

### 保存可靠性的修正要求

在已有接收与发布门禁上，为 Project 建立持久原子配额预留与结算，最终发布时复核授权、状态、路径与原预留。当前最终用量复查不能替代该协议；网络 I/O 不放入长期 Project 行锁。

持久保存上传意图、原请求身份与 blob 归属，区分确定拒绝、存储未知、commit 未知。未知先核对，不能异常后立即删除可能已提交的对象；确认无引用的失败上传才进入可重试清理。幂等需定义 actor/Project、摘要、保留期和并发胜者，不是仅加 header。

## 读取、下载与预览

普通下载和冻结来源使用同一实际字节校验；按原 ID 取元数据，锁外读取该对象，确认 size/SHA-256 后才返回完整内容。冻结读取额外比对原快照的 ID、路径、MIME 与 hash，不使用同路径新文件替代。

S3 在同一次 GET 中最多读取声明 size + 1 bytes 判定超限，不先 stat 再读；连接、读取、关闭均在线程内，不阻塞事件循环。取消等待不证明 SDK 线程或远端已停止，线程仍负责关闭自己的响应。这个边界不等于全服务内存、并发或网络超时预算。

| 下载结果 | 稳定响应与处理 |
| --- | --- |
| 元数据不存在或越权 | 原有 404；不通过存储枚举补查身份 |
| 元数据仍在、原 blob 缺失 | 409 document_content_missing；不按同名文件补齐 |
| 实际大小/hash 不符或元数据不合法 | 409 document_content_invalid；不返回部分内容或重算保存 hash |
| S3 拒绝、断连、读取故障 | 503 document_storage_unavailable；不当作文件不存在，不公开 key/SDK 错误 |

附件始终带 no-store、nosniff 与安全 Content-Disposition；保留合法 MIME，非法值降级为 octet-stream。文件名用单段 ASCII fallback 与 UTF-8 filename*，不直接拼接中文、引号或控制字符，不改数据库原名。

Web 仅预览 txt/md/markdown/htm/html，统一上限 1,000,000 bytes。列表 size 只用于禁用按钮；client 只接受 200，按实际响应流计数，缺失或低报 Content-Length 不绕过上限，错误正文也有界。超限即停止等待并提示下载，不展示截断片段；UTF-8 无法解码时明确失败。

Markdown/文本保持原文。HTML 在无浏览上下文的 template 中解析，只重建静态正文/基础结构白名单；不保留脚本、上传 CSS、表单、嵌套 frame、外部资源、URL/事件属性。图片只留下已有替代文字，链接只显示文本；超过 20,000 个解析节点则安全显示原文，不递归截断内容。

重建后的 srcDoc 在正文之前设置[默认拒绝的 CSP](https://www.w3.org/TR/CSP3/#meta-element)，只允许平台固定显示 CSS；iframe 使用 sandbox="" 与 no-referrer。页面明确提示预览会移除主动内容，原始文件仍可下载。这是静态文档预览，不是 generated Host，也不保证下载后在其他应用打开原文件的安全。

预览共用 30 秒读取门禁。关闭、同 tick 换文件、换 actor/会话/Project 时立即失效原请求；晚到正文或 401 不影响新上下文。当前读取的 401/403/Project 404 关闭写入资格，不被列表刷新重新打开；预览结果不解除未知删除。

## 删除与历史引用

单文档删除已接引用检查及原会话门禁；元数据 commit 后才尝试原 key 的 storage 删除。没有持久清理回执：storage 失败仍会留下 blob，原 ID 再删先得 404；S3 adapter 吞掉全部 S3Error，204 可掩盖 AccessDenied。整 Project 删除另走元数据清单，不调用逐文件清理。

### 删除事务与引用判定

```text
Organization UPDATE → 当前 User SHARE → 原 AuthSession UPDATE
  → Project / 成员 SHARE → 原 Document UPDATE
  → 校验历史引用 → 删除元数据 / flush → 最终授权 → commit
  → 锁外尝试删除原 blob
```

只接收原 cookie/CSRF 与服务器 request UUID，不另选同用户的新会话。各次锁等待、引用读取后和最终 flush 后复核原授权；资格失效整体回滚。DB 异常、取消或 commit 响应未知不触发 blob 删除，也不自动重发。

| 保存事实 | 删除判定 |
| --- | --- |
| 任意状态 Run 的可信文档快照 | 原请求、Project/槽位、成员/hash 一致后，保护冻结 ID；终态和已存副本不解除引用 |
| 任意状态 Schedule 的 SINGLE/SET | 保护显式原 ID；暂停、归档或未触发不解除引用 |
| 所有保留 occurrence 的 SINGLE/SET | 原快照/checksum/索引及父身份须匹配；编辑后的旧配置、SETTLED 无 Run 的审计仍保护原 ID |
| Schedule/occurrence 的动态 ALL | 尚未展开的规则不永久占有当前目录；已创建 Run 的 ALL 只保护原冻结成员 |
| 原可选槽位未选、无文档的合法 Run | 不推导文档引用；任意输入文本中的 UUID 不是引用 |
| 旧 provider 名无法辨义、缺失或损坏快照 | 返回 409 document_references_unavailable，不当作空集合放行 |

精确引用返回 409 document_in_use，不公开引用记录的正文或 ID。只扫描相关 Project，不以“项目有任意 Run”代替判定；旧数据不回写、不补 hash。当前扫描成本及真实 PostgreSQL 并发尚待验收。

新 Run 创建、调度保存与删除使用同一 Organization 门禁；调度创建/编辑在锁内重新校验文档元数据，避免“锁外验证成功 → 文档被删 → 保存悬空 ID”。认领只在 Schedule 锁内复制原配置，不反向获取 Organization；已有 Run 的输入回执、工作区和 Evidence 沿原冻结来源派生，不新增授权。任意 SQL、旧 writer 或混合版本不在该保证内。

### 持久清理仍待补齐

- 若需停止新选择，另设计可审计退役状态；不迁移旧 ID/hash，不用删除再上传模拟版本。
- 元数据持久记录待清理身份和精确对象；异步确认、失败重试，不按 bucket 前缀广泛删除。不存在、拒绝、断连、未知分别记录。
- 单文档与整 Project 共用清理协议；Run 副本和备份独立保留。当前没有该协议、状态 API 或修复 CLI。

## 页面与结果未知

目录上传是顺序的独立 POST，单份失败后继续；done 表示已处理，不是成功数。取消本地等待不能撤回已保存文件。

目标分别展示成功/确定拒绝/未知，保留原文件、Project、路径，不自动重发整目录，也不以同名列表项证明成功。上传/删除需同步防重复及 actor/Project/request 身份检查；切换、卸载后丢弃晚到响应。

删除使用共享请求门禁，确认前同步防重复，所有文档共用一个写入入口；不以删除 B 中断删除 A。只有合法 204 才结束本次删除等待；异常 2xx、超时/断连均保留原 ID 为未知，不宣称附件已彻底清理。

未知后人工 GET 原 ID：合法元数据或明确 document_not_found 404 只说明当前目录事实；Project 404、401/403 不算核对成功，并关闭新写入。读取成功后仍须人工解除门禁，解除不重发。刷新列表、预览和同名重传不替换原 ID；换 actor/CSRF/Project 或离页不接收旧响应、不承诺跨刷新恢复。归档项目只读。

client 已校验 UUID/带时区日期/checksum/安全整数、字段白名单及原 Project/ID，受控三语不显示内部错误。上传的持久原意图、完整部分成功/未知处理仍待补齐；未来分页须标明已加载范围。

## 开发接续与验收

入口：[documents route](../../PJM/backend/src/projectmind/api/routes/documents.py)、[存储实现](../../PJM/backend/src/projectmind/storage/)、[Web 管理组件](../../PJM/web/src/components/DocumentManagerPanel.tsx)、[契约索引](../../PJM/README.md#contracts)。当前缺口登记 R01/R10/R11；排障见[Runbook](../operations/runbook.md#文档保存与删除的只读分诊)。

- 实 DB 验证配额/同名竞争、撤权/归档/引用竞争和完整回滚。
- 在 put/commit/响应边界注入失败，重启后可确认原意图，不误删成功对象或重复创建。
- 专用 bucket 区分 S3 拒绝、超时、不存在；验证篡改、伪报 MIME、大文件与 Unicode header。
- 实组件 + mock API 验证目录部分成功、重复提交、切换、三语/键盘/窄屏；真实会话和存储另验。
- 历史 Run、物化副本与备份保持原身份；真实 DB/bucket 写入只使用明确授权的专用目标。
