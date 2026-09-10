# 项目文档的保存、读取与清理

本页负责文档资产；[资源快照](resource-snapshots.md)负责 Run 选择、冻结和副本，[项目生命周期](project-lifecycle.md)负责整项目操作。以下区分现状与修正要求，不是完整存储可靠性已交付的声明。

## 一个例子：列表消失不等于清理完成

上传 A 产生文档 ID 与 blob；Run 1 冻结 A 的 ID/hash。删除 A 当前先删元数据、再尝试删 blob；同路径重传 A′ 是新 ID，不能修复 Run 1 的原引用。

必须区分文档目录、存储字节、Run 冻结引用、物化副本：列表消失不证明字节清理，Run 仍显示 A 不证明可下载，删除报错也不证明元数据未提交。

## 身份、目录与公开面

ProjectDocument 元数据在 PostgreSQL，blob 用内部 storage_key 定位。document_id 是身份，folder/name 是展示路径；同 Project/路径唯一，不按内容去重。提供上传、列表、精确元数据读取、下载和删除，无覆盖、移动、改名、版本或恢复 API；目录由 folder 投影，空目录不保存。

| 操作 | 当前语义 |
| --- | --- |
| POST documents | 必填原 Idempotency-Key，单 file + 可选 folder 的 multipart；201 原发布元数据，同名 409 document_conflict |
| GET document-uploads/{upload_key} | 当前 actor 在原 Project 的上传记录；PENDING 未确认发布，PUBLISHED 带原发布回执，不是当前目录 |
| POST / GET document-uploads/{upload_key}/closure | 原作者显式停止待发布上传 / 查询独立关闭回执；不停止 PUT、不删 blob、不退配额 |
| GET documents | 按 folder/name 排序的完整数组，无查询/分页/Folder 契约 |
| GET document | 按 Project + 原 ID 读取当前元数据；不是原删除或清理回执 |
| GET document content | Project + 精确 ID 授权、核验实际 size/hash 后返回附件，不是公共 blob URL |
| DELETE document | 元数据 commit 后调用 storage，通常 204，不保证完整清理 |

文档公开数据只含身份、路径、size/MIME/checksum、上传者和时间。读用 ProjectReadActor，写用 ProjectWriteActor 且要求 ACTIVE；越权与不存在统一 404，已处理文档响应使用 no-store。上传、原请求查询和单文档删除另在业务事务复核会话与当前 Project/成员；这不等于全部存储清理与恢复已完成。

### 存储归属与配置切换

新文档通过 0037 保存原 namespace UUID、非秘密连接描述的 checksum 和后端是否持久化；它们与内部 key 一起传递，不进入公开元数据或 Run 清单。上传在 PUT 前后及发布前复核；普通下载、冻结读取和删除只使用与原身份相符的已配置 client，不从 DB 值生成 endpoint 或注入凭据。

S3 的 `PROJECTMIND_OBJECT_STORAGE_NAMESPACE_ID` 是部署显式指定的存储世代 UUID；同一原存储的 API/Worker 保持一致，重建存储必须使用新 UUID。描述摘要覆盖规范化 HTTP(S) endpoint、bucket 和固定空前缀，不含 access/secret key，凭据轮换不改身份。endpoint 拒绝 userinfo、路径前缀、查询、fragment 与含糊 authority，不再静默忽略这些成分。memory 每实例生成独立非持久身份，不能冒充重启恢复。

未配置、归属不匹配或旧行未绑定时，文档 blob 操作返回 503 document_storage_unavailable；列表与原 ID 元数据仍可读。单文档删除在修改元数据前拒绝已知归属问题。0037 不用今天的配置回填旧数据，不改原 ID/key/hash；旧资产的受控核验与归属迁移工具仍待实现，不手写当前摘要或重传同名文件代替核验。

这是配置身份边界，不是服务端 bucket 身份证明：同 endpoint/bucket 重建后错误复用 UUID、管理员改对象、版本保留和迟到 PUT 仍需受保护的 namespace marker、精确对象版本与清理协议。当前只解析单一已配置 namespace；SkillSource 等其他 blob 消费者尚未接此持久归属，不能由文档链路推导全部存储已受保护。

