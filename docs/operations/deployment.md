# 发布、迁移与分阶段放行

已有环境的发布手册。首次准备见[启动](quickstart.md)，恢复资产与数据库替换见[备份恢复](backup-recovery.md)。命令在目标部署目录 `PJM/` 执行；先确认环境、维护窗口、版本与负责人，未知目标不试运行。

## 先分清四种操作

| 操作 | 范围 |
| --- | --- |
| config / preflight | 配置解析 / DB migration 与 Redis PING，不证明业务就绪 |
| build / export / load | 构建镜像 / 打包本地镜像 / 导入 archive，不是备份或应用验收 |
| migration | 改变 DB；失败后查实际 revision，不用 stamp 掩盖 |
| 启动 / 放行 | 进程启动可能消费旧工作；后台恢复和普通业务开放分别批准 |

## 一个例子：关闭 dispatch 后仍有工作

已入 Redis 的 Effect 和即将到期的 Schedule，不会因 `PROJECTMIND_WORKER_DISPATCH_ENABLED=false` 自动停止：

| 入口 | false 时的实际行为 |
| --- | --- |
| Outbox relay | 不选择新的 Run/Effect dispatch topic，lifecycle 通知仍可配送 |
| 已入队 Run/Effect job | 仍可进入 claim/executor；不检查该开关 |
| Schedule / recovery cron | 仍可创建 Run、处理期限和恢复 |
| Skill 解释/调整 job | 独立入口，仍可能调用模型 |

