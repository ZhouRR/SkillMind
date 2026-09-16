# 发布、迁移与启动

Windows 构建镜像，Linux 用 Docker Compose + GNU make 部署。首次安装与后续更新均执行 `make deploy`，不要求宿主 Python/Node。首次配置与管理员创建见[启动](quickstart.md)，数据恢复见[恢复](backup-recovery.md)。

## 环境文件与配置边界

服务器部署目录只需四个文件：

```text
skm/
├── images.tar
├── compose.yml
├── .env
└── Makefile
```

[Compose](../../SKM/compose.yml)是服务正本，[Makefile](../../SKM/Makefile)直接调用 Docker Compose。首次从源码 `.env.example` 准备配置；更新只替换镜像包、Compose 和 Makefile，不覆盖服务器密码和业务配置。镜像只包含 system Interpreter，业务 Skill 在部署后导入。

默认使用当前目录 `.env`，不要 source/include dotenv。特殊路径用 `make deploy ENV_FILE=/path/app.env`，同一文件供 Compose 插值与 Backend env_file 注入。COMPOSE_PROJECT_NAME 按 shell → .env → 默认 skillmind 选择；更新保留原 project 名和数据卷。

复用既有 Traefik 与外部网络，不部署网关、不发布 host port。服务器域名、TLS、网络及 SKILLMIND_CONTEXT_PATH 须正确配置；Web 路径已编入镜像，改服务器 .env 不能改变静态产物。数据库、存储、KEK/model 在首次启动前准备。SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID 必填非零 UUID、API/Worker 共用；新存储首次生成，更新保留，不自动迁移旧对象。migration-check/readiness 拒绝缺失或零值，但不证明 blob 可读写。

## Agent SDK 与 device code 登录

API 与 Worker 使用相同的 `SKILLMIND_AGENT_SDK`（默认 `codex`，可设 `claude`），同时选择 Skill 解释器和 Run 引擎。Codex 配置如下：

```dotenv
SKILLMIND_AGENT_SDK=codex
SKILLMIND_CODEX_MODEL=gpt-5.6-terra
SKILLMIND_CODEX_REASONING_EFFORT=max
SKILLMIND_CODEX_HOME=/var/lib/skillmind/codex
```

Compose 同时启动 `worker`（业务执行）与 `maintenance`（Outbox、回收与定时触发），共用 Backend 镜像和 `.env`。维护进程仅连接内部网络，不挂载 Run/Codex 卷；`maintenance` 健康检查独立于业务队列。手动更新须一起更新这两个服务；仅启动业务 Worker 不再运行定期维护。部署前停止旧 Worker，避免旧 cron 继续进入业务队列；已有业务 job 不迁移、不清空。

Compose 的 `codex-data` 卷只挂载到 Worker，保存设备登录和原生会话；更新保留此卷。使用镜像内入口启动登录：

```bash
docker compose exec -T worker python -m skillmind.agent.codex_login
docker compose exec -T worker python -m skillmind.agent.codex_login --status
```

首条命令输出验证网址与一次性 device code，在浏览器完成 ChatGPT 授权后等待命令返回 `authenticated: true`。状态命令只输出是否登录，不输出 token 或账户正文。登录期间需可访问官方认证服务；不要复制宿主 `.codex`、写入 API key 或把凭据放进镜像。非交互部署不会自动执行登录。

Worker 需要代理出口时，在 `.env` 设置 `SKILLMIND_WORKER_HTTP_PROXY` / `SKILLMIND_WORKER_HTTPS_PROXY` 和可选的 `SKILLMIND_WORKER_NO_PROXY`，Compose 仅向业务 Worker 映射大小写代理变量，并追加内部服务及 loopback 的直连例外。修改后用 `docker compose up -d --no-deps worker` 重建容器；先确认没有在途业务执行。直接启动 Worker 的环境仍可使用标准 `HTTP_PROXY` / `HTTPS_PROXY`（也支持小写及 `ALL_PROXY`）。Codex 子进程仅继承这些网络配置与允许的系统环境，数据库、对象存储和 Claude 凭据仍不传入。`NO_PROXY` / `no_proxy` 合并保留原值，并追加 `localhost`、`127.0.0.1`、`::1`，确保本机受控 MCP 不经过代理。认证服务和模型服务都须可访问；设备码请求 403 不能靠切换模型或降低思考强度解决。