## 上传的三个边界

[上传入口](../../PJM/backend/src/projectmind/api/routes/documents.py)先完成 Origin/CSRF、会话与 Project 授权，再读取 multipart；不使用会在 dependency 前解析正文的 File/Form 参数。公开请求仍为一个 file 和可选一个 folder，重复、未知字段、文件/普通字段混用与不完整结束边界返回 422 invalid_document_upload。

正文用现有 multipart 引擎流式解析，不创建临时文件或后台写线程。文件实际字节受配置上限约束；整个请求最多为该上限 + 16 KiB，分块、缺失/低报 Content-Length 与结束边界后的 epilogue 都计数。超限在追加 buffer 前返回 413 document_upload_too_large，不进入存储。header 合计上限 16 KiB，folder 至多 1,024 bytes，再由业务规则限制字符数；filename/folder 使用严格 UTF-8，拒绝歧义参数、扩展参数和压缩 envelope。

接收完成后交付不可变 bytes；取消与断连直接结束接收，不被解释为成功。每请求有界缓冲不等于全进程内存、并发数、慢请求或代理缓冲预算。

### 上传的授权事务

[DocumentService](../../PJM/backend/src/projectmind/documents/service.py)在首次 await 前复制正文，规范化路径并校验大小/MIME/文本策略：

1. TX A：复核原授权与 ACTIVE，按 actor/Project/key 查询原意图；首次才检查目录/待发布路径和配额，保存原意图及占用，commit 后释放锁。
2. 锁外 PUT：只有确认首次预约 commit 的调用可写对象一次，核验回执与原 key/size/MIME/hash。提交未知或同键重发不进入 PUT。
3. TX B：复核同一原授权、原预约未关闭、ACTIVE 与最新占用；创建关联元数据并保存 PUBLISHED 回执，flush、最终授权，再 commit 并返回 201。

两个事务都采用 Organization UPDATE → 当前 User SHARE → 原 AuthSession UPDATE → Project/成员 SHARE。锁等待、读取和 flush 后重新取时间复核原会话，不另选同用户新登录；归档、成员移除、停用、撤销或项目删除后不得再发布元数据。上传者从原 access 取得，不由调用者另传 UUID。存储 I/O 不持有这些锁。

| 当前检查 | 保证范围与缺口 |
| --- | --- |
| 文件默认 25 MiB，Project 默认 500 MiB | 占用为全部上传意图 size + 未关联旧元数据 size + 未关联旧清理记录 size；不是 bucket 实际字节统计 |
| 名称、folder 最多 200 字符 | 名称还须通过共享 storage key 的单段 128 限制；129–200 名称在 PUT 前稳定返回 422 invalid_document_name，不截断 |
| MIME allowlist、指定文本凭据扫描 | 校验申报类型，不证明真实格式、病毒安全或任意二进制已脱敏 |
| 预约和最终发布共用组织门禁 | 新 PUT 前已计入占用；发布不重复计费，旧负数元数据拒绝而不抵扣。待发布路径也阻止同名新 PUT |
| 唯一约束作为最后保护 | 精确元数据路径冲突映射 document_conflict；其他 DB 异常不冒充同名拒绝。PUT 后失败保留预约与可能存在的对象 |

PUT adapter 回执须与原请求一致，不能让任意 key/大小/hash 决定保存身份。协议记录为 UNCONDITIONAL_V1：一次 application PUT 不是一次 wire PUT，不排除 SDK 重试、multipart 或迟到写入。拒绝、取消、存储错误和 commit 未知不补偿删除或释放占用；旧 PUT 可能仍在运行，DB 也可能已提交。精确路径约束在事务内映射冲突前复核期限；failed flush 使用先前锁定的授权字段快照，仅作拒绝分类，避免失败 ORM 隐式查询，不凭快照继续写入。门禁仅约束本版 writer；旧 API、任意 SQL、真实 PostgreSQL 并发及完整恢复仍需独立验收。

### 原上传身份与查询

