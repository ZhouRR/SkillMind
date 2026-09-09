# 系统结构与执行链路

Backend 是模块化单体，API 与 Worker 共享 `projectmind` package、分进程运行；业务层依赖 AgentEngine 抽象。

## 组件和数据所有权

```text
浏览器 Web → 共享 Traefik → FastAPI → 领域服务 → PostgreSQL
                  └── Web 静态资源        │         └── 业务与审计正本
                                      Outbox
                                         ↓
                                   Redis → ARQ Worker
                                              ↓
                                    AgentEngine → 模型
                                              ↓
                                       Tool Gateway
                                              ↓
                                    Provider → 已绑定资源
                                              ↓
                                      Evidence / Artifact
```

PostgreSQL 保存 Skill、Run/Segment/Attempt、Session、Event/Outbox、Result 和批准。
Redis 承担队列、短期锁、通知与登录防护；blob 在 object storage，执行文件在 Run workspace。
这些存储不属于同一事务；恢复需一起核对数据库引用、blob、workspace 和密钥。

## 三条关键链路

| 链路 | 关键步骤 |
| --- | --- |
| Skill 成为任务 | Source → Interpreter → Blueprint → 审查/发布 → Project 启用 → TaskCatalog |
| 一次 Run | 冻结输入/授权 → Outbox → claim → 受监督准备 → Brief/启动校验 → 执行 → 等待或终态 |
| 外部变更 | Proposal → 精确批准 → EffectExecution → CAS → 写入 → read-back |

模型提出调用，平台校验 capability、绑定、参数、预算与批准后才执行。
创建冻结授权与选择；Run 输入回执、Segment Brief、Attempt lease 分别证明不同事实，不能互相替代。
登录防护的 Redis 配额也不等于数据库 Session 的撤销状态。

## 变更应放在哪一层

| 层 | 实现与责任 |
| --- | --- |
| API | [routes](../../PJM/backend/src/projectmind/api/routes/) / [actor dependency](../../PJM/backend/src/projectmind/api/auth_dependencies.py)：认证、授权、入出参 |
| 领域与持久化 | service/domain 决定规则；repository / [db](../../PJM/backend/src/projectmind/db/)负责事务、锁与快照 |
| 执行 | [agent](../../PJM/backend/src/projectmind/agent/) / [worker](../../PJM/backend/src/projectmind/worker/)：SDK、Tool、监督与恢复 |
| Web / 契约 | [代码入口](../../PJM/README.md#web)负责校验和交互；[contracts](../../PJM/README.md#contracts)固定公开形状 |

规则归属见[设计索引](../design/README.md#どの設計を変更するか)，同步步骤见[变更指南](../development/change-guide.md)。

## 部署边界

复用共享 Traefik，仅 web/api 进入 edge network，不发布宿主端口。外部 context path 被 Traefik 去除；
API 内部保持 `/api`、Web 保持 `/`，Web build path 与 API root_path 须匹配。
[Compose](../../PJM/compose.yaml)包含应用、存储与一次性迁移/初始化服务，module-builder 尚未接入。
[技术结构图](technical-architecture.html)用于整体展示，发布步骤见[部署指南](../operations/deployment.md)。
