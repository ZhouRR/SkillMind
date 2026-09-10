# 项目文档的保存、读取与清理

本页负责文档资产；[资源快照](resource-snapshots.md)负责 Run 冻结/副本，[项目生命周期](project-lifecycle.md)负责整项目操作。当前缺口见[计划 R01/R10/R11](../planning/roadmap.md)，下述局部实现不代表完整存储可靠性。

## 一个例子：列表消失不等于清理完成

A 的 document ID/hash 被 Run 冻结后，删除元数据再删 blob 是两步；同路径重传 A′ 会产生新 ID，不能修复原引用。目录、存储字节、冻结引用、物化副本须分别确认：列表消失不证明清理，Run 显示引用不证明可下载，删除报错不证明元数据回滚。

## 身份、目录与公开面

ProjectDocument 元数据在 PostgreSQL，内部 storage_key 定位 blob。document_id 是身份，folder/name 仅展示路径；同 Project/路径唯一、不按内容去重。无覆盖、移动、改名、版本或恢复 API；目录由 folder 投影，不保存空目录。

| 操作 | 语义 |
| --- | --- |
| POST documents | 原 Idempotency-Key + 单 file/可选 folder；201 原发布元数据，同名 409 document_conflict |
| GET document-uploads/{upload_key} | 原 actor/Project 上传记录；PENDING 未确认发布，PUBLISHED 返回历史回执，不是当前目录 |
| POST / GET document-uploads/{upload_key}/closure | 原作者显式停止发布 / 查询关闭回执；不停止 PUT、删 blob 或退配额 |
| GET documents / document | 完整数组按 folder/name 排序 / Project + 原 ID 当前元数据；无分页/Folder 契约，不是删除回执 |
| GET document content | 精确 ID 授权并核验实际 size/hash 后返回附件，不公开 blob URL |
| DELETE document | 元数据 commit 后尝试删 blob；204 不保证完整清理 |

公开字段仅身份、路径、size/MIME/checksum、上传者/时间；读用 ProjectReadActor，写用要求 ACTIVE 的 ProjectWriteActor，越权/不存在统一 404，已处理响应 no-store。上传、原请求查询和删除另经业务事务复核当前会话/Project/成员。

### 存储归属与配置切换

0037 为新文档保存 namespace UUID、非秘密连接描述 checksum、后端持久性；与 key 一起传递，不进入公开元数据/Run 清单。PUT 前后及发布前复核，下载/冻结读取/删除只用原身份匹配的已配置 client，不由 DB 构造 endpoint 或凭据。

S3 的 SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID 是存储世代 UUID：同原存储的 API/Worker 一致，重建必须换 UUID。摘要含规范化 HTTP(S) endpoint、bucket、固定空前缀，不含凭据，轮换不改身份；endpoint 拒绝 userinfo、路径前缀、query、fragment、含糊 authority。memory 每实例独立非持久身份。

未配置、归属不符或旧行未绑定，blob 操作为 503 document_storage_unavailable，列表/精确元数据仍可读；删除须在改元数据前拒绝。0037 不以当前配置回填旧 ID/key/hash，旧资产核验/归属迁移工具尚未实现。

配置 UUID 不是服务端 bucket 身份证明；同地址重建误复用 UUID、对象篡改/版本和迟到 PUT 仍需 marker/精确清理协议。当前只解析单 namespace，SkillSource 等消费者未接持久归属。

## 上传的三个边界

[入口](../../SKM/backend/src/skillmind/api/routes/documents.py)先验 Origin/CSRF、会话与 Project，再读 multipart，不用 dependency 前解析正文的 File/Form。只允许一个 file、可选一个 folder；重复/未知字段、类型混用、不完整结束返回 422 invalid_document_upload。

现有 multipart 引擎流式解析，不建临时文件/后台写线程。实际文件 bytes 受配置上限约束，整请求最多上限 + 16 KiB，header 合计 16 KiB、folder 1,024 bytes；分块、缺失/低报 Content-Length、epilogue 均计数，追加 buffer 前超限返回 413 document_upload_too_large。filename/folder 严格 UTF-8，拒绝歧义/扩展参数和压缩 envelope；业务另限字符数。

完成后交付不可变 bytes，断连/取消直接传播。每请求有界不等于全进程内存、并发、慢请求或代理预算。