Idempotency-Key 必须是单个非 nil UUID；缺失、重复或非法值在授权后、读取正文前返回 422 invalid_document_upload_key。0038 在 PUT 前保存组织/Project/actor/key、服务器 request UUID、原会话 ID、规范化输入摘要、目标 document ID、原 namespace/key、字节占用与时间；不保存正文或凭据。原输入摘要覆盖协议版、组织/Project/actor、folder/name、size/MIME/正文 hash，不含后来请求或会话 ID。

| 原记录 | 同键 POST / GET 的含义 |
| --- | --- |
| 无记录 | POST 可首次预约；GET 返回 404 document_upload_not_found，只表示当前未查到，不证明旧 POST 以后不会到达或提交 |
| PENDING | 同内容 POST 返回 409 document_upload_pending；已显式关闭则 document_upload_closed，均不再 PUT。GET 仍为 200、document=null，不断言对象有无 |
| PUBLISHED | 同内容 POST 返回原 201 元数据；GET 200 带原九字段回执。原文档后来被删也不改写回执、不重建目录 |
| 同 key 不同输入摘要 | POST 返回 409 document_upload_key_conflict，不占新配额、不写对象 |
| 记录损坏或无法核实 | 503 document_upload_unavailable，不补造成功、空记录或新身份 |

POST 重放仍须通过当前接收/上传策略与 ACTIVE 授权；无需当前存储可达，也不重新计费。只读 GET 不重新检查文件策略，允许同 actor 的新有效会话查询旧记录，也允许授权归档项目；不能查看其他 actor 的原请求，ADMIN 也不例外。查到、未命中与拒绝均经过最终授权复核。恢复页面只凭原 UUID 读取，不用文件名或当前列表猜测结果。

本版不自动过期或删除意图，已发布和元数据已删的占用均保留；副本、备份、旧孤立对象和历史版本不因此变成已精确计量。可靠清理与占用结算仍待补齐，不能以长时间无响应、metadata 删除或一次对象不存在释放额度。0038 不给旧文档补造上传历史，任何意图或关联都阻止降级丢表/列。

### 显式停止待发布上传

关闭是原作者对原 Project/key 的独立操作。POST `document-uploads/{upload_key}/closure` 必须显式提交 `{"confirmation":"STOP_PUBLICATION"}`；当前有效会话、CSRF、ACTIVE 与成员资格按上传事务复核。同 actor 新登录可以发起这次关闭，但不能接管旧 PUT；ADMIN 也不能关闭别人的 key。

| 竞争结果 | 原事实与响应 |
| --- | --- |
| 关闭先提交 | 201 独立回执；同目标重放 200、原回执不变。TX A/TX B 拒绝 document_upload_closed，不能再发布 |
| 发布先提交 | 409 document_upload_already_published，不改 PUBLISHED 回执、不自动删除文档 |
| 关闭提交未知 | 按同一 URL GET 核对；200 才确认关闭，404 document_upload_closure_not_found 不证明旧 POST 以后不会提交，不自动重发 |

独立[回执契约](../../PJM/contracts/documents/v1/upload-closure.schema.json)严格含 upload_key、project_id、document_id、closed_at、publication_state=CLOSED；不向旧五字段上传响应扩展状态。原上传仍投影 PENDING/null，未发布目标不补造发布日期。GET 使用当前原作者的只读授权，允许新有效会话与归档，查到、未命中及拒绝都经过最终资格复核。

0042 将原意图的 publication_closed_at 与独立 document_upload_closures 审计同事务保存。审计关联原意图，独立 checksum 绑定原 key、私有 namespace/key、原作者/请求/会话/时间/内容及本次关闭身份；不改 v1 输入摘要、不保存正文。缺失、孤立或不匹配的标记/审计返回 document_upload_unavailable，不补签。关闭后释放展示路径供新 key/新 ID 使用，但原 size 仍计入占用；关闭记录、原意图和对象身份不删除。

DB CHECK 阻止带关闭标记的意图变为 PUBLISHED，为忽略新列的旧发布 SQL 留下回滚保护；它不阻止旧 PUT，也不代替全实例停写与成套发布。0042 有任意关闭标记或审计即拒绝降级。真实 PostgreSQL 竞争、迁移与恢复仍须独立验收。

