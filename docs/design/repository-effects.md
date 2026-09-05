# 受控外部写入与代码仓库回写

> 现行设计。Git/SVN Provider 已实现；真实系统专项验收仍见[计划 §13](../planning/roadmap.md#13-当前执行状态)。

## 调用与批准链路

```text
Agent 读取资源 → Evidence → change.propose/v1
                                 ↓
                         冻结 ChangeProposal
                                 ↓
                   用户审查版本/checksum/具体变更
                                 ↓
                    Run 发起人或 ADMIN 批准
                                 ↓
                  EffectExecution Worker / Provider
                                 ↓
                  检查目标版本 → 写入 → read-back
                                 ↓
                         before/after Evidence
```

Agent 只能调用提案工具，`repository.write/v1` 不进入它的直接 Tool 集合。平台以 [EFFECT_CAPABILITIES](../../PJM/backend/src/projectmind/effects/catalog.py) 统一决定 Provider、提案校验器和预授权资格。

## 已注册写入

| capability | Provider | 自动批准条件 | 前置与验证 |
| --- | --- | --- | --- |
| `issue.update/v1` | Redmine CAS adapter | 仅 ADMIN 明确配置的 LOW 风险、精确 scope 策略 | discovery → revision CAS → 幂等写入 → 回读 |
| `repository.write/v1` | Git、SVN | 不可预授权，始终人工批准 | base revision/目标约束 → 提交 → remote 回读 |

标准 Redmine REST 接口不自动具备平台需要的 CAS 与幂等语义；未通过版本化 discovery 时不能 apply。审批时重新校验 actor、Project、Proposal version/checksum、绑定和 Integration 状态；批准不能扩大最初 scope。

## 仓库写入模式

| `write_mode` | 目标 | 约束 |
| --- | --- | --- |
| `direct`（默认） | Integration 配置的默认分支/目标 | Git 仅 fast-forward，不 force；目标移动时拒绝陈旧写入 |
| `branch` | 新建 `projectmind/` 保留命名空间分支 | 不覆盖已有非本次幂等提交；SVN 使用约定 branches 路径 |

两种模式都须先批准。branch 模式配置了支持的 forge transport 时可以创建 PR/MR；未配置时只有分支与 commit。PR 创建是提交之后的外部调用，**不能承诺 commit 与 PR 跨系统原子成功**。失败后按同一提案/幂等标识恢复，不能盲目重新提交。

现行默认值来自 [repository_write.py](../../PJM/backend/src/projectmind/effects/repository_write.py)。旧“禁止默认分支写入”“SVN 只能提案”“branch 必定创建 PR”的描述均不再适用。

## 变更载体和冻结内容

提案保存目标 binding、capability、operation、风险、依据 Evidence、前置 revision 和变更预览。仓库载体为逐文件全文 `SET` / `REMOVE`；不是允许模型直接执行任意 unified diff 或 git/svn 命令。

Provider 使用独立临时可写副本，复用 [repository client](../../PJM/backend/src/projectmind/agent/repository_client.py) 的凭据和命令边界。它不能把 Agent 的只读 `input/` 当作提交工作副本。

[Tool 契约](../../PJM/contracts/tools/repository.write/v1/request.schema.json) · [提案校验](../../PJM/backend/src/projectmind/effects/repository_write.py) · [Effect Provider](../../PJM/backend/src/projectmind/effects/repository_effect.py) · [forge](../../PJM/backend/src/projectmind/effects/forge.py)

## 并发、重试和失败

| 情况 | 行为 |
| --- | --- |
| Proposal 版本/checksum 不匹配 | 拒绝审批旧预览，重新取得待审版本 |
| 前置 revision 或目标已变化 | 标记陈旧/冲突，要求重新观察和提案 |
| 相同幂等键重试 | 检查既有写入结果，按 Provider 语义重放，不重复副作用 |
| 写入成功但回读失败 | 明确验证失败并保留审计，不伪装成功或重新写入 |
| 分支 commit 成功但 PR 失败 | 恢复 PR 阶段并检查已有分支；保留已发生的外部变更 |
| 需要撤回 | 由管理员审查后执行新的 revert/修正流程；当前没有自动回滚服务 |

“可回滚”表示有可追溯的 before/after 和可制定修正操作，不表示平台已经实现一键撤回。禁止 force push、自动合并 PR、自动批准、跨仓库原子写入和无 scope 更新。

## 验收

本地回归分别验证提案拒绝、target/base 冲突、幂等 replay、direct/branch、scope 与 read-back。真实 Git/SVN/forge/Redmine 验收使用专用目标，覆盖“外部写入成功后连接断开”以及恢复时不重复变更。端到端未通过前不将本地 Provider 测试称为生产验证。
