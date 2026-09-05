# 资源快照与 Run 工作区

> 当前设计与实现差距。Runtime 总体规则见[Agent Runtime §6.4](agent-runtime.md#64-资源快照的物化)。

## 实际资源范围

| 资源 | 当前取得方式 | Run 内落点 | 冻结情况 |
| --- | --- | --- | --- |
| repository | 使用 Run 冻结的 binding，按 scope/revision 读取 Git/SVN | `input/<requirement_key>/` | 绑定已冻结，首次物化后校验内容复用 |
| document | 蓝图包含 document requirement 时，枚举当前 Project 的文档全集 | `input/documents/` | 首次准备时才取得清单；不是逐 requirement 的 Run binding 快照 |
| issue | `issue.read/v1` 在合法 binding 范围内逐条读取 | 不物化文件树 | 读取时形成 Evidence，不能假设票据永不改变 |

依据：[WorkspaceMaterializer](../../PJM/backend/src/projectmind/agent/workspace_materializer.py) 的 `materialize`、`_materialize_documents` 与 `_materialize_repository`。repository 和 document 不能写成同一种冻结保证。

## 产物与访问

```text
Run workspace/
├── input/                         Agent 只读
│   ├── documents/
│   └── <repository requirement>/
│       └── .projectmind/
│           ├── manifest.json      文件、hash、来源、skipped
│           ├── files.txt          文件名索引
│           └── history.txt        有界提交历史
├── workspace/                     Agent 可写的临时工作
└── output/                        Agent 可写的报告/补丁
```

每个物化根有自己的 manifest；document 根不生成 repository history。实际路径由物化器返回给 Brief，Agent 先读索引/清单，再使用 `workspace.search/v1`、`workspace.read/v1`。

- Agent 写入仅使用 `workspace.write/v1`，不能修改 `input/` 或原资源。
- 总文件数/总字节超限使准备失败，不能截断后伪装为完整成功。
- 单文件过大、不支持的二进制、symlink 等按实现记录为 `skipped`；“跳过”不等于“不存在”。
- xlsx/xlsm/docx 由平台转文本并保留位置及原文 hash；PDF 未支持。
- 重试若已有 manifest，验证文件与 hash 后复用；不一致明确失败，不悄悄重建。
- 凭据只在 Provider 边界解析，不写入文件、manifest、Evidence 或 Agent 上下文。

[实现](../../PJM/backend/src/projectmind/agent/workspace_materializer.py) · [访问 Provider](../../PJM/backend/src/projectmind/agent/workspace_provider.py) · [二进制转换](../../PJM/backend/src/projectmind/agent/binary_text.py)

## 已知差距与后续设计

当前 document 路径的权限边界是 Project，而非选中的文档槽位。用户只选择一个文档，并不代表 Agent 只会得到该文档；Run 创建到首次准备之间新增/更新的文档也可能进入快照。首次物化后的 hash 复用不能证明“创建时输入已完全冻结”。

后续应把 document 快照纳入统一资源冻结协议：

1. Preflight 明确选择单文档、文档集合或显式“项目文档全集”；不得由一个抽象 requirement 隐式授权全集。
2. 创建 Run 时固定文档 ID、版本/content hash 和集合清单。全集选择也必须冻结当时成员。
3. 物化器只读取该清单，校验 Project 所有权与 content hash。删除/内容不可达时明确失败或记录符合契约的不可读项，不替换为新版本。
4. 在 Run 详情展示输入范围；新 Segment/Attempt 继续复用原清单。
5. 同步 ResourceBinding/Run snapshot、DTO、Provider、Brief、API/Web 与迁移/历史兼容测试。

这些是后续实施要求，本次文档整理没有修改物化器。上线前验收必须包含“选一份却拿到多份”“排队期间文档变化”“重试内容漂移”和跨 Project 拒绝。

## 验收条件

| 层 | 验证内容 |
| --- | --- |
| 本地确定性 | scope、symlink、单文件 skipped、总体超限、manifest 篡改、重试复用 |
| 项目文档 | 上传文本/表格/设计书，Agent 根据 Brief 自行发现；记录实际全集范围 |
| 仓库资源 | 固定 revision、文件索引、历史、文本化及 Evidence 位置 |
| 真实连接 | HTTPS Git/SVN 凭据传递、访问范围、错误脱敏与断连恢复 |
| 模型行为 | 无额外提示能找到物化资源；对 skipped 明确报告限制 |
