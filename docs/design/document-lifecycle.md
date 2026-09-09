# 项目文档的保存、读取与清理

本页负责文档资产；[资源快照](resource-snapshots.md)负责 Run 选择、冻结和副本，[项目生命周期](project-lifecycle.md)负责整项目操作。以下区分现状与修正要求，不是完整存储可靠性已交付的声明。

## 一个例子：列表消失不等于清理完成

上传 A 产生文档 ID 与 blob；Run 1 冻结 A 的 ID/hash。删除 A 当前先删元数据、再尝试删 blob；同路径重传 A′ 是新 ID，不能修复 Run 1 的原引用。

必须区分文档目录、存储字节、Run 冻结引用、物化副本：列表消失不证明字节清理，Run 仍显示 A 不证明可下载，删除报错也不证明元数据未提交。

## 身份、目录与公开面

ProjectDocument 元数据在 PostgreSQL，blob 用内部 storage_key 定位。document_id 是身份，folder/name 是展示路径；同 Project/路径唯一，不按内容去重。只有上传、列表、下载、删除，无覆盖、移动、改名、版本或恢复 API；目录由 folder 投影，空目录不保存。

| 操作 | 当前语义 |
| --- | --- |
| POST documents | 单 file + 可选 folder 的 multipart，201 元数据；同名 409 document_conflict |
| GET documents | 按 folder/name 排序的完整数组，无查询/分页/Folder 契约 |
| GET document content | Project + 精确 ID 授权后返回附件，不是公共 blob URL |
| DELETE document | 元数据 commit 后调用 storage，通常 204，不保证完整清理 |

公开数据只含身份、路径、size/MIME/checksum、上传者和时间。读用 ProjectReadActor，写用 ProjectWriteActor 且要求 ACTIVE；越权与不存在统一 404。授权先于业务提交，尚不能保证在途撤权/归档阻止随后保存。

## 上传的三个边界

[DocumentService](../../PJM/backend/src/projectmind/documents/service.py)当前顺序：

```text
名称/目录校验 → 读取元数据用量 → 大小/MIME/配额/文本校验
  → put 新 blob → 元数据事务 commit → 201
```

| 当前检查 | 实际缺口 |
| --- | --- |
| 文件默认 25 MiB，Project 默认 500 MiB | route 先完整 file.read；不是请求体/内存硬上限，配额不含孤立 blob/副本/备份 |
| 名称、folder 最多 200 字符 | storage key 单段最多 128，129–200 名称可能存储失败且未映射上传 422；应写前稳定拒绝，不截断 |
| MIME allowlist、指定文本凭据扫描 | 校验申报类型，不证明真实格式、病毒安全或任意二进制已脱敏 |
| 读取用量后检查配额 | 没有原子预留，并发上传可分别通过旧用量 |
| blob 先写、元数据唯一约束后验 | 冲突可留下孤立 blob；所有 IntegrityError 目前被误归为同名冲突 |

### 保存可靠性的修正要求

写前限制实际流式字节，明确内容/MIME 策略；为 Project 建立原子配额预留与结算，最终发布时复核授权、状态、路径与预留。网络 I/O 不放入长期 Project 行锁。

持久保存上传意图、原请求身份与 blob 归属，区分确定拒绝、存储未知、commit 未知。未知先核对，不能异常后立即删除可能已提交的对象；确认无引用的失败上传才进入可重试清理。幂等需定义 actor/Project、摘要、保留期和并发胜者，不是仅加 header。

## 读取、下载与预览

普通下载不复核真实 size/checksum；read_frozen_document 才验证原 ID、元数据与字节。目标统一完整性与安全错误，区分损坏、缺失、存储不可用，不用同路径新文件补旧引用。

Web 仅预览 txt/md/markdown/htm/html，元数据不超过 1,000,000 bytes；Markdown 显示原文，超限提示下载。HTML srcDoc 使用 sandbox="" 禁脚本/同源，但没有外部资源禁载策略，不是网络隔离或 generated Host。

待补实际预览字节上限、外部图片/CSS 拒绝、下载文件名安全编码。现行直接拼 filename 对非 Latin-1 名称会失败，引号也需测试；上传接受中文名不证明下载可用。

## 删除与历史引用

当前无 Run/Schedule 引用检查或持久清理回执：元数据 commit 后 storage 失败会留下 blob，原 ID 再删先得 404；S3 adapter 吞掉全部 S3Error，204 可掩盖 AccessDenied。整 Project 删除另走元数据清单，不调用逐文件清理。

### 引用保护与清理的修正要求

- Run、Schedule、JSON snapshot 和在途冻结引用共同阻止物理删除；引用检查与新增引用共享并发约束，不只隐藏按钮或检查外键。
- 若需停止新选择，另设计可审计退役状态；不迁移旧 ID/hash，不用删除再上传模拟版本。
- 元数据持久记录待清理身份和精确对象；异步确认、失败重试，不按 bucket 前缀广泛删除。不存在、拒绝、断连、未知分别记录。
- 单文档与整 Project 共用清理协议；Run 副本和备份独立保留。当前没有该协议、状态 API 或修复 CLI。

## 页面与结果未知

目录上传是顺序的独立 POST，单份失败后继续；done 表示已处理，不是成功数。取消本地等待不能撤回已保存文件。

目标分别展示成功/确定拒绝/未知，保留原文件、Project、路径，不自动重发整目录，也不以同名列表项证明成功。上传/删除需同步防重复及 actor/Project/request 身份检查；切换、卸载后丢弃晚到响应。

删除未知只读核对原 ID，不能宣称附件已彻底清理。client 尚缺 UUID/日期/checksum/安全整数/Project 关联的严格校验，组件未知状态也未闭合；受控三语错误不暴露存储细节。未来分页须标明已加载范围。

## 开发接续与验收

入口：[documents route](../../PJM/backend/src/projectmind/api/routes/documents.py)、[存储实现](../../PJM/backend/src/projectmind/storage/)、[Web 管理组件](../../PJM/web/src/components/DocumentManagerPanel.tsx)、[契约索引](../../PJM/README.md#contracts)。当前缺口登记 R01/R10/R11；排障见[Runbook](../operations/runbook.md#文档保存与删除的只读分诊)。

- 实 DB 验证配额/同名竞争、撤权/归档/引用竞争和完整回滚。
- 在 put/commit/响应边界注入失败，重启后可确认原意图，不误删成功对象或重复创建。
- 专用 bucket 区分 S3 拒绝、超时、不存在；验证篡改、伪报 MIME、大文件与 Unicode header。
- 实组件 + mock API 验证目录部分成功、重复提交、切换、三语/键盘/窄屏；真实会话和存储另验。
- 历史 Run、物化副本与备份保持原身份；真实 DB/bucket 写入只使用明确授权的专用目标。
