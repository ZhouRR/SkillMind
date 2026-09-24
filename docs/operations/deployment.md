# 发布、迁移与启动

Windows 构建镜像，Linux 用 Docker Compose + GNU make 部署。首次与更新均执行 `make deploy`；首次配置见[启动](quickstart.md)，数据恢复见[恢复](backup-recovery.md)。

## 环境文件与配置边界

服务器同一部署目录放置 `images.tar`、`compose.yml`、`.env`、`Makefile`。[Compose](../../SKM/compose.yml)定义服务，[Makefile](../../SKM/Makefile)定义部署步骤；更新替换镜像包、Compose 和 Makefile，保留服务器 `.env`。镜像只含 system Interpreter，业务 Skill 从页面导入。

默认读取当前目录 `.env`，不要 source/include dotenv。特殊路径先 `export ENV_FILE=/path/app.env`，再执行 `make deploy`；原生 Compose 命令对应传 `--env-file "${ENV_FILE:-.env}"`。更新保持原 `COMPOSE_PROJECT_NAME` 和数据卷，避免误连空环境。

复用已有 Traefik 和外部网络，不发布 host port。域名、TLS、网络及 `SKILLMIND_CONTEXT_PATH` 须匹配；Web context path 编入镜像，修改服务器配置后仍需重建 Web。production 使用 HTTPS，可信代理范围按实际网络设置。

`SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID` 必须是非零 UUID，并由全部 Backend 共用。新存储生成新值，更新保留既有值；更换 endpoint/bucket 或重建存储不自动迁移旧文档，不能复用 UUID 绕过归属检查。preflight 检查配置，不检查对象读写。服务器显式设置内容类型白名单时，更新不会替换它；Excel 上传须允许 `.xlsx` / `.xls` 的真实 MIME。

## Agent SDK 与 device code 登录

API、业务 `worker` 和 `maintenance` 共用 Backend 镜像与 `.env`。业务 Worker 执行模型与工具；维护 Worker 在独立队列运行 Outbox、回收和定时触发，不挂载 Run/Codex 卷。更新须包含三个服务。

`SKILLMIND_AGENT_SDK` 选择解释器与 Run 引擎，默认 Codex；模型、思考强度与 Codex 数据目录配置见[环境模板](../../SKM/.env.example)。更新时保留服务器已有值，只补缺失字段，不用模板默认值覆盖已选模型或代理。

部署后执行设备登录，在命令给出的网页输入一次性代码并等待成功；第二条只检查登录状态：

```bash
docker compose --env-file "${ENV_FILE:-.env}" exec -T worker python -m skillmind.agent.codex_login
docker compose --env-file "${ENV_FILE:-.env}" exec -T worker python -m skillmind.agent.codex_login --status
```

登录与原生会话保存在 `codex-data`，更新保留该卷。不要复制宿主 `.codex` 或把凭据放入镜像。需代理时设置 `SKILLMIND_WORKER_HTTP_PROXY` / `SKILLMIND_WORKER_HTTPS_PROXY` / `SKILLMIND_WORKER_NO_PROXY` 后重建业务 Worker；Compose 自动保留内部服务和 loopback 直连。认证服务与模型服务都须可达。

使用 Claude 时显式设置 `SKILLMIND_AGENT_SDK=claude`，配套设置 Anthropic 兼容端点、凭据和模型，示例见[配置模板](../../SKM/.env.example)。切换前核清活动解释与 Run，同批更新 Backend；不同引擎的原生会话不能互相恢复，失败不会自动切换引擎。

## 迁移前置与执行

普通重新部署沿用现有数据和配置。涉及数据库变更时，按实际 migration 评估兼容性、在途执行和恢复点；数据恢复另走[恢复流程](backup-recovery.md)。不要并发部署或在部署期间改写镜像 tag、配置。

`make deploy` 停止当前 Compose project 的应用服务，迁移后自动恢复后台并消费队列。涉及停写的操作还须核对其他实例及远端在途请求；Makefile 不提供全局停写。`SKILLMIND_WORKER_DISPATCH_ENABLED=false` 会阻止新业务 job 的配送/执行，不能取消正在进行的调用，也不停止维护进程。

按需要启用功能，以下配置默认均为 `false`，API/Worker/Maintenance 保持一致：

