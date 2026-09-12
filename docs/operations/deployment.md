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

## 迁移前置与执行

先区分操作场景，不把升级/恢复要求套到空环境首次安装：

- **全新安装**：确认使用新环境、配置和镜像齐全后执行；不存在的旧数据、队列无需备份/对账。复用旧卷或旧队列不算全新安装。
- **已有环境更新**：先关闭新业务入口、核清在途调用/远端未知效果，停止全部写入者并取得[一致恢复点](backup-recovery.md#一致恢复点包含什么)。不并发部署或改写镜像/tag、配置；重命名不迁移旧数据。
- **数据恢复/故障对账**：走[恢复流程](backup-recovery.md)，不执行会自动启动 Worker 的 `make deploy`。

`make deploy` 停止当前 project 应用服务，再初始化基建、迁移并启动 API/Web/Worker；执行即允许恢复后台工作，可能立即消费队列和恢复任务。默认保持 `SKILLMIND_DEFERRED_FEATURES_ENABLED=false`、`SKILLMIND_DATABASE_WRITES_ENABLED=false` 和 `SKILLMIND_DOCUMENT_WRITES_ENABLED=false`，三个值分别在 API/Worker 保持一致。后置开关控制既有外部写入、调度发火和子 Agent；数据库开关仅开放 PostgreSQL INSERT/UPDATE；文档开关仅开放项目文档库的 Artifact CREATE 提案与人工批准执行，三者不互相放行。文档写入需 API/Worker 同时升级至 v2 对象协议并完成 0048 migration，使用既有文档库 namespace；旧 v1 待写只允许核对。数据库启用前须配置明确表/列/操作范围，并按[回执权限要求](../design/repository-effects.md#postgresql-单行事务与原执行回执)安装目标库回执表。`SKILLMIND_WORKER_DISPATCH_ENABLED=false` 阻止新的 Run/Effect/解释 job 执行，不取消已在运行的调用，也不是维护模式。Makefile 不控制其他实例、orphan、其他 daemon 或远端写入，不能代替全局停写确认。

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
| api-web / worker | 等 API/Web health，再启动 Worker |
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

## 迁移与回退审查

有数据库/协议变化时审查本次跨越的 [upgrade/downgrade](../../SKM/backend/migrations/versions/)及消费者；回退时核相关限制。纯样式更新不要求重审所有历史迁移，`make deploy` 仍自动检查当前 DB 路径。下表是版本兼容索引，不是每次发布逐项执行的清单：

| revision | 回退限制 |
| --- | --- |
| 0018–0021 | 组织共享不能还原成单 Project；新结果包络、Segment/Interaction/Effect 不由旧 Worker 自动兼容 |
| 0022–0023 | 降级可能丢用户偏好/密文材料；已有引用与旧 KEK 必须保留 |
| 0025–0028 | 调度/生成版本/索引/审计可能丢失。0027 在 DDL 前锁表，子 Session、无 SDK ID、BRANCH 或旧唯一键冲突均拒绝；仅旧 PRIMARY 可无损表示的数据可降级，不删审计 |
| 0029 输入回执 | 任何回执行存在时拒绝降级，不仅 READY；不能删回执绕过 |
| 0030 预算账本 | 三表任一有数据即拒绝降级，包括已结算记录；不为旧 Run 补造账户，无通用核对/退款 CLI |
| 0031 会话 v2 | 旧会话需重登，任何 v2 行（含撤销）阻止丢列；新旧 API 不混跑 |
| 0032 用户生命周期 | 初始化旧版本、不造事件；任何安全事件阻止降级，API/页面与真实 DB 分别验收 |
| 0033 成员审计 | 不补旧事件；任何事件阻止降级/整项目删除。成员/账户/偏好/项目删除共用组织锁，旧 writer 不混跑 |
| 0034 项目版本 | 旧版 1 不代表历史次数；任一版本 >1 拒绝丢列。API/Web 成套切换，缺原版本为 422、不补版；回退前全停写/在途核对 |
| 0035 调用绑定/观察 | 旧预约不补调用历史；锁表后，任何非空绑定字段或观察记录拒绝降级，观察对原预约为 RESTRICT |
| 0036 调度 occurrence | 旧行 protocol=0，不补历史；旧 writer 不混跑。RESTRICT 保留引用；锁表后任一 occurrence、protocol=1 或 configuration_version≠1 均拒绝降级，不删记录绕过 |
| 0037 文档存储归属 | 旧行不绑定，新写保存完整身份；任一归属非空拒绝丢列。旧 API/Worker 不混跑，迁移不证明旧 blob 可读/可清理 |
| 0038 原上传意图 | PUT 前保存原请求/占用，发布/清理不删除；意图/关联阻止降级。POST 必填 Idempotency-Key，缺失 422；API/Web 成套切换、旧 writer 不混跑 |
| 0039 文档清理记录 | 删除前保存原对象/元信息/DELETE 身份，不造旧上传历史；任一记录阻止降级，文档/意图/清理阻止整项目删除；旧删除 writer 不混跑 |
| 0040 附件字节 | 新字段全空为未发布，不回填；任一非空拒绝降级。API/Web 理解 checks v2，Worker/Interpreter/Tool catalog 配套；保留 write/v1，不给旧 Skill/Run 升权 |
| 0041 评价原请求 | 原键/hash 同事务保存，旧行空值；任一绑定非空拒绝丢列。API/新 Web 配套提交/确认/分页，旧接口无新增重放保证，不删评价绕回退 |
| 0042 停止待发布 | 关闭标记/独立审计同存，不改原回执/占用；任一存在拒绝降级。API/Web 配套；DB CHECK 拒绝旧 SQL 发布已关闭项，但不阻止旧 PUT 或替代停写/对账 |
| 0043 预算启动所有权 | 旧行不造 owner，新绑定存 token hash；任一 owner 痕迹拒绝丢列。旧调用方不混跑，hash/迁移不证明模型未启动、停止或计量完整 |
| 0044 解释原请求 | 只建新台账、不补旧会话；任何请求/调用记录阻止降级。Web/API/Worker 配套切换，旧解释/调整 URI 关闭，旧队列任务明确拒绝；停止旧 API/Worker 后迁移，不从旧 job 补造作者或重放未知模型调用 |
| 0048 成果对象 v2 | 只扩展 0045 台账的协议 CHECK，不改 v1 原 key/hash；v1 仅核对，v2 使用隔离物理前缀。含任意 v2 台账或 revision 2 文档库 binding 即拒绝降级，API/Worker 配套更新，不混跑旧 writer |

有表/旧页面可读不证明功能接齐或非终态可续行；预算另过[混合 Worker 门禁](../design/run-budgets.md#上线门禁与接线顺序)。兼容未知保持停写，按[回退](backup-recovery.md#应用版本回退)处理，不删审计/快照/未决占用凑条件。

## 会话协议切换检查

仅升级涉及[会话凭据协议](../design/authentication.md#会话凭据-v2-与切换要求)时适用：保全未确认动作 ID/key、通知重登并排空旧 API；隔离验证旧行升级、新登录/失效和降级拒绝，head 正确后开新 API。用真实 HTTPS 验多页/多实例 CSRF、失效与 no-store，不采集凭据或探测真实用户密码。

失败关闭入口，选 forward fix/完整恢复，不改默认 version、删会话或直接回旧镜像。旧备份可能复活撤销会话，恢复时另核；字段存在不证明旧 Web 兼容，重登不代替后台放行。

## 启动与放行

部署默认启动 API/Web/Worker，不改业务开关；首次完成后执行 `make bootstrap-admin`。普通业务开放前验证登录、权限、Skill、Run/结果/证据和外部接入；真实 smoke 会写入或计费，只用获准环境与输入。启动成功不替代模型、事务、存储或备份恢复验收；恢复场景仍须核清事实后分别放行 API/Web、后台与普通入口。

## 仅更新 Web

Windows 只构建 Web 后正常导出，Linux 仍执行 `make deploy`。此入口统一走应用停机、迁移检查与重启流程，不承诺 Web-only 零停机；未变的 Backend 镜像仍需包含在包内。API/契约/路径兼容性须在发布前确认。

## 后续开发约束与验收

保持交付为镜像包、Compose、.env 与 Makefile，不重新增加服务器端辅助脚本、隐藏错误或日常部署必填审批参数。新增检查优先使用 Compose 状态/退出码与镜像内已有运维命令；不能用删数据、忽略错误或自动回滚来简化流程。

[R11](../planning/roadmap.md#开发任务)仍缺跨实例停写/清理与真实恢复证据。Make/PowerShell 的 fake Docker 回归只证明命令顺序与失败停止；实际 Rancher/Linux、CPU、迁移事务、health/HTTPS 和业务效果须在目标环境验收。
