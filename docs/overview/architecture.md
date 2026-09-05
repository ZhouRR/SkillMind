# 系统结构与执行链路

> 定位：架构导航。设计不变量由各领域规范负责，具体数据形状由可执行契约负责。

## 组件和数据所有权

```text
浏览器 Web
    │ HTTPS / JSON / SSE
共享 Traefik ── context path 路由
    │
    ├── Web 静态资源
    └── FastAPI ── 领域服务 ── PostgreSQL
                        │       ├── Run / Segment / Attempt
                        │       ├── Skill / Binding / Session
                        │       └── Event / Outbox / Result / Approval
                        │
                     Outbox → Redis Queue → ARQ Worker
                                              │
                                   AgentEngine → 模型会话
                                              │
                                         Tool Gateway
                                              │
                                  Provider → 已绑定资源
                                              │
                                      Evidence / Artifact
```

Backend 是模块化单体。API 与 Worker 共享一个 `projectmind` package，分进程运行；业务层依赖 AgentEngine 抽象。PostgreSQL 保存业务和审计事实，Redis 负责队列、短期锁与通知，object storage 保存 blob，Run workspace 保存执行文件。恢复必须覆盖数据库引用到的 blob 和需要恢复的 workspace。

## 三条关键链路

| 链路 | 起点 → 关键处理 → 落点 | 设计入口 |
| --- | --- | --- |
| Skill 成为任务 | Source → Interpreter → Blueprint → DRAFT/发布 → Project 启用 → TaskCatalog | [Skill 契约](../design/skill-contract.md)、[实现](../design/skill-interpretation.md) |
| 一次 Run | 创建并冻结输入/绑定 → Outbox → Worker claim → Agent/Tool → 交互或终态 | [Runtime](../design/agent-runtime.md) |
| 外部变更 | ChangeProposal → 精确版本批准 → EffectExecution → 前置版本检查 → 写入与 read-back | [受控写入](../design/repository-effects.md) |

模型得到目标、Skill 指导和有限资源上下文，提出工具调用；平台验证 capability、绑定、参数、额度和批准后才调用 Provider。模型判断不替代权限检查。

## 变更应放在哪一层

| 层 | 实现入口 | 责任 |
| --- | --- | --- |
| API | [routes](../../PJM/backend/src/projectmind/api/routes/)、[actor dependency](../../PJM/backend/src/projectmind/api/auth_dependencies.py) | 身份、资源授权、请求响应转换 |
| 领域与服务 | [runs](../../PJM/backend/src/projectmind/runs/)、[skills](../../PJM/backend/src/projectmind/skills/)、[effects](../../PJM/backend/src/projectmind/effects/) | 业务规则与跨对象协调 |
| 持久化 | [db](../../PJM/backend/src/projectmind/db/)、各模块 repository | 事务、锁、不可变快照和迁移 |
| Agent 与 Provider | [agent](../../PJM/backend/src/projectmind/agent/)、[worker](../../PJM/backend/src/projectmind/worker/) | SDK 适配、工具边界、执行和恢复 |
| Web | [api](../../PJM/web/src/api/)、[lib](../../PJM/web/src/lib/)、[pages](../../PJM/web/src/pages/) | 数据校验、纯投影、页面和交互 |
| 契约 | [contracts](../../PJM/contracts/README.md) | 跨组件数据形状、example、OpenAPI |

## 部署边界

生产入口使用已有 Traefik。仅 `web` 和 `api` 连接 external edge network；ProjectMind 不提供自己的 Traefik，也不发布宿主端口。API 内部路由保持 `/api`，Web 保持 `/`；公开 context path 由 Traefik 去除，Web 构建路径与 API root_path 必须匹配。

当前 Compose 包含 API、Web、Worker、PostgreSQL、Redis、object storage 及一次性 migration/init 服务。生成模块的 `module-builder` 是后续设计，尚未进入 [compose.yaml](../../PJM/compose.yaml)。

[技术结构图](technical-architecture.html)适合展示组件关系；实施前继续阅读[变更指南](../development/change-guide.md)和对应领域设计。
