# 架构与核心术语

Backend 是模块化单体：API、业务 Worker 与维护 Worker 共享 Python package，分别运行。业务层依赖 AgentEngine 抽象，SDK 适配器负责 Codex/Claude 的具体通信。

## 执行链路

```text
Web / API 调用方 → Traefik → FastAPI → 领域服务 → PostgreSQL
                                          ↓ Outbox
                                Redis 队列与通知
                                  ↓         ↓
                             业务 Worker  维护 Worker
                                  ↓
                           AgentEngine → 模型
                                  ↓
                         Tool Gateway / Provider
                                  ↓
                        受绑定和授权限制的外部资源
```

- Skill：原文导入 → 最小执行声明 → 审查发布 → 项目启用 → 任务目录。历史 Blueprint 保持原版本兼容。
- Run：冻结版本、输入与授权上限 → 入队 → 准备只读输入 → 执行 → 等待答复/批准或提交结果。
- 外部变更：观测 → 提案 → 人工或适用的自动批准 → Effect → 回读并保存回执。

数据库是业务与审计正本，Redis 提供队列、短期锁、通知和登录防护；文档 blob 与 Run workspace 独立存储，不能当作同一事务。恢复参见[一致恢复点](../operations/backup-recovery.md#一致恢复点包含什么)。

## 读代码时的核心术语

| 名称 | 含义 |
| --- | --- |
| Organization / Project | 组织资产与项目访问范围 |
| SkillVersion / Task | 不可变发布版本及该版本提供的任务入口 |
| Integration / ResourceBinding | 外部连接及运行获准使用的资源范围，凭据不交给模型 |
| Run / Segment / Attempt | 一次业务请求、一次分析或续行阶段、同阶段的技术尝试 |
| Brief / Snapshot | 冻结执行指令与输入事实，不能用当前配置回填旧值 |
| Interaction / Proposal | 普通问题与具体外部变更提案，采用不同回答协议 |
| Effect / Receipt | 实际变更及原操作回执；回执不证明远端当前状态 |
| Evidence / Artifact | 可追溯的观测证据与已保存文件 |
| Result / Evaluation | 不可变执行结果与追加式人工评价/修订 |

代码定位统一见[变更指南](../development/change-guide.md)，详细接口见 [Contracts](../../SKM/README.md#contracts)。修改时核对[运行边界](../development/runtime-guide.md)，部署拓扑与路径配置见[部署指南](../operations/deployment.md)，此处不另列模块规格。