Claude 回退需显式设置 `SKILLMIND_AGENT_SDK=claude` 并重建 API/Worker 容器，保留既有 Anthropic 兼容端点与凭据、DeepSeek 模型、`CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000` 和 `CLAUDE_CODE_EFFORT_LEVEL=max`。不在请求失败时自动重发或降档。切换前核清活动解释/Run；不同引擎的原生会话不能互相恢复。Codex 当前预算限制见[运行时](../design/agent-runtime.md#codex-adapter)。

## 迁移前置与执行

先区分操作场景，不把升级/恢复要求套到空环境首次安装：

- **全新安装**：确认使用新环境、配置和镜像齐全后执行；不存在的旧数据、队列无需备份/对账。复用旧卷或旧队列不算全新安装。
- **已有环境更新**：先关闭新业务入口、核清在途调用/远端未知效果，停止全部写入者并取得[一致恢复点](backup-recovery.md#一致恢复点包含什么)。不并发部署或改写镜像/tag、配置；重命名不迁移旧数据。
- **数据恢复/故障对账**：走[恢复流程](backup-recovery.md)，不执行会自动启动 Worker 的 `make deploy`。

`make deploy` 停止当前 project 应用服务，再初始化基建、迁移，同批启动 API/Worker/Maintenance，核对一致性后恢复 Web；执行即允许恢复后台工作，可能立即消费队列和恢复任务。默认保持 `SKILLMIND_DEFERRED_FEATURES_ENABLED=false`、`SKILLMIND_DATABASE_WRITES_ENABLED=false`、`SKILLMIND_DOCUMENT_WRITES_ENABLED=false` 和 `SKILLMIND_GIT_WRITES_ENABLED=false`，四个值分别在 API/Worker 保持一致。后置开关控制既有外部写入、调度发火和子 Agent；数据库开关仅开放 PostgreSQL INSERT/UPDATE；文档开关仅开放项目文档库的 Artifact CREATE 提案与人工批准执行；Git 开关仅开放批准后的 commit/push，四者不互相放行。Git 须配套升级 API/Worker/Web 并执行 0050 migration；现有连接仍保持原权限，需在资源页面显式选择写入范围与模式。文档写入需 API/Worker 同时升级至 v2 对象协议并完成 0048 migration，使用既有文档库 namespace；旧 v1 待写只允许核对。数据库启用前须配置明确表/列/操作范围，并按[回执权限要求](../design/repository-effects.md#postgresql-单行事务与原执行回执)安装目标库回执表。`SKILLMIND_WORKER_DISPATCH_ENABLED=false` 阻止新的 Run/Effect/解释 job 执行，不取消已在运行的调用，也不是维护模式。Makefile 不控制其他实例、orphan、其他 daemon 或远端写入，不能代替全局停写确认。

### Windows 构建与移送

在源码 SKM/ 的 PowerShell 执行：

```powershell
docker compose build
.\scripts\export-images.ps1
```

也可只构建 `docker compose build web` 或 `docker compose build api`。导出仍保存当前两个应用 tag：skillmind/backend:0.1.0 与 skillmind/web:0.1.0，不按创建时间猜测版本，不 build/pull、不读取 .env、不启动校验容器。

输出固定为 `images/images.tar`。再次导出用 `.\scripts\export-images.ps1 -Force`，只允许替换已有 archive，不重建镜像；保存完成才替换，失败时保留旧 archive，残留 .partial 不是成品。PowerShell 使用单横线 `-Force`。

构建时 Compose 默认读取 .env 用于解析配置；只有 Web context path 作为业务构建参数，不把数据库/存储密码传入 build args。服务器 CPU 架构须与镜像匹配；需要跨架构时在构建/拉取阶段设置 DOCKER_DEFAULT_PLATFORM，导出不转换架构。

将 **images.tar、compose.yml、Makefile** 拷到服务器同一部署目录，保留服务器 .env。使用可信传输渠道并保留上一版镜像包；导出不提供独立来源认证或 archive 内部身份核验。

PostgreSQL/Redis/MinIO/mc 已有时复用本地镜像，不足时部署自动拉取 Compose 指定版本。服务器完全离线且尚无基建镜像时，在 Windows 先准备并同包导出：

```powershell
docker compose pull postgres redis object-storage object-storage-init
.\scripts\export-images.ps1 -IncludeInfrastructure -Force
```

该选项只保存本地镜像；第三方版本升级单独评估。

服务器显式配置 `SKILLMIND_DOCUMENT_ALLOWED_CONTENT_TYPES` 时，更新镜像不会替换此白名单。Excel 验收需在原列表追加 `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` 和 `application/vnd.ms-excel`，保留其他已授权类型；不通过伪装 MIME 绕过限制。

### Linux 核验与部署

进入四文件所在目录，执行：

```bash
make deploy
```

输出阶段与操作：

| 阶段 | 行为 |
| --- | --- |
| configuration / load-images | 检查配置和 archive 存在，Docker 导入镜像 |
| prepare-images | 应用镜像必须已导入；补齐缺失基建镜像，核镜像与服务器架构 |
| stop-application | 停止当前 project 的应用服务和旧一次性任务，保留数据卷 |
| infrastructure / initialize-storage | 等 PostgreSQL/Redis 就绪、启动 MinIO、幂等创建 bucket；连接重试有上限 |
| migration-check / migrate / readiness | 校验存储 namespace 配置及合法前进路径、执行 Alembic upgrade head，再核配置与 DB head/Redis |
| backend / runtime-check / web | 同批重建 API/Worker/Maintenance，核对镜像与配置后恢复 Web |
| complete | 显示当前服务状态 |

失败显示原始错误、阶段与退出码，立即停止，不自动重试、回滚、删卷或重置密码。镜像、部分迁移或服务可能已生效；先查状态和原错误，再决定如何继续：

```bash
make status
make logs
docker compose --file compose.yml run --rm -T --no-deps migrate alembic current
docker compose --file compose.yml run --rm -T --no-deps migrate alembic heads
```

自定义 ENV_FILE 时，原生 Compose 命令也须传 `--env-file "$ENV_FILE"`。迁移通过 compose.yml 的 pull_policy: never 使用本地镜像。日志/config/inspect 可能含敏感值，只分享脱敏错误，不上传 .env。

revision 不识别、多个 head、连接失败或迁移错误均需核对实际 DB 状态；不 stamp、不删除 alembic_version/审计记录绕过。数据库初始化、migration 与 bucket 创建是实际写入，health/preflight 成功不证明业务、HTTPS、blob/KEK 或恢复已验收。

## 同批更新与检查

API、执行 Worker 和维护 Worker 共用 `skills/service_wiring.py`；维护侧不加载解释器。`make deploy` 使用同批镜像与配置强制重建全部后端，等待健康检查、执行一致性核对后恢复 Web。此流程是协调重启，不是零停机；不删除数据卷或 Codex 登录数据，也不重跑未知业务。

独立诊断使用 `make runtime-check`，检查所有后端副本的实际 image ID、解释身份、能力目录、队列和功能开关。诊断不调用模型、不创建 Run、不查询业务数据；解释器一致地未配置时仅警告，不把可选解释器变成确定性导入的新前提。

独立检查失败只返回非零，不自动停止或修复容器。部署中的检查失败会停止后续步骤，但已经启动的后端可能继续运行；失败不是全局停写证明。相同 fingerprint 只证明当前容器配置一致，不证明登录、外部网络或业务结果正确。

需要查看脱敏配置时运行 `docker compose --env-file .env exec -T api python -m skillmind.ops.runtime_identity`，对 Worker 将服务名改为 `worker`。它是短命诊断进程，不是实际模型进程的状态探针。

## 迁移与回退审查

只审查本次真实涉及的数据库和公开协议变更；精确 upgrade/downgrade 条件以 [migration 实现](../../SKM/backend/migrations/versions/)为准，不在发布文档复制逐版本目录。当前运行策略的诊断、结构工具和报告消费者应同批更新。

变更前明确恢复点，回退按 [备份恢复](backup-recovery.md#应用版本回退)处理。不能 stamp、删除审计/快照、清空队列或重放 UNKNOWN 来绕过迁移失败。单纯文档、显示或局部代码优化不额外引入业务批准、重新导入或历史格式转换流程。

## 会话协议切换检查

API Key 需先应用 0051 migration，再放行同版 API/Worker；既有浏览器会话保持有效。Key 资格版及降级限制见[认证设计](../design/authentication.md#外部应用-api-key)。


仅升级涉及[会话凭据协议](../design/authentication.md#会话凭据-v2-与切换要求)时适用：保全未确认动作 ID/key、通知重登并排空旧 API；隔离验证旧行升级、新登录/失效和降级拒绝，head 正确后开新 API。用真实 HTTPS 验多页/多实例 CSRF、失效与 no-store，不采集凭据或探测真实用户密码。

失败关闭入口，选 forward fix/完整恢复，不改默认 version、删会话或直接回旧镜像。旧备份可能复活撤销会话，恢复时另核；字段存在不证明旧 Web 兼容，重登不代替后台放行。

## 启动与放行

部署默认启动 API/Web/Worker/Maintenance，不改业务开关；首次完成后执行 `make bootstrap-admin`。普通业务开放前验证登录、权限、Skill、Run/结果/证据和外部接入；真实 smoke 会写入或计费，只用获准环境与输入。启动成功不替代模型、事务、存储或备份恢复验收；恢复场景仍须核清事实后分别放行 API/Web、后台与普通入口。

## 仅更新 Web

Windows 只构建 Web 后正常导出，Linux 仍执行 `make deploy`。此入口统一走应用停机、迁移检查与重启流程，不承诺 Web-only 零停机；未变的 Backend 镜像仍需包含在包内。API/契约/路径兼容性须在发布前确认。

## 后续开发约束与验收

保持交付为镜像包、Compose、.env 与 Makefile，不重新增加服务器端辅助脚本、隐藏错误或日常部署必填审批参数。新增检查优先使用 Compose 状态/退出码与镜像内已有运维命令；不能用删数据、忽略错误或自动回滚来简化流程。

[R11](../planning/roadmap.md#开发任务)仍缺跨实例停写/清理与真实恢复证据。Make/PowerShell 的 fake Docker 回归只证明命令顺序与失败停止；当前 Linux 部署和正常业务路径已有实际使用，范围见计划；Rancher、跨实例恢复、完整 HTTPS 与异常业务效果仍须分别验收。