| 配置前缀均为 `SKILLMIND_` | 用途 |
| --- | --- |
| `WORKER_DISPATCH_ENABLED` | Run、Effect、解释和核对 job 的配送/执行 |
| `DATABASE_WRITES_ENABLED` | PostgreSQL INSERT/UPDATE |
| `DOCUMENT_WRITES_ENABLED` | 项目文档库 Artifact CREATE |
| `GIT_WRITES_ENABLED` | 受控 Git commit/push |
| `MCP_TOOLS_ENABLED` | 已授权 MCP 工具发现、查询和受控调用 |
| `SCHEDULING_ENABLED` | 定时触发；仍需业务配送开启 |
| `DEFERRED_FEATURES_ENABLED` | 其他后置能力；不作为单项功能的默认启用方式 |

功能开关之外仍需资源范围、任务绑定和批准。启动画面的自动批准涵盖 DB/文档、Git 和 MCP，平台按原请求中各项同意验证，旧 Run 不因更新扩权；边界见[运行与资源指南](../development/runtime-guide.md)。数据库写入前，由目标库管理员应用[回执表 SQL](../../SKM/scripts/sql/postgres-effect-receipts.sql)，向执行角色授予 schema USAGE、回执表 SELECT/INSERT 及必要业务表权限；不授回执修改/删除权限。

MCP 连接填写完整 Streamable HTTP `/mcp` 地址与凭据，在资源编辑页取得工具、选择整体访问权限后保存；新发现工具须保存后才授权。任务声明并绑定所需能力；目录或绑定变化后通过新 Run 使用新快照。旧权限格式升级需重新发现和保存，Skill 声明变化时重新导入。

### Windows 构建与移送

在源码 `SKM/` 的 PowerShell 执行：

```powershell
docker compose build api web
.\scripts\export-images.ps1
```

再次导出加 `-Force`。导出固定应用 tag `skillmind/backend:0.1.0` 和 `skillmind/web:0.1.0`，不执行 build/pull；失败保留旧 archive，`.partial` 不是成品。只改 Web 可仅构建 `web`，导出仍需两个应用镜像。构建架构须匹配服务器，导出不转换架构。

按[环境文件布局](#环境文件与配置边界)移送导出包。基建镜像缺失时部署会拉取；完全离线的初次部署先在 Windows 准备并同包导出：

```powershell
docker compose pull postgres redis object-storage object-storage-init
.\scripts\export-images.ps1 -IncludeInfrastructure -Force
```

### Linux 核验与部署

在四文件目录执行 `make deploy`。主要阶段如下：

| 阶段 | 行为 |
| --- | --- |
| configuration / cleanup-old-images / load-images / prepare-images | 校验配置；先清理未被任何容器引用的旧应用镜像，再导入镜像、补齐基建并检查架构 |
| stop-application / infrastructure / initialize-storage | 停止当前应用、启动基建、创建 bucket |
| migration-check / migrate / readiness | 核合法迁移路径、upgrade head、核 DB head/Redis/存储配置 |
| backend / runtime-check / web / cleanup-replaced-images | 同批重建 API/Worker/Maintenance，核一致性后恢复 Web，并清理替换后不再被容器引用的旧应用镜像 |

镜像清理仅针对 `skillmind/backend` 和 `skillmind/web`，检查范围包含已停止容器；仍被容器引用的镜像会保留。清理使用非强制删除，不处理其他仓库镜像、容器或数据卷。

失败会显示原始错误、阶段和退出码并停止后续步骤；已完成步骤不会自动回滚，已启动后台可能继续运行。先检查再决定重试：

```bash
make status
make logs
docker compose --env-file "${ENV_FILE:-.env}" run --rm -T --no-deps migrate alembic current
docker compose --env-file "${ENV_FILE:-.env}" run --rm -T --no-deps migrate alembic heads
```

## 同批更新与检查

`make runtime-check` 检查 Backend 各副本的实际 image ID 和配置 fingerprint，不调用模型、创建 Run 或查询业务数据。独立执行失败只返回非零，不停止容器。检查一致不证明登录、网络、blob/KEK 或业务结果正确。

## 迁移与回退审查

只核本次实际涉及的 [migration](../../SKM/backend/migrations/versions/) 与协议消费者。未知 revision、多 head 或迁移失败时先核 DB 状态，不 stamp、删审计或清队列。旧镜像仅在兼容当前 schema、快照和队列时可恢复使用，见[应用版本回退](backup-recovery.md#应用版本回退)。

## 仅更新 Web

仅构建 Web 后仍正常导出并执行 `make deploy`，包含未变的 Backend 镜像；此流程会停机、检查迁移并重启应用。发布前确认 API/契约/context path 兼容。