入口见 [WorkerSettings](../../PJM/backend/src/projectmind/worker/settings.py)。开关不是维护模式，不终止远端请求；修改环境文件也不使现有进程即时重载。停写需阻止全部业务入口/触发、处理在途工作并确认所有实例的 API/Worker 写入者停止。远端结果未知时隔离并按[原 Effect 身份对账](runbook.md#incident-与-recovery)，不换键重做。

## 环境文件与配置边界

[Makefile](../../PJM/Makefile) 和镜像导出统一调用 [compose.py](../../PJM/scripts/compose.py)。宿主需要 Python 3.12 标准库，无需 Backend 依赖；Windows 导出可指定 Python executable。

| 选择 | 唯一来源 |
| --- | --- |
| 环境文件 | 显式 `--env-file` → shell `ENV_FILE` → `PJM/.env`，解析为同一绝对文件路径，供插值和 API/Worker/migrate 的 env_file 使用 |
| Compose project | 显式 `--project-name` → shell `COMPOSE_PROJECT_NAME` → projectmind；**文件内同名变量不选择目标**，旧的非默认部署须显式指定 |
| Compose 文件 / 目录 | 固定本套代码的 compose.yaml / PJM；拒绝旁路 file、project、profile 选择器 |
| 应用镜像 | 普通入口固定默认 tag；发布入口显式 pin Backend/Web 的完整 sha256 image ID，三 Backend service 共用同一 ID |

例如 `python3 scripts/compose.py --env-file /controlled/config/app.env --project-name approved-project -- config --quiet`。相对文件路径按 `PJM/` 解析，不受调用目录漂移影响；缺失/空路径拒绝，不回退另一文件。不要手设内部 `PJM_COMPOSE_ENV_FILE` 或绕过入口直接调用 Compose；compose.yaml 在缺少该来源时拒绝解析。

普通 shell 变量仍优先于文件的插值，service.environment 优先于 env_file；不解析或 source dotenv 来改变 Docker 语义。切换 ENV_FILE 不会自动改写数据库 URL、容器、已运行进程或外部资源，仍须逐项核对目标。详见 [Compose 插值规则](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)和 [env_file](https://docs.docker.com/reference/compose-file/services/#env_file)。

日常校验用 `make config`（quiet），不是业务就绪证明。完整 config、config --environment、容器 env/inspect 可能泄露 Secret，不贴共享报告。发布工具内部比较有效环境，但不输出值或保存配置副本。

文档存储须显式配置 `PROJECTMIND_OBJECT_STORAGE_NAMESPACE_ID`，同一存储世代的 API/Worker 共用并随恢复清单保存；未指定不开放文档 blob 操作，health/preflight 成功不能替代此项核验。UUID 与 endpoint/bucket 描述共同校验，新建存储不能沿用旧 UUID；凭据轮换不改身份。旧文档不自动绑定，升级前核对[存储归属与兼容边界](../design/document-lifecycle.md#存储归属与配置切换)，不能以更新配置代替历史资产迁移。

## 迁移前置与执行

先完成独立环境验收，取得[一致恢复点](backup-recovery.md#一致恢复点包含什么)，保留前后 image ID/archive；API/Worker/migrate/Web 使用经过验证的兼容组合。

```text
恢复点 → 全实例停写/在途对账 → 加载镜像 → 查 revision/head
  → 单独迁移 → API/Web 核对 → 批准恢复后台 → 验收 → 普通入口放行
```

发布分为四个独立命令，不把前一阶段成功文件当作放行凭证。先从受控发布清单取得并确认以下变量；不要将示例身份照抄到真实环境：

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

也可直接用 `python3 scripts/deploy.py <phase> --help` 查看等价参数。Make 使用 shell 中已明确导出的上述变量，缺少确认/身份会拒绝。load 不删除容器或旧镜像；先验证 tar checksum、两个必需 tag 的唯一 config digest，再从同一已打开文件导入并按 ID 检查，不能靠本地旧 tag 补 archive 缺项。不接受缺 manifest、含糊的多平台 application 映射；应先产出目标平台的可验证 archive。

migrate 前需 PostgreSQL/Redis 健康、storage 运行且本配置 bucket 初始化成功；这些基础服务不由发布命令隐式启动。工具枚举同 daemon/project 的全部容器，包括 orphan 和 one-off，拒绝活动业务写入者。迁移前用目标镜像的 `preflight --migration-plan` 读取全部 revision、检查单 head 和合法前进路径；只有随后单独 upgrade 与 DB head/Redis 检查都成功，本阶段才完成。

人工排查仍可执行只读 `alembic current` / `alembic heads`，但必须使用本次已核实的目标镜像和相同配置。下列普通入口使用默认 tag，只有 tag 的实际 ID 已与发布清单核实时才执行：

```bash
python3 scripts/compose.py -- run --rm -T --no-deps --pull never migrate alembic current
python3 scripts/compose.py -- run --rm -T --no-deps --pull never migrate alembic heads
```

revision 不在目标链、多 head 或检查失败时停止。失败后核对实际已提交 revision，记录脱敏错误；不要手改 alembic_version。此前 revision、非事务操作和外部数据不随后一项失败全部回滚，先决定 forward fix 或完整恢复。

## 迁移与回退审查

每次发布审查[实际 upgrade/downgrade](../../PJM/backend/migrations/versions/)和消费者，不从下表推导可任意降级：

| revision | 回退限制 |
| --- | --- |
| 0018–0021 | 组织共享不能还原成单 Project；新结果包络、Segment/Interaction/Effect 不由旧 Worker 自动兼容 |
| 0022–0024 | 降级可能丢用户偏好/密文材料；退役来源不可逆导入，已有引用与旧 KEK 必须保留 |
| 0025–0028 | 调度/生成版本/索引与审计可能丢失。0027 在任何 DDL 前锁表，任何子 Session、无 SDK ID、BRANCH 或旧唯一约束冲突都拒绝降级，不再删除审计行；仅可无损表示为旧 PRIMARY 约束的数据可继续 |
| 0029 输入回执 | 任何回执行存在时拒绝降级，不仅 READY；不能删回执绕过 |
| 0030 预算账本 | 三表任一有数据即拒绝降级，包括已结算记录；不为旧 Run 补造账户，无通用核对/退款 CLI |
| 0031 会话 v2 | 旧会话需重登，任何 v2 行（含撤销）阻止丢列；新旧 API 不混跑 |
| 0032 用户生命周期 | 旧用户初始化版本但不伪造事件；任何安全事件阻止降级，API/页面和实 DB 验收仍须单独确认 |
| 0033 成员审计 | 不回填旧加入/移除事件；任何成员事件阻止降级与整项目删除。成员、账户、偏好和项目删除须使用同一组织锁协议，旧写入方不可混跑 |
| 0034 项目版本 | 旧项目初始化版本 1，不代表历史修改次数；任一项目版本超过 1 拒绝丢列。项目 API/Web 成套切换，旧客户端缺原版本返回 422，不自动补版；全实例停写和在途请求核对后才可考虑回退 |
| 0037 文档存储归属 | 旧行保留未绑定，新写必须保存完整身份；任一非空归属信息都阻止降级丢列。旧 API/Worker 绕过该检查，不可混跑；迁移正确不证明旧 blob 可读或可清理 |
| 0038 原上传意图 | PUT 前保存原请求/占用，发布与清理要求不删除它；任何意图或关联阻止降级。POST 新增必填 Idempotency-Key，旧 Web/调用方缺 header 返回 422；API/Web 成套切换，旧 writer 不能混跑 |
| 0039 文档清理记录 | 新旧单文档删除先保存原对象/元信息和 DELETE 身份；旧文档不补造原上传历史。任意清理记录阻止降级，文档/意图/清理均阻止整项目删除；旧删除 writer 不可混跑 |
| 0040 附件字节 | Evidence 新字段整体为空表示未发布，不从工作区回填；任一字段非空都阻止降级。API/Web 须理解 Result checks v2，Worker 与系统 Interpreter/Tool catalog 成套切换；保留 workspace.write/v1，不自动给旧 Skill/Run 升权限 |
| 0041 评价原请求 | 原键/hash 同评价保存，旧行保持空值；任一绑定字段非空都阻止降级丢列。新 Web 使用独立提交/只读确认/分页接口，旧接口不因此获得重放保证；API 与新页面配套发布，不删除评价以绕过回退限制 |
| 0042 停止待发布 | 关闭标记与独立审计一起保存，不改原上传回执或占用；任一关闭标记/审计都阻止降级。新 API/Web 成套发布；DB CHECK 阻止忽略新列的旧 SQL 发布已关闭意图，不阻止旧 PUT，也不替代全实例停写/存储对账 |
| 0043 预算启动所有权 | 旧行不补造 owner，新绑定同时保存启动 token hash；任一 owner 痕迹阻止降级丢列。旧预算调用方不校验所有权，不可混跑；迁移与 token hash 不证明模型未启动、停止或计量完整 |

新表存在不是功能接入或生产升级许可。旧页面可读也不证明非终态 Run 可续行；预算另满足[计量与混合 Worker 门禁](../design/run-budgets.md#上线门禁与接线顺序)。兼容不明时保持停写，走[版本回退](backup-recovery.md#应用版本回退)，不清审计、快照或未决占用来凑条件。

## 会话协议切换检查

1. 确認所有 API 实例和流量入口，通知重登并保全未确认业务动作的 ID/key；排空并停止旧实例。
2. 在授权隔离环境验证旧行升级、新登录、失效与降级拒绝；迁移 head 正确后才启动新 API。
3. 新实例使用同一 v2 参数，通过真实 HTTPS 验证多页面/多实例稳定 CSRF、失效拒绝与 no-store；不采集 Cookie/CSRF 原值或使用真实用户密码探测。
4. 失败保持入口关闭，选择 forward fix 或完整恢复；不以回旧镜像、改默认 version、删会话替代兼容方案。

恢复旧备份还须防止已撤销会话重新有效。Web 字段仍是字符串不证明旧 Web 已兼容；后台工作放行与浏览器重登分别审核。协议正本见[认证](../design/authentication.md#会话凭据-v2-与切换要求)。

## 启动与放行

保持 Worker 停止、普通入口关闭。迁移阶段成功后，使用同一清单与已确认的维护变量，只启动 API/Web：

```bash
make deploy-api
make
```

`make run` 现为 deploy-api 别名，不再整体启动 Worker。工具固定 --no-build/--no-deps/--pull never，等待 API/Web health，复验实际 image ID、有效环境和 preflight。必需服务或初始化容器缺失、配置与现有容器不同、检查失败时停止，不删参数绕过。preflight 不验 blob、KEK、认证、模型或恢复；health 也不证明业务验收。此阶段核对权限、Run/Result/Evidence、blob 与缺失输入；授权 GET 仍可能更新 session idle，不等于备份所需完全停写。

后台放行前，核对旧队列、在途 Run/Effect/Schedule，并获准恢复该环境所有后台工作；这不是 MAINTENANCE_CONFIRMED 的隐含授权。随后执行：

```bash
make deploy-worker BACKGROUND_APPROVED=1
```

工具重新检查目标、API/Web 配置/镜像/health 和 preflight，再仅启动 Worker。Worker 没有独立业务 healthcheck，运行状态不证明旧工作已经核清。入口只开放给已批准的[smoke](runbook.md#通常-smoke)；验证通过再人工开放普通业务，失败重新隔离并保留已产生事实。

smoke 需要运行中的 Worker，会写入/计费。优先在 DB、Redis/队列、存储、Worker 和凭据均隔离的验收环境完成；单独建 Project 不足以隔离旧 job/cron。当前无“只消费测试 Project”的模式，不能为跑 smoke 跳过后台恢复许可。

任一阶段超时、失败或中断都不自动续行、回滚或重发。load 可能已导入部分镜像，migration 可能已提交，up 可能已启动部分服务；先核对原事实。重新调用 API/Worker 阶段不会把仍运行的业务容器当成“上次成功”自动跳过，需要先明确检查与处理。

## 后续开发约束与验收

发布脚本的本机状态检查不是跨实例锁或权限系统，不控制共享 Traefik 的入口、其他 daemon、外部进程或远端效果。独占维护期间不得并发修改配置、镜像、Docker context 或启动其他写入者；确认标志只是操作者声明，不是全局停写证明。

[计划 R11](../planning/roadmap.md#r11-运维与工程工具)继续负责统一全实例停写、清理及真实恢复验收。脚本回归证明参数、身份拒绝与失败不续行；Docker Compose 的实际注入、PowerShell 导出、迁移锁/事务、镜像 health、HTTPS 和业务放行须在获准环境分别验证。