### 上传的授权事务

[DocumentService](../../SKM/backend/src/skillmind/documents/service.py)首次 await 前复制正文，规范化路径并验大小/MIME/文本策略：

| 阶段 | 提交边界 |
| --- | --- |
| TX A | 原授权/ACTIVE 下按 actor/Project/key 查意图；首次才验目录/待发布路径/配额，保存原意图与占用并 commit |
| 锁外 PUT | 仅确认首次预约 commit 的调用可 application PUT 一次；同键重发/提交未知不 PUT，回执须匹配原 key/size/MIME/hash |
| TX B | 同一原授权、未关闭预约、ACTIVE/最新占用下创建元数据与 PUBLISHED 回执，flush/最终授权后 commit，返回 201 |

两个事务锁序均为 Organization UPDATE → User SHARE → 原 AuthSession UPDATE → Project/成员 SHARE。等锁、读取、flush 后取新时间复核原会话，不换同用户新登录；上传者从原 access 取得。撤销/到期、停用/移除成员、归档/项目删除后不得发布；存储 I/O 不持上述锁。

| 校验 | 保证范围 |
| --- | --- |
| 默认单文件 25 MiB、Project 500 MiB | 占用 = 全部上传意图 + 未关联旧元数据 + 未关联旧清理记录 size，不是 bucket 实际字节 |
| 名称/folder 最多 200 字符 | 名称另受共享 key 单段 128 限制；129–200 在 PUT 前 422 invalid_document_name，不截断 |
| MIME allowlist、指定文本凭据扫描 | 仅申报类型/指定文本，不证明真实格式、病毒安全或任意二进制脱敏 |
| 组织 gate | PUT 前预约，发布不双计；旧负 size 拒绝不抵扣，PENDING 路径阻止同名新 PUT |
| 唯一约束 | 仅精确路径冲突映射 document_conflict；其他 DB 异常不冒充同名，PUT 后失败保留意图与可能对象 |

UNCONDITIONAL_V1 的一次 application PUT 不等于一次 wire PUT，仍可能 SDK 重试/multipart/迟到写。拒绝、取消、存储错误、commit 未知均不补偿删除或释放占用。

路径冲突返回前复核期限；failed flush 只以锁内授权副本作拒绝分类，禁止失效 ORM 隐式查询或凭副本续写。上述门禁限本版 writer，不覆盖任意 SQL/旧实例。

### 原上传身份与查询

Idempotency-Key 必须单个非 nil UUID；缺失/重复/非法在授权后、正文前返回 422 invalid_document_upload_key。0038 的原意图绑定组织/Project/actor/key、服务器 request UUID、原会话、规范化输入摘要、目标 document ID、namespace/key、占用/时间，不存正文/凭据。摘要含协议版、组织/Project/actor、folder/name、size/MIME/hash，不含后来请求或会话 ID。

| 原记录 | 同键 POST / GET |
| --- | --- |
| 无 | POST 可预约；GET 404 document_upload_not_found 不证明旧请求以后不会提交 |
| PENDING | 同内容 POST 409 document_upload_pending，已关闭为 document_upload_closed，均不 PUT；GET 200、document=null，不断言对象有无 |
| PUBLISHED | 同内容 POST 原 201，GET 原九字段回执；文档后来删除不改回执、不重建目录 |
| 同 key 不同摘要 | POST 409 document_upload_key_conflict，无新占用/PUT |
| 损坏/不可核实 | 503 document_upload_unavailable，不补造事实 |

POST 重放仍验当前接收策略/ACTIVE，但不要求存储可达或重新计费。GET 不复验文件策略，允许同 actor 新有效会话和授权归档，不允许其他 actor（含 ADMIN）查询；查到/未中/拒绝都过最终授权。不以文件名或列表推断原请求结果。

意图不自动过期/删除；已发布或元数据已删仍保留占用，不代表副本/备份/孤立对象/历史版本已精确计量。无响应、metadata 删除或一次对象不存在均不足以结算。0038 不补造旧上传历史，任意意图/关联阻止降级。

### 显式停止待发布上传