## 读取、下载与预览

普通下载和冻结来源使用同一实际字节校验；按原 ID 取元数据，锁外读取该对象，确认 size/SHA-256 后才返回完整内容。冻结读取额外比对原快照的 ID、路径、MIME 与 hash，不使用同路径新文件替代。

S3 在同一次 GET 中最多读取声明 size + 1 bytes 判定超限，不先 stat 再读；连接、读取、关闭均在线程内，不阻塞事件循环。取消等待不证明 SDK 线程或远端已停止，线程仍负责关闭自己的响应。这个边界不等于全服务内存、并发或网络超时预算。

| 下载结果 | 稳定响应与处理 |
| --- | --- |
| 元数据不存在或越权 | 原有 404；不通过存储枚举补查身份 |
| 元数据仍在、原 blob 缺失 | 409 document_content_missing；不按同名文件补齐 |
| 实际大小/hash 不符或元数据不合法 | 409 document_content_invalid；不返回部分内容或重算保存 hash |
| 原归属未绑定/不匹配、S3 拒绝或读取故障 | 503 document_storage_unavailable；不当作文件不存在，不公开 namespace/key/SDK 错误 |

附件始终带 no-store、nosniff 与安全 Content-Disposition；保留合法 MIME，非法值降级为 octet-stream。文件名用单段 ASCII fallback 与 UTF-8 filename*，不直接拼接中文、引号或控制字符，不改数据库原名。

Web 仅预览 txt/md/markdown/htm/html，统一上限 1,000,000 bytes。列表 size 只用于禁用按钮；client 只接受 200，按实际响应流计数，缺失或低报 Content-Length 不绕过上限，错误正文也有界。超限即停止等待并提示下载，不展示截断片段；UTF-8 无法解码时明确失败。

Markdown/文本保持原文。HTML 在无浏览上下文的 template 中解析，只重建静态正文/基础结构白名单；不保留脚本、上传 CSS、表单、嵌套 frame、外部资源、URL/事件属性。图片只留下已有替代文字，链接只显示文本；超过 20,000 个解析节点则安全显示原文，不递归截断内容。

