# 发布、迁移与分阶段放行

已有环境的发布手册；首次准备见[启动](quickstart.md)，数据替换见[恢复](backup-recovery.md)。命令在 Linux 服务器的版本发布目录执行，先确认环境、维护窗口、版本与负责人；未知目标不试运行。

本工程按 Skillmind 全新命名部署，不保留旧产品标识或旧数据兼容层；既有 volume、数据库、队列和对象存储不会自动迁移。使用独立的新环境初始化，旧环境数据按其原版本保全。镜像只包含 system Interpreter Skill；合成测试素材仅保存在 `backend/tests`，业务 Skill 在部署后导入。

## 先分清四种操作

| 操作 | 范围 |
| --- | --- |
| config / preflight | 配置解析 / DB migration 与 Redis PING，不证明业务就绪 |
| build / export / load | 构建镜像 / 打包本地镜像 / 导入 archive，不是备份或应用验收 |
| migration | 改变 DB；失败后查实际 revision，不用 stamp 掩盖 |
| 启动 / 放行 | 进程启动可能消费旧工作；后台恢复和普通业务开放分别批准 |

## 一个例子：关闭 dispatch 后仍有工作

已入 Redis 的 Effect 和即将到期的 Schedule，不会因 `SKILLMIND_WORKER_DISPATCH_ENABLED=false` 自动停止：

| 入口 | false 时的实际行为 |
| --- | --- |
| Outbox relay | 不选择新的 Run/Effect dispatch topic，lifecycle 通知仍可配送 |
| 已入队 Run/Effect job | 仍可进入 claim/executor；不检查该开关 |
| Schedule / recovery cron | 仍可创建 Run、处理期限和恢复 |
| Skill 解释/调整 job | 独立入口，仍可能调用模型 |