原作者向原 Project/key POST closure，正文必须为 {"confirmation":"STOP_PUBLICATION"}；按上传事务复核当前会话/CSRF/ACTIVE/成员。同 actor 新会话可关闭但不接管旧 PUT，ADMIN 不代关他人 key。

| 竞争结果 | 响应 |
| --- | --- |
| 关闭先提交 | 首次 201、同目标重放 200，原回执不变；TX A/TX B 拒绝 document_upload_closed |
| 发布先提交 | 409 document_upload_already_published，不改发布回执或自动删文档 |
| 关闭提交未知 | GET 同 URL；200 才确认，404 document_upload_closure_not_found 不证明旧 POST 不会提交，不自动重发 |

[独立回执](../../SKM/contracts/documents/v1/upload-closure.schema.json)严格包含 upload_key、project_id、document_id、closed_at、publication_state=CLOSED，不扩旧五字段响应。原上传仍为 PENDING/null，不补发布日期。GET 按原作者当前只读授权，可新会话/归档，全部路径过最终门禁。

0042 原子保存 publication_closed_at 和 document_upload_closures 审计；独立 checksum 绑定原 key、私有目标、原作者/request/session/时间/内容及本次关闭身份，不改 v1 摘要、不存正文。缺失/孤立/不匹配为 document_upload_unavailable，不补签。

关闭释放展示路径给新 key/ID，但保留原占用、意图、关闭审计与对象身份。DB CHECK 阻止关闭后 PUBLISHED，能使忽略新列的旧发布 SQL 回滚，却不能阻止旧 PUT 或代替全实例停写。任意关闭标记/审计阻止 0042 降级。

## 读取、下载与预览

普通下载和冻结读取共用实际字节校验：按原 ID 取元数据，锁外读对象，size/SHA-256 一致才返回完整内容；冻结另比原快照 ID/路径/MIME/hash，不以同路径新文件替代。

S3 同一次 GET 最多读取声明 size + 1 bytes，不先 stat；连接/读取/关闭在线程内，线程自行关闭响应。取消等待不证明线程/远端已停，也不等于全服务内存/并发/网络 deadline。

| 结果 | 响应 |
| --- | --- |
| 元数据不存在/越权 | 404，不通过存储枚举补身份 |
| 原 blob 缺失 | 409 document_content_missing，不同名补齐 |
| 实际 size/hash 不符、元数据非法 | 409 document_content_invalid，无部分返回或重算保存 hash |
| 归属未绑定/不符、S3 拒绝/读取故障 | 静态 503 document_storage_unavailable，不暴露 namespace/key/SDK 错误 |

附件带 no-store、nosniff、安全 Content-Disposition；合法 MIME 保留，非法降 octet-stream。文件名用单段 ASCII fallback + UTF-8 filename*，不拼原引号/控制字符，不改 DB 原名。

Web 预览仅 txt/md/markdown/htm/html，最多 1,000,000 实际 bytes，只接受 200，错误正文也有界。列表 size 仅作按钮提示，Content-Length 不可信；超限停止并提示下载，不显示截断片段，UTF-8 错误明确拒绝。

文本/Markdown 保留原文；HTML 在无浏览上下文 template 解析，只重建静态结构白名单，无脚本、上传 CSS、表单/frame、外部资源、URL/事件属性；图片留 alt、链接留文本。超过 20,000 节点安全显示原文，不递归截断。