重建后的 srcDoc 在正文之前设置[默认拒绝的 CSP](https://www.w3.org/TR/CSP3/#meta-element)，只允许平台固定显示 CSS；iframe 使用 sandbox="" 与 no-referrer。页面明确提示预览会移除主动内容，原始文件仍可下载。这是静态文档预览，不是 generated Host，也不保证下载后在其他应用打开原文件的安全。

预览共用 30 秒读取门禁。关闭、同 tick 换文件、换 actor/会话/Project 时立即失效原请求；晚到正文或 401 不影响新上下文。当前读取的 401/403/Project 404 关闭写入资格，不被列表刷新重新打开；预览结果不解除未知删除。

## 删除与历史引用

单文档删除复核引用、原会话和存储归属。0039 为新旧文档同事务保存独立清理要求，再删元数据；关联上传意图同时记录 cleanup_requested_at。commit 后才尝试同 namespace 原 key 的 storage 删除。storage 失败仍会留下 blob，原 document ID 再删先得 404；持久要求不等于清理成功回执或已接异步重试。

清理记录保存原 document ID、完整元数据、namespace/key，以及本次 DELETE 的组织/操作者/request/session ID 与时间，不存凭据。来源分为 UPLOAD_INTENT_V1 与 LEGACY_UNVERIFIED：前者关联原意图且不重复计费；后者将旧元数据 size 同事务转入清理占用，不伪造旧 upload key/会话或停止证明。未绑定/不安全/损坏的原目标仍在删除前拒绝，不以今天的配置补齐。

有任意文档、上传意图或清理记录的 Project 返回 409 project_delete_blocked_by_document_uploads；文档不再进入配置批量删除清单。必须先经单文档协议处理资产，剩余审计/清理记录仍阻止物理删除，不能以整项目操作清空占用。0039 任何清理记录都阻止降级；新旧元数据删除、清理要求和关联更新一起提交或回滚，上传原发布回执不变。

S3 的 get/stat/exists/delete 仅将明确 NoSuchKey/NoSuchObject 视为对象不存在；NoSuchBucket、AccessDenied、SDK/HTTP/连接故障均为不可用。PUT 的失败不证明没有写入，取消不证明远端停止。上传或删除的存储故障返回静态 503，Web 仍按写结果未知处理；删除可能已提交元数据，不能把 503 当作回滚。SDK 重试、multipart 与无条件 PUT 尚未改为可阻止迟到写入的协议；204 不证明历史版本、在途 PUT 或全部字节已经清理。

### 删除事务与引用判定

```text
Organization UPDATE → 当前 User SHARE → 原 AuthSession UPDATE
  → Project / 成员 SHARE → 原 Document UPDATE
  → 校验历史引用 → 有关联则原 Upload UPDATE / 标记清理
  → 保存独立 Cleanup 要求（旧占用转入）→ 删除元数据
  → flush → 最终授权 → commit
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

清理链须分开保存三种事实；当前已接独立停止发布与已发布文档的清理要求，清理成功/结算 API 和修复 CLI 尚未实现。

| 顺序与事实 | 实施要求 | 不能据此推导 |
| --- | --- | --- |
| 停止发布 | 使用[独立关闭协议](#显式停止待发布上传)，与 TX B 共用授权和组织门禁；发布先完成则冲突 | 旧 PUT 已停止、对象不存在或额度可退 |
| 封闭存储写入并精确清理 | 核实原 namespace、对象版本和阻止迟到写的持久证明，锁外操作；拒绝、断连和未知可按原目标恢复，不做前缀清空 | 一次 DELETE/HEAD、DB lease 或本地取消已构成证明 |
| 结算原占用 | 有效清理执行权与证明同事务绑定原目标，只结算一次；commit 未知先读原记录 | 可以删除上传/清理审计、修改 PUBLISHED 回执或放行整个项目删除 |

停止发布/清理使用独立协议，不向现有严格五字段上传响应追加状态。待发布目标没有发布日期，不套用已发布元数据补造它；新协议绑定原 key 和私有目标，保留 v1 输入摘要与历史回执。旧 UNCONDITIONAL_V1 必须另有全部旧写入者停止与归属核验证据才能结算，不按等待时长自动收养为新协议。

精确清理/结算的服务端前提须先落实，不能只给 client 增加条件 header。当前 Compose 固定的 MinIO 版本依赖 [policy v3.1.3](https://github.com/minio/minio/blob/RELEASE.2025-09-07T16-13-09Z/go.mod#L62)，其[条件键集合](https://github.com/minio/pkg/blob/v3.1.3/policy/condition/keyname.go#L225)不含 `s3:if-none-match` / `s3:if-match`，不能照搬 AWS 策略限制无条件写。该版 [PUT 条件检查](https://github.com/minio/minio/blob/RELEASE.2025-09-07T16-13-09Z/cmd/erasure-object.go#L1176)也未将读仲裁错误一律拒绝；据此不能认定仲裁未知时封闭性已经成立，需受控服务端方案与故障验收。

新 namespace 还需受保护的服务端 marker、与旧通用 put/delete 隔离的写入权限及全对象版本保证；[前缀/目录排除](https://github.com/minio/minio/blob/RELEASE.2025-09-07T16-13-09Z/internal/bucket/versioning/versioning.go#L91)说明仅检查 versioning=Enabled 不够。启动只能核对既有身份，不能缺 marker 就自动创建或用配置布尔值冒充验收。补齐这些条件后再接 Worker 重试、结算及备份恢复；长期保留占用仍是缺口，不是完整清理方案。Run 副本、备份与停止新选择的退役规则分别评审，不改旧 ID/hash。

## 页面与结果未知

目录上传按文件顺序独立 POST，选择时固定原 actor/Project、每份 UUID、File 与路径；分别显示已发布、确定拒绝、未知与未发送。确定的文件拒绝可继续下一份；未知或资格拒绝立即暂停，后续文件保持未发送。取消等待、30 秒期限、损坏成功响应或断连不撤回服务端操作，也不自动重发。

未知只经原 key 的人工 GET 核对：PENDING、404、503 或取消查询不解除未知；确认原 PUBLISHED 后仍须点击继续，才发送尚未发送的文件。刷新列表和同名项不代替原回执。原 UUID 可选取复制，刷新后可人工输入进行只读恢复；不把 File、Cookie/CSRF 或正文保存到浏览器存储，不承诺恢复旧批次或重传丢失文件。

手动输入 key 的只读查询与真实上传批次分开：可取消等待、结束查询或换 key，保留原批次的 File、未发送项和未知状态，不生成 POST。只读查询即使查到同 key 的 PUBLISHED 也不替代批次内的原请求核对；退出它不解除真实未知写入。查询只保存于当前页面内存，新上传会关闭旧查询响应的接收权；取消/换 key 后的晚到成功或 401 不影响新操作。

未知批次另可确认停止原 key 的发布。关闭 POST 未知时仍锁住原批次，只允许查询其独立关闭回执；合法 CLOSED 才标记该项已关闭，再由用户显式继续未发送项。关闭冲突不代替原 PUBLISHED 查询；404、503、取消与晚到响应均不解除未知。手工输入的关闭回执查询也独立展示，不替代真实批次内的原 key 核对，不重传 File 或自动删除文档。

刷新或新会话后，手动原上传查询确认 PENDING 的 key 可另行显式确认关闭，不创建假 File 或假批次。它使用同一关闭协议与写入门禁；未知后核对本次原目标，成功只结束独立操作，不自动恢复旧批次。当前页面已有真实未知批次时，不另开其他 key 的关闭写入。任何新批次动作、关闭确认/提交/核对或删除确认都同步使旧手动查询失效，晚到响应不影响新动作。

上传和删除共用同步写入门禁，确认删除时也不允许开始上传；换 actor/会话/Project 或离页丢弃旧响应。新有效会话可以重新人工查询原 key，不能借此重放旧写入。归档只读查询，不继续未发送批次。

删除使用共享请求门禁，确认前同步防重复，所有文档共用一个写入入口；不以删除 B 中断删除 A。只有合法 204 才结束本次删除等待；异常 2xx、超时/断连均保留原 ID 为未知，不宣称附件已彻底清理。

未知后人工 GET 原 ID：合法元数据或明确 document_not_found 404 只说明当前目录事实；Project 404、401/403 不算核对成功，并关闭新写入。读取成功后仍须人工解除门禁，解除不重发。刷新列表、预览和同名重传不替换原 ID；换 actor/CSRF/Project 或离页不接收旧响应、不承诺跨刷新恢复。归档项目只读。

列表、预览与原请求核对的资格通知都须在当前请求及绝对期限检查后生效，同一页面 owner 只通知一次会话失效。归档只关闭写入，不覆盖先前的会话/所属拒绝，也不禁止获授权的原 ID/key 读取；核对失败结束等待，但仍保留原 DELETE 未知。

client 校验 UUID/带时区日期/checksum/安全整数、字段白名单及原 Project/ID，原上传查询另核 key/state/回执。受控三语不显示内部错误；未来分页须标明已加载范围。

## 开发接续与验收

入口：[documents route](../../PJM/backend/src/projectmind/api/routes/documents.py)、[存储实现](../../PJM/backend/src/projectmind/storage/)、[Web 管理组件](../../PJM/web/src/components/DocumentManagerPanel.tsx)、[契约索引](../../PJM/README.md#contracts)。当前缺口登记 R01/R10/R11；排障见[Runbook](../operations/runbook.md#文档保存与删除的只读分诊)。

- 实 DB 验证配额/同名竞争、撤权/归档/引用竞争和完整回滚。
- 在 put/commit/响应边界注入失败，重启后可确认原意图，不误删成功对象或重复创建。
- 专用 bucket 区分 S3 拒绝、超时、不存在；验证篡改、伪报 MIME、大文件与 Unicode header。
- 实组件 + mock API 验证目录部分成功、重复提交、切换、三语/键盘/窄屏；真实会话和存储另验。
- 历史 Run、物化副本与备份保持原身份；真实 DB/bucket 写入只使用明确授权的专用目标。