入口见 [WorkerSettings](../../SKM/backend/src/skillmind/worker/settings.py)。开关不是维护模式，不终止远端请求，环境修改也不即时重载。停写须关闭全部入口/触发、处理在途并确认所有 API/Worker 实例停止；远端未知则隔离，按[原 Effect 对账](runbook.md#incident-与-recovery)，不换键重做。

## 环境文件与配置边界

构建端为 Windows + Rancher Desktop（Moby）+ PowerShell + Docker Compose；部署端为 Linux + Docker Compose v2 + GNU make 和常规系统工具（sh、realpath、sha256sum、mktemp、id）。两端都不要求宿主 Python、Node 或 jq。CPU 架构仍须匹配：默认 linux/amd64，ARM64 显式构建 linux/arm64；不能仅凭都是 Linux 混用镜像。

[Makefile](../../SKM/Makefile)经 [compose.sh](../../SKM/scripts/compose.sh)调用宿主 Docker；[deploy.sh](../../SKM/scripts/deploy.sh)收集受控观测值，再用 Backend 镜像内的 Python 校验既有部署契约。检查容器不接网络、不挂 Docker socket，只读临时观测和镜像 archive；迁移/preflight 才经 Compose 连接基建。当前只支持在部署服务器本机执行，远程 daemon 的 bind path 不在支持范围。

| 选择 | 唯一来源 |
| --- | --- |
| 环境文件 | make/shell 的 ENV_FILE → 发布目录 .env；同一绝对文件同时供 Compose 插值和 API/Worker/migrate 注入，不随发布包分发 |
| Compose project | make/shell 的 COMPOSE_PROJECT_NAME → skillmind；文件内同名值不选择目标，必须保留服务器现有 project 名以复用原 volume |
| Compose 文件 / 目录 | 固定当前发布目录的 compose.yaml；拒绝命令参数中的 file、project、profile 旁路 |
| 应用镜像 | 发布从校验后的 release.env 取完整 Backend/Web image ID；API/Worker/migrate 共用 Backend ID。日常操作默认 tag，可用 BACKEND_IMAGE_ID / WEB_IMAGE_ID 显式固定 |

例如 `make config ENV_FILE=/controlled/config/app.env COMPOSE_PROJECT_NAME=approved-project`。相对环境路径以发布目录为准，缺失/空路径拒绝；不手设内部 SKM_COMPOSE_ENV_FILE。发布包不替换服务端 .env、密码、存储世代或数据卷。

保留 Docker 优先级：shell 插值高于文件，service.environment 高于 env_file，不 source dotenv。切换 ENV_FILE 不改现有进程/容器/DB URL/外部资源，须另核目标；见[插值规则](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)和 [env_file](https://docs.docker.com/reference/compose-file/services/#env_file)。

日常用 `make config`（quiet）；完整 config、容器 env/inspect 可泄密，不进报告。发布工具只内部比较有效环境，不输出或持久保留值。

文档存储必配 SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID，由同世代 API/Worker 与恢复清单共享；未配置拒绝 blob 操作。UUID 与 endpoint/bucket 共同校验，新建存储换 UUID、凭据轮换不换身份。旧文档不自动绑定，按[归属迁移](../design/document-lifecycle.md#存储归属与配置切换)处理；health/preflight 或改配置不能替代。

## 迁移前置与执行

先在独立环境验收 API/Worker/migrate/Web 兼容组合，取得[一致恢复点](backup-recovery.md#一致恢复点包含什么)，保留新旧 image ID/archive。

```text
恢复点 → 全实例停写/在途对账 → 加载镜像 → 查 revision/head
  → 单独迁移 → API/Web 核对 → 批准恢复后台 → 验收 → 普通入口放行
```

### Windows 构建与移送

在源码 `SKM/` 的 PowerShell 执行；context path 必须与服务器一致：

```powershell
./scripts/export-images.ps1 -Version 0.1.0-preview1 -Platform linux/amd64 -ContextPath /skillmind
```

默认构建 API/Web，导出共享 Backend + Web，不重新构建 PostgreSQL、Redis、MinIO。服务器缺少配套基建镜像或首次离线部署时加 `-IncludeInfrastructure`，显式 pull/save 当前 Compose 指定版本；通常更新可复用服务器已有基建镜像。第三方版本升级仍需单独审查，不在 make deploy 中自动替换运行中的基建。

输出 `SKM/images/skillmind-<Version>/`：images.tar、compose.yaml、Makefile、必要 Shell、.env.example、release.env 和 SHA256SUMS。清单记录版本、平台、context path 和实际 image ID；脚本在 Backend 检查容器内验证 archive 后才发布目录。没有真实 .env 或业务内容。完整目录移送到 Linux 的新版本目录，禁止只拷 tar 配旧脚本；保存终端显示的 RELEASE_SHA256 到独立可信记录。

`-SkipBuild` 仅导出已有合格镜像；`-WebOnly` 只构建 Web，但包内仍携带同一 Backend 作为基线/校验 runtime。版本目录不覆盖，失败 staging 保留供检查，不能当成成品；不要并发修改源码、tag 或输出目录。

### Linux 核验与部署

进入新发布目录，先从可信记录设置下列值。必须在执行包内 Makefile/脚本之前核对来源和文件；包内 checksum 本身不证明真实性。

| 变量 | 确认内容 |
| --- | --- |
| ENV_FILE / COMPOSE_PROJECT_NAME | 服务器原配置的绝对路径与原 project 名，各阶段不变 |
| DAEMON_ID | 人工核准的目标 `docker info --format '{{.ID}}'`，不能跨 daemon 流用 |
| RELEASE_SHA256 | Windows 输出并独立保管的 SHA256SUMS 文件摘要，不从收到的包自动自签 |
| MAINTENANCE_CONFIRMED=1 | 一致恢复点、全实例停写/在途对账、普通入口关闭和独占维护均已确认 |

```bash
printf '%s  SHA256SUMS\n' "$RELEASE_SHA256" | sha256sum --check -
sha256sum --check --strict SHA256SUMS
make config
```

逐条确认成功；变量须 shell export 或作为 make 参数传入。完成维护准备后，停止本 project 的业务容器（其他实例仍需人工确认），再运行：

```bash
sh scripts/compose.sh stop api web worker migrate
make deploy
```

`make deploy` 连续执行 load → migration-plan → migration → API/Web health 与 preflight，任一步失败立即停止；它不建立备份、不停止其他实例、不启动基建或 Worker、不开放普通入口。只看说明用 `make deploy-help`。需要逐阶段审查可分别执行 `make deploy-load`、`make deploy-migrate`、`make deploy-api`；首次基建准备见 [Quickstart](quickstart.md)。

load 先验整个包与 tar checksum，从同一打开的 archive 导入，再按不可变 ID/平台/context path 和 tar 中两个必需 tag 的唯一 config digest 校验。首次服务器没有 Python runtime，因此内部 manifest 检查发生在 Docker load 之后、迁移/业务启动之前；失败可能留下已导入镜像或已更新 tag，但绝不拿本地旧 tag 补缺项。保留旧 image ID/archive，不并发改写文件。

migrate 前 PostgreSQL/Redis 必须健康、storage 运行且当前 bucket 初始化成功。工具检查同 daemon/project 全部容器（含 orphan/one-off）、镜像和有效环境并拒绝活动写入者；目标镜像的 `preflight --migration-plan` 确认全部 revision、单 head 和合法前进路径，单独 upgrade 后再验 DB head/Redis。观测暂存在仅操作者可读的临时目录，正常退出/可捕获中断时清理，不输出值；断电或强杀后的残留仍按敏感材料保管。

只读排查 current/heads 也须使用同一配置，把 BACKEND_IMAGE_ID 设为已核实清单中的完整 ID：

```bash
sh scripts/compose.sh run --rm -T --no-deps --pull never migrate alembic current
sh scripts/compose.sh run --rm -T --no-deps --pull never migrate alembic heads
```

revision 越链、多 head 或检查失败即停，核对已提交 revision 后决定 forward fix/完整恢复。不改 alembic_version/stamp；此前 revision、非事务操作及外部事实不自动回滚。

## 迁移与回退审查

每次发布审查[实际 upgrade/downgrade](../../SKM/backend/migrations/versions/)和消费者，不从下表推导可任意降级：

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

有表/旧页面可读不证明功能接齐或非终态可续行；预算另过[混合 Worker 门禁](../design/run-budgets.md#上线门禁与接线顺序)。兼容未知保持停写，按[回退](backup-recovery.md#应用版本回退)处理，不删审计/快照/未决占用凑条件。

## 会话协议切换检查

1. 核清全部入口/实例，保全未确认动作 ID/key、通知重登，排空并停旧 API。
2. 隔离环境验旧行升级、新登录/失效和降级拒绝；head 正确后才开新 API。
3. 同一 v2 参数下以真实 HTTPS 验多页/多实例 CSRF、失效和 no-store，不采集原凭据或探测真实用户密码。
4. 失败关闭入口，选 forward fix/完整恢复，不改默认 version、删会话或直接回旧镜像。

旧备份可能复活撤销会话，须另核；字符串字段不证明旧 Web 兼容，重登与后台放行分别审核。正本见[认证](../design/authentication.md#会话凭据-v2-与切换要求)。

## 启动与放行

保持 Worker 停止、普通入口关闭。逐阶段执行时，迁移成功后使用同一清单与维护变量启动 API/Web（make deploy 已包含此阶段，不重复执行）：

```bash
make deploy-api
make
```

`make run` 是 deploy-api 别名。工具固定 --no-build/--no-deps/--pull never，等 health 并复验 image ID/有效环境/preflight；基建/初始化缺失、配置不符或失败即停，不删保护参数。preflight 不验 blob/KEK/认证/模型/恢复，须另核权限、Run/结果/证据和输入；授权 GET 可更新 idle，不是备份所需完全停写。

核清旧队列和在途 Run/Effect/Schedule，另获准恢复本环境全部后台工作（MAINTENANCE_CONFIRMED 不含此授权），再执行：

```bash
make deploy-worker BACKGROUND_APPROVED=1
```

工具复查目标、API/Web 配置/镜像/health/preflight 后仅开 Worker；无独立业务 healthcheck，运行状态不证明旧工作已核清。先只开获准 [smoke](runbook.md#通常-smoke)，通过后人工开放普通业务，失败隔离并保留事实。

smoke 会写入/计费，须隔离 DB、Redis/队列、存储、Worker、凭据；单独 Project 不隔离旧 job/cron，当前无“只消费测试 Project”模式，不得跳过后台许可。

任何阶段失败/超时/中断不自动续行、回滚或重发：镜像可能部分导入、迁移已提交、服务已启动。先核原事实；重调用不会自动把活动容器当作上次成功跳过。

## 仅更新 Web

仅当 API/契约/数据库/运行配置均未变，且新 Web 与现用 Backend 已验兼容时使用。Windows 加 `-WebOnly` 构建导出；Linux 用同一清单方式验证新版本目录，另确认 Web 切换窗口：

```bash
make deploy-web WEB_ONLY_CONFIRMED=1
```

此路径仍要求 MAINTENANCE_CONFIRMED=1，含义限定为 Web 切换维护窗口和独占操作，不要求停 API/Worker；不能拿它批准 Backend 或配置变更。检查现有 API/可选 Worker 的 image ID、有效环境及必要服务健康后，仅 `up web --no-deps`；不执行 migration/preflight、不重启 API/Worker。Backend 版本不一致、旧 Web 不健康或有效环境变化即拒绝；路由、挂载等其他配置未变仍须人工审查。失败保持 Web 入口隔离、核实际状态，不自动回滚。

## 后续开发约束与验收

脚本不控制共享 Traefik、其他 daemon/进程或远端效果，不是跨实例锁/授权系统。独占维护时不得并发改配置、镜像、context 或启动其他 writer；确认变量只是声明，不证明全局停写。

[R11](../planning/roadmap.md#开发任务)保留全实例停写/清理及真实恢复缺口。脚本回归只验参数、身份拒绝与失败不续行；实际 Compose/PowerShell、迁移事务、health/HTTPS 和业务放行须获准环境验收。