srcDoc 正文前设置[默认拒绝 CSP](https://www.w3.org/TR/CSP3/#meta-element)，仅允许平台固定 CSS；iframe sandbox=""、no-referrer。页面说明主动内容已移除、原文件仍可下载；这不是 generated Host，也不保证其他应用打开原文件安全。

预览读取期限 30 秒；关闭/同 tick 换文件、换 actor/会话/Project 立即作废旧请求，晚到正文/401 不影响新上下文。当前 401/403/Project 404 关闭写资格，列表刷新不重开，预览不解除未知删除。

## 删除与历史引用

0039 在原授权/引用/归属检查后，同事务保存独立清理要求、关联意图的 cleanup_requested_at，再删元数据；commit 后才尝试删同 namespace 原 key。失败可留 blob，重删原 ID 得 404；持久要求不是清理回执或异步重试。

清理要求保存原 ID/元数据/namespace/key、本次 DELETE 组织/actor/request/session/时间，无凭据：

| 来源 | 占用与历史 |
| --- | --- |
| UPLOAD_INTENT_V1 | 关联原意图，不双计 |
| LEGACY_UNVERIFIED | 旧元数据 size 原子转入清理占用，不造 upload key/会话/停止证明 |

未绑定、不安全或损坏目标仍在删除前拒绝，不用当前配置补齐。新旧元数据删除、要求和关联更新原子提交，PUBLISHED 回执不变；任意清理要求阻止 0039 降级。

文档不进入整项目配置批删；任意文档/意图/清理记录均触发 project_delete_blocked_by_document_uploads，不能清空审计/占用释放项目。

S3 get/stat/exists/delete 仅明确 NoSuchKey/NoSuchObject 算不存在；NoSuchBucket、AccessDenied、SDK/HTTP/连接故障均不可用。PUT 失败/取消不证明无写入或已停止；上传/删除存储故障为静态 503，页面仍按未知处理。204 不证明历史版本、在途 PUT 或所有字节清理，SDK 重试/multipart/无条件 PUT 尚未被封闭。

### 删除事务与引用判定

```text
Organization UPDATE → User SHARE → 原 AuthSession UPDATE
  → Project/成员 SHARE → 原 Document UPDATE
  → 历史引用 → 关联 Upload UPDATE/标记清理
  → 独立 Cleanup（旧占用转入）→ 删除元数据
  → flush → 最终授权 → commit → 锁外删原 blob
```

只用原 cookie/CSRF、服务器 request UUID，不选新会话。等锁、引用读取和最终 flush 后复核；失效回滚。DB 异常、取消、commit 未知不调用 blob 删除、不自动重发。

| 保存事实 | 引用判定 |
| --- | --- |
| 任意状态 Run 的可信文档快照 | 原请求/Project/槽位/成员/hash 一致后保护冻结 ID；终态/已存副本不解除 |
| 任意状态 Schedule 的 SINGLE/SET | 保护显式原 ID，暂停/归档/未触发不解除 |
| 保留 occurrence 的 SINGLE/SET | 校验原快照/checksum/索引/父身份；旧配置、SETTLED 无 Run 审计仍保护 |
| Schedule/occurrence 动态 ALL | 未展开规则不永久占当前目录；已建 Run 只保护冻结成员 |
| 原可选槽未选、合法无文档 Run | 无引用；任意输入 UUID 不算引用 |
| 旧 provider 含义不明、快照缺损 | 409 document_references_unavailable，不当空集合 |

精确引用为 409 document_in_use，不公开引用正文/ID。仅扫相关 Project，不以“有任意 Run”代判，不回写旧数据/hash；扫描成本和真实并发仍待验。

新建 Run、调度保存、删除共用 Organization gate；调度创建/编辑锁内重验元数据，防止锁外验证后被删。认领只在 Schedule 锁内复制配置，不反向取组织锁；已有输入回执/工作区/Evidence 沿冻结来源派生，不扩权。旧 writer/任意 SQL/混跑不受此保证。

### 持久清理仍待补齐

已实现独立停止发布和删除后的清理要求；清理成功/结算 API、修复 CLI 尚未实现。必须分开保存：

| 事实 | 必要证明 |
| --- | --- |
| 停止发布 | [关闭协议](#显式停止待发布上传)与 TX B 共用门禁；不证明 PUT 已停/对象不存在/可退额度 |
| 封闭写入、精确清理 | 原 namespace/对象版本及阻止迟到写的持久证明，锁外执行、按原目标恢复，不前缀清空；DELETE/HEAD、DB lease、本地取消不够 |
| 结算原占用 | 有效执行权与清理证明同事务绑定原目标，只结算一次，commit 未知先读；不删审计、改发布回执或放行整项目删除 |

新协议绑定原 key/私有目标，保留 v1 摘要及旧严格响应，不补造未发布元数据。旧 UNCONDITIONAL_V1 另须全部旧 writer 停止与归属核验证据，不按等待时长自动收养。

服务端封闭前提不能由 client 条件 header 代替。当前 Compose 固定 MinIO 使用 [policy v3.1.3](https://github.com/minio/minio/blob/RELEASE.2025-09-07T16-13-09Z/go.mod#L62)，[条件键](https://github.com/minio/pkg/blob/v3.1.3/policy/condition/keyname.go#L225)不含 s3:if-none-match / s3:if-match，不能照搬 AWS 策略限制无条件写；[PUT 条件检查](https://github.com/minio/minio/blob/RELEASE.2025-09-07T16-13-09Z/cmd/erasure-object.go#L1176)也未对读仲裁错误一律拒绝，不能认定未知时已封闭。

新 namespace 需受保护的服务端 marker、与旧 put/delete 隔离的权限、全对象版本保证；[前缀/目录排除](https://github.com/minio/minio/blob/RELEASE.2025-09-07T16-13-09Z/internal/bucket/versioning/versioning.go#L91)使仅验 versioning=Enabled 不足。启动只核对已有 marker，不自动创建或以配置布尔值充当验收。服务端方案/故障验收后再接 Worker 重试、结算与恢复；长期占用是缺口。Run 副本、备份、退役另行评审，不改旧 ID/hash。

## 页面与结果未知

目录上传依次独立 POST，选择时冻结 actor/Project、每份 UUID/File/路径，显示已发布、确定拒绝、未知、未发送。确定文件拒绝可继续；未知或资格拒绝暂停，剩余不发送。取消、30 秒期限、损坏成功响应、断连均不撤回操作或自动重发。

| 操作 | 恢复门禁 |
| --- | --- |
| 真实批次上传未知 | 人工 GET 原 key；PENDING/404/503/取消不解除，原 PUBLISHED 确认后仍须显式继续未发送项 |
| 真实批次关闭未知 | 只查本次原 key 的独立 closure；合法 CLOSED 后标记关闭，再人工继续；发布冲突须另查 PUBLISHED |
| 手工原 key 只读查询 | 可取消/结束/换 key；不造 POST/File/假批次，不替代真实批次核对或解除其未知 |
| 刷新/新会话后的独立关闭 | 手工确认原 key 为 PENDING 后显式发起；成功只结束独立操作，不恢复旧批次；有真实未知批次时不另关其他 key |
| 删除未知 | GET 原 ID；合法元数据或明确 document_not_found 404 仅确认目录事实，仍须人工解除；Project 404、401/403 不算核对成功 |

原 UUID 可复制或刷新后手输只读恢复；File、正文、Cookie/CSRF 不进浏览器存储，不承诺恢复旧批次或重传丢失文件。列表/同名项不替代回执，关闭不自动删文档。新会话可查询、不能重放旧写入；归档只读。

手工查询与真实批次独立，退出/成功均不消费批次 File、未发送项或未知。新批次动作、关闭确认/提交/核对、删除确认同步作废旧查询；取消/换 key 的晚到成功或 401 不影响新操作。

上传/删除共用同步写门禁，确认删除期间也不得上传，删除 B 不中断 A。仅合法 204 结束删除等待，不宣称完整清理；异常 2xx 同样未知。人工解除不重发，列表/预览/同名重传不换原 ID。

换 actor/会话/CSRF/Project、离页均隔离旧响应；删除未知不承诺跨刷新恢复。资格通知必须属于当前请求且未过绝对期限，同一 owner 只通知一次失效。归档仅关闭写入，不覆盖既有身份/归属拒绝或阻止授权原 ID/key 读取；核对失败结束等待但保留未知。

client 校验 UUID、带时区时间、checksum、安全整数、字段白名单、原 Project/ID；原上传另核 key/state/回执。三语不展示内部错误，未来分页须标明已加载范围。

## 开发接续与验收

入口：[documents route](../../SKM/backend/src/skillmind/api/routes/documents.py)、[存储](../../SKM/backend/src/skillmind/storage/)、[管理组件](../../SKM/web/src/components/DocumentManagerPanel.tsx)、[契约](../../SKM/README.md#contracts)，排障见[Runbook](../operations/runbook.md#文档保存与删除的只读分诊)。

重点验证配额/同名/撤权/归档/引用竞争及回滚；PUT/commit/响应边界失败、重启原意图确认；S3 拒绝/超时/缺失/篡改、MIME/大文件/Unicode；目录部分成功、防重/切换、三语/键盘/窄屏。历史引用/副本/备份保持原身份；真实 DB/bucket 与会话验收只使用获准专用目标，不由 mock 或文档检查替代。
