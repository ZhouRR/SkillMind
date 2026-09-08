# 资源快照与 Run 工作区

> 用途：定义一次 Run 可以读取哪些资源、何时固定内容，以及重试时如何保持一致。前置阅读：[领域模型](domain-model.md)、[Runtime 文件边界](agent-runtime.md#62-文件shell-和-web)。实现进度与验收证据只维护在[计划 R01](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)。

## 先区分三种“冻结”

| 层次 | 固定什么 | 不能由此推导什么 |
| --- | --- | --- |
| 授权快照 | Project、资源身份、capability、允许的 scope、Integration 配置版本 | 冻结分支名不等于固定 commit；获得读取权不代表内容始终可达 |
| 内容快照 | 文档 ID/hash/成员清单，或仓库某次读取解析出的具体 revision | checksum 不是授权凭据；不可变引用也不是 blob 的永久备份 |
| 物化副本 | 本次实际取得的文件、转换结果、索引与 skipped，写入只读 input | 首次准备的副本不自动证明“Run 创建时已经取得全部内容” |

这三个时点必须分别记录。不能用“binding 已冻结”概括所有资源的内容一致性。

## 实际资源范围

| 资源 | 读取与物化入口 | 当前能依据代码确认的边界 |
| --- | --- | --- |
| repository | Run binding → repository source/client → `input/<requirement_key>/`；也可调用 `repository.read/v1` | 创建时冻结授权与 revision 选择规则；具体 commit/SVN revision 在打开资源时解析，物化 manifest 记录该次解析值 |
| document | Run sources → 文档快照 → document source/物化器 → `input/documents/` | 工作副本已加入显式清单和 ID/hash 校验；API/Web/既有回归尚未同步完成，不能作为已交付保证 |
| issue | `issue.read/v1` 在合法 binding 范围内逐条读取 | 不物化文件树；每次读取形成带来源/版本的 Evidence，不承诺外部票据内容永久不变 |

旧 document 实现于首次准备时读取 Project 全集。工作副本正在改为创建时冻结选择，二者不可混称为“当前已完整支持”。核对部署行为时必须记录镜像/版本，不能把本地代码进度套到服务器。

## 文档选择与冻结设计

### 用户选择的是范围，不是 Provider 名称

| 选择 | Run 创建时的含义 | 后续变化 |
| --- | --- | --- |
| 单份文档 | 固定一个 Project 内的 document ID 与内容 hash | 同路径删除再上传的新 ID 不替代原文档 |
| 文档集合 | 固定所选 ID 集合；去重、规范排序并校验归属 | 不追加未选文档 |
| 显式项目全集 | 用户确认后固定该次创建读取到的成员集合 | 排队期间与之后新增文档不进入本 Run |
| 可选 requirement 不选择 | 没有该槽位的文档读取授权 | 不隐式退回全集或任意默认 Provider |

必需 requirement 缺少合法选择时拒绝创建，不先创建一个权限不完整的 Run 再在对话中扩权。多个文档槽位可以共享 `input/documents/` 下的去重并集；Agent 的文档可见范围是这个 **Run 级并集**，不是槽位之间互相隔离。manifest 仍须保留各 requirement 的成员关系，重复 ID 的元数据必须一致。

### 一次执行的时序

```text
Preflight 展示候选/范围（暂态，不是冻结证据）
  → 服务端创建事务：校验 actor/Project/选择，保存文档清单与 hash
  → Worker 准备：按原 ID 取得原内容，验证实际字节，再写 manifest
  → Agent 读取：只读冻结范围，Evidence 引用实际内容
  → 新 Segment / Attempt：继续使用同一清单，不重新枚举 Project
```

冻结信息由服务端生成，至少覆盖 Project、requirement、选择模式、document ID、路径、MIME、size、content hash 和快照 checksum。客户端不能自行提交一个 checksum 就成为可信快照。

工作副本的暂定选择串为 `document:<UUID>`、`documents:<UUID>,<UUID>…` 和 `project-documents:all`，内部快照保存在 Run 的 `selected_sources_json`。这些是开发定位信息，**尚不是可宣称完成联调的公开协议**；后续需统一目录候选、集合选择 UI、Schema/example、Run detail 和历史兼容。不要给普通用户暴露这些内部选择串。

### 失败、缓存与历史

| 情况 | 设计要求 |
| --- | --- |
| 首次读取时 ID 已删除、blob 缺失、元数据或实际字节 hash 不符 | 准备失败；不换成同路径的新文档，也不当作普通 skipped 悄悄继续 |
| 内容合法但格式不支持或单文件过大 | manifest 记录 skipped 及原因，结果说明未覆盖范围 |
| 已存在完整物化副本 | 校验 Project/Run、冻结集合、来源 hash、实际文件及平台生成文件后复用；不要求重新读取当前 Project 全集 |
| 副本不完整、被改写或混入额外文件 | 明确失败；不静默删除现场并重建 |
| live document Tool 无法取得原 ID | 返回不可用；不能回退读取更新内容。已有的合法物化证据与 live 可达性是两回事 |
| 历史终态 Run 没有文档清单 | 保持原记录可读，标明范围信息缺失，不补造历史选择 |
| 旧非终态 Run 无可信清单 | 不自动授权项目全集；升级前明确处置，需要新输入时创建新 Run |

删除 Project 文档不是清除既有 Run 证据的同义操作；历史 blob/workspace 的保留和删除必须由独立保留策略决定，不能借恢复流程改写历史。

### 创建重放与调度

Run 创建的幂等重放必须保留首次成功创建的快照。相同请求与幂等键重发时，不能因为“全集”后来变了就生成另一份输入；也不能以跳过 actor、Project、输入或精确 SkillVersion 校验来解决冲突。

工作副本目前先解析文档，再计算包含快照的 `request_hash`，因此集合变化、删除与并发创建下的重放仍是待解决项。同步范围包括请求身份判断、创建冲突处理和有状态测试，不只修改 hash 函数。

Schedule 保存的是选择规则；每个 occurrence 经普通创建服务产生独立 Run。显式“全集”在每个新 Run 创建时冻结，而不是 Schedule 保存时就永久固定成员。同一 occurrence 的重放则必须回到同一个 Run/快照。选择已失效时按[调度失效语义](task-scheduling.md#保存和执行边界)停止，不改用其它来源。

## 仓库授权与内容版本

`scope.paths` 是访问硬边界；`scope.revisions` 是允许读取的 revision 范围。分支名或 `HEAD` 可能解析为不同 commit，空 revision allowlist 也不表示“只许读第一次物化的 revision”。具体选择与检查在 [repository_source.py](../../PJM/backend/src/projectmind/agent/repository_source.py)。

物化器在首次打开时固定该副本的具体 revision 并记录 manifest。需要复现同一基线时，优先读取该副本；额外的 live 仓库读取仍须满足冻结授权，并在 Evidence 中记录实际 revision。跨版本对比必须明确标注两份版本，不能把最新 live 内容与原副本混写为同一个事实。

若任务要求“创建瞬间的仓库内容”，应在创建前选择具体 commit/SVN revision 并验证可达性，或另行实现创建时解析与持久化；仅保存分支名不满足此要求。仓库副本复用时的 binding/Run 身份与额外文件检查也须单独验收，不能把已有 hash 校验当作全部验证。

## 产物与访问

```text
Run workspace/
├── input/                         Agent 只读
│   ├── documents/
│   │   ├── <原目录>/<文件或转换文本>
│   │   └── .projectmind/          manifest.json、files.txt
│   └── <repository requirement>/
│       ├── <scope 内的文件>
│       └── .projectmind/          manifest.json、files.txt、history.txt
├── workspace/                     Agent 可写的临时工作
└── output/                        Agent 可写的报告/补丁
```

每个物化根有自己的 manifest；document 不生成仓库 history。路径由物化器实际返回给 Brief，Agent 先读索引和跳过原因，再按需 search/read。

- 写入仅经 `workspace.write/v1`，不得修改 `input/`、Project 文档库或外部资源。
- 当前 `max_files/max_bytes` 按物化根检查，索引/历史等生成文件也占额度；不是所有根共享的 Run 总量账本。若需要 Run 总量上限，须增加跨根累计检查。
- 单根总体超限使准备失败，不截断后伪装完整成功。单文件与二进制的 skipped 不等于源文件不存在。
- xlsx/xlsm/docx 由[统一转换器](../../PJM/backend/src/projectmind/agent/binary_text.py)转文本，保留源 hash 与位置；PDF 未支持。
- 凭据不写入文件、manifest、Evidence 或 Agent 上下文。只读是 Tool/路径边界共同强制，不能只靠目录名。

## 已知差距与后续设计

| 差距 | 实施与审查入口 |
| --- | --- |
| 文档冻结改动尚未贯通 | [snapshot](../../PJM/backend/src/projectmind/documents/snapshot.py)、[binding](../../PJM/backend/src/projectmind/documents/binding.py)、[Run 创建](../../PJM/backend/src/projectmind/runs/service.py)、[document Provider](../../PJM/backend/src/projectmind/agent/document_provider.py) |
| 选择 UI / 公开快照未同步 | [共享输入](../../PJM/web/src/components/TaskLaunchFields.tsx)、[taskDraft](../../PJM/web/src/lib/taskDraft.ts)、[Run detail 契约](../../PJM/contracts/runs/detail/v1.schema.json) |
| 集合变化后的幂等与旧 Run 兼容 | [request_hash](../../PJM/backend/src/projectmind/runs/domain.py)、Run repository、服务与 Provider 的既有回归 |
| 仓库基线与缓存来源检查 | repository source、[物化器](../../PJM/backend/src/projectmind/agent/workspace_materializer.py)与[回归](../../PJM/backend/tests/agent/test_workspace_materializer.py) |
| 所有物化根的共同总量限制 | 物化器与 Worker 准备流程；不与[模型预算](subagents.md#预算现状与修正设计)混算 |

这些条目说明后续开发责任，不表示本次文档整理已修复 Runtime。

## 验收条件

| 场景 | 必须观察到的结果 |
| --- | --- |
| 单份/集合/全集与可选不选 | 实际可读成员与用户确认范围一致；槽位并集可解释 |
| 创建后新增、删除、同路径重传 | 不增加输入，不替换原 ID；缺失清楚失败 |
| 跨 Project、伪造 ID/hash、路径逃逸 | 越权/越界在读取前拒绝；实际 hash 不符的内容不得交给 Agent |
| 相同请求重发与并发创建 | 一个幂等身份只得到原 Run/快照；不同输入仍冲突 |
| Segment/Attempt、缓存篡改与旧 Run | 快照不漂移；额外文件/错误来源拒绝；历史不补造 |
| 仓库分支移动与多 revision | 输入基线与 live Evidence 各自版本可追溯，不混淆为创建时固定内容 |
| 文件转换、skipped 与额度 | 原文可追溯；未覆盖范围明确；逐根限制与总量保证分别证明 |
| 真实资源与模型 | HTTPS Git/SVN 凭据、断连与错误脱敏；Agent 可按 Brief 找到输入并说明限制 |
