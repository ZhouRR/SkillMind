# 发布、迁移与分阶段放行

已有环境的发布手册；首次准备见[启动](quickstart.md)，数据替换见[恢复](backup-recovery.md)。命令在目标 `SKM/` 执行，先确认环境、维护窗口、版本与负责人；未知目标不试运行。

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

[Makefile](../../SKM/Makefile)和镜像导出共用 [compose.py](../../SKM/scripts/compose.py)，仅需宿主 Python 3.12 标准库；Windows 可指定 executable。

| 选择 | 唯一来源 |
| --- | --- |
| 环境文件 | 显式 `--env-file` → shell `ENV_FILE` → `SKM/.env`，解析为同一绝对文件路径，供插值和 API/Worker/migrate 的 env_file 使用 |
| Compose project | 显式 `--project-name` → shell `COMPOSE_PROJECT_NAME` → skillmind；**文件内同名变量不选择目标**，旧的非默认部署须显式指定 |
| Compose 文件 / 目录 | 固定本套代码的 compose.yaml / SKM；拒绝旁路 file、project、profile 选择器 |
| 应用镜像 | 普通入口固定默认 tag；发布入口显式 pin Backend/Web 的完整 sha256 image ID，三 Backend service 共用同一 ID |

例如 `python3 scripts/compose.py --env-file /controlled/config/app.env --project-name approved-project -- config --quiet`。相对路径以 `SKM/` 为准，缺失/空路径拒绝；不手设内部 SKM_COMPOSE_ENV_FILE 或绕过入口。

保留 Docker 优先级：shell 插值高于文件，service.environment 高于 env_file，不 source dotenv。切换 ENV_FILE 不改现有进程/容器/DB URL/外部资源，须另核目标；见[插值规则](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)和 [env_file](https://docs.docker.com/reference/compose-file/services/#env_file)。

日常用 `make config`（quiet）；完整 config、容器 env/inspect 可泄密，不进报告。发布工具只内部比较有效环境，不输出或保存值。

文档存储必配 SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID，由同世代 API/Worker 与恢复清单共享；未配置拒绝 blob 操作。UUID 与 endpoint/bucket 共同校验，新建存储换 UUID、凭据轮换不换身份。旧文档不自动绑定，按[归属迁移](../design/document-lifecycle.md#存储归属与配置切换)处理；health/preflight 或改配置不能替代。

## 迁移前置与执行

先在独立环境验收 API/Worker/migrate/Web 兼容组合，取得[一致恢复点](backup-recovery.md#一致恢复点包含什么)，保留新旧 image ID/archive。

```text
恢复点 → 全实例停写/在途对账 → 加载镜像 → 查 revision/head
  → 单独迁移 → API/Web 核对 → 批准恢复后台 → 验收 → 普通入口放行
```

各阶段独立批准，不凭上一阶段文件自动放行。从受控清单确认以下变量，不照抄示例身份：

| 变量 | 确认内容 |
| --- | --- |
| ENV_FILE / COMPOSE_PROJECT_NAME | 已批准的配置文件和目标项目，后续各命令保持一致 |
| DAEMON_ID | 目标 `docker info --format '{{.ID}}'`，同名项目不能跨 daemon 复用 |
| BACKEND_IMAGE_ID / WEB_IMAGE_ID | 经验证的完整 `sha256:…` image ID，不是 tag、短 hash 或 registry manifest digest |
| IMAGE_ARCHIVE / ARCHIVE_SHA256 | 可信 Docker save tar 与已核实的完整文件 checksum；只读受控保存，不允许并发改写 |
| MAINTENANCE_CONFIRMED=1 | 恢复点、全部实例停写/在途对账、普通入口关闭、独占维护责任均已人工确认 |

`make deploy` 只显示帮助，无状态变化。确认后逐个执行，不拼成自动继续的命令链：

```bash
make deploy-load
make deploy-migrate
```

Make 要求 shell 显式导出清单变量，缺确认/身份即拒绝；等价参数见 `python3 scripts/deploy.py <phase> --help`。load 不删旧容器/镜像，先验 tar checksum 与两个必需 tag 的唯一 config digest，再从同一打开文件导入、按 ID 复查。archive 缺项不能用本地旧 tag 补，缺 manifest/含糊多平台映射拒绝。

migrate 前自行确认 PostgreSQL/Redis 健康、storage 运行和当前 bucket 初始化成功，发布不隐式启动基建。工具检查同 daemon/project 全部容器（含 orphan/one-off）并拒绝活动写入者；目标镜像的 `preflight --migration-plan` 须确认全部 revision、单 head 和合法前进路径，单独 upgrade 后再验 DB head/Redis。

只读排查 current/heads 也须用相同配置和已核实镜像。以下默认 tag 的实际 ID 必须与清单一致：

```bash
python3 scripts/compose.py -- run --rm -T --no-deps --pull never migrate alembic current
python3 scripts/compose.py -- run --rm -T --no-deps --pull never migrate alembic heads
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

保持 Worker 停止、普通入口关闭。迁移阶段成功后，使用同一清单与已确认的维护变量，只启动 API/Web：

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

## 后续开发约束与验收

脚本不控制共享 Traefik、其他 daemon/进程或远端效果，不是跨实例锁/授权系统。独占维护时不得并发改配置、镜像、context 或启动其他 writer；确认变量只是声明，不证明全局停写。

[R11](../planning/roadmap.md#开发任务)保留全实例停写/清理及真实恢复缺口。脚本回归只验参数、身份拒绝与失败不续行；实际 Compose/PowerShell、迁移事务、health/HTTPS 和业务放行须获准环境验收。
