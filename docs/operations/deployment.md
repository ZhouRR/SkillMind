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

[Makefile](../../PJM/Makefile) 的 `ENV_FILE` 只供 Compose `--env-file` 插值；[compose.yaml](../../PJM/compose.yaml) 的 API/Worker/migrate 仍固定使用 `env_file: .env`。`ENV_FILE=.env.production` **不会同步切换 Backend 设置**，可能造成 DB 容器参数与应用连接来源混用。service.environment 优先于 env_file，shell 也可能覆盖插值。

现阶段使用独立部署目录及其已确认的 `.env`，不要只换 ENV_FILE 操作另一环境，也不把生产配置覆盖进开发目录。合法性检查用 `docker compose --env-file .env config --quiet`；通过仅说明可解析。完整 config、config --environment、容器 env/inspect 可能泄露 Secret，不贴入共享报告。

配置修正需让插值与三个 Backend service 使用一致来源，同步 Make/Compose/脚本，并用无敏感值的两套配置验证默认/自定义路径、缺失拒绝、shell 覆盖和实际注入。详见 [Compose 插值规则](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)及 [env_file](https://docs.docker.com/reference/compose-file/services/#env_file)。

## 迁移前置与执行

先完成独立环境验收，取得[一致恢复点](backup-recovery.md#一致恢复点包含什么)，保留前后 image ID/archive；API/Worker/migrate/Web 使用经过验证的兼容组合。

```text
恢复点 → 全实例停写/在途对账 → 加载镜像 → 查 revision/head
  → 单独迁移 → API/Web 核对 → 批准恢复后台 → 验收 → 普通入口放行
```

以下命令启动一次性容器并连接目标 DB。先确认目标镜像和配置，head 以镜像迁移链为准：

```bash
docker compose --env-file .env run --rm migrate alembic current
docker compose --env-file .env run --rm migrate alembic heads
```

revision 不在目标链、多 head 或检查失败时停止。确认后单独迁移：

```bash
docker compose --env-file .env run --rm migrate
```

成功前不启动应用。失败后用 alembic current 核对已提交 revision，记录脱敏错误；不要手改 alembic_version。此前 revision、非事务操作和外部数据不随后一项失败全部回滚，先决定 forward fix 或完整恢复。

## 迁移与回退审查

每次发布审查[实际 upgrade/downgrade](../../PJM/backend/migrations/versions/)和消费者，不从下表推导可任意降级：

| revision | 回退限制 |
| --- | --- |
| 0018–0021 | 组织共享不能还原成单 Project；新结果包络、Segment/Interaction/Effect 不由旧 Worker 自动兼容 |
| 0022–0024 | 降级可能丢用户偏好/密文材料；退役来源不可逆导入，已有引用与旧 KEK 必须保留 |
| 0025–0028 | 调度/生成版本/索引与审计可能丢失；0027 downgrade 会删无 SDK ID 的 Session，再恢复唯一约束，不在有子会话数据时试跑 |
| 0029 输入回执 | 任何回执行存在时拒绝降级，不仅 READY；不能删回执绕过 |
| 0030 预算账本 | 三表任一有数据即拒绝降级，包括已结算记录；不为旧 Run 补造账户，无通用核对/退款 CLI |
| 0031 会话 v2 | 旧会话需重登，任何 v2 行（含撤销）阻止丢列；新旧 API 不混跑 |
| 0032 用户生命周期 | 旧用户初始化版本但不伪造事件；任何安全事件阻止降级，API/页面和实 DB 验收仍须单独确认 |
| 0033 成员审计 | 不回填旧加入/移除事件；任何成员事件阻止降级与整项目删除。成员、账户、偏好和项目删除须使用同一组织锁协议，旧写入方不可混跑 |

新表存在不是功能接入或生产升级许可。旧页面可读也不证明非终态 Run 可续行；预算另满足[计量与混合 Worker 门禁](../design/run-budgets.md#上线门禁与接线顺序)。兼容不明时保持停写，走[版本回退](backup-recovery.md#应用版本回退)，不清审计、快照或未决占用来凑条件。

## 会话协议切换检查

1. 确認所有 API 实例和流量入口，通知重登并保全未确认业务动作的 ID/key；排空并停止旧实例。
2. 在授权隔离环境验证旧行升级、新登录、失效与降级拒绝；迁移 head 正确后才启动新 API。
3. 新实例使用同一 v2 参数，通过真实 HTTPS 验证多页面/多实例稳定 CSRF、失效拒绝与 no-store；不采集 Cookie/CSRF 原值或使用真实用户密码探测。
4. 失败保持入口关闭，选择 forward fix 或完整恢复；不以回旧镜像、改默认 version、删会话替代兼容方案。

恢复旧备份还须防止已撤销会话重新有效。Web 字段仍是字符串不证明旧 Web 已兼容；后台工作放行与浏览器重登分别审核。协议正本见[认证](../design/authentication.md#会话凭据-v2-与切换要求)。

## 启动与放行

保持 Worker 停止、普通入口关闭。DB/Redis/storage、bucket 初始化、迁移和本地目标镜像就绪后，只启动 API/Web：

```bash
docker compose --env-file .env up -d --no-build --no-deps --pull never api web
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
make
```

--no-deps/--pull never 避免连带启动迁移等依赖或自动拉镜像；前置不满足时停止，不删参数绕过。preflight 不验 blob、KEK、认证、模型或恢复。此阶段核对权限、Run/Result/Evidence、blob 与缺失输入；授权 GET 仍可能更新 session idle，不等于备份所需完全停写。

后台放行前，核对旧队列、在途 Run/Effect/Schedule，并获准恢复该环境所有后台工作。随后才启动 Worker，入口仅开放给已批准的[smoke](runbook.md#通常-smoke)；验证通过再开放普通业务，失败重新隔离并保留已产生事实。

smoke 需要运行中的 Worker，会写入/计费。优先在 DB、Redis/队列、存储、Worker 和凭据均隔离的验收环境完成；单独建 Project 不足以隔离旧 job/cron。当前无“只消费测试 Project”的模式，不能为跑 smoke 跳过后台恢复许可。`make deploy` 整体重启，不能实现分段门禁。

## 后续开发约束与验收

[计划 R11](../planning/roadmap.md#r11-运维与工程工具)负责统一停写与发布编排：覆盖全部实例/API/job/cron；阶段失败不放行；配置来源和实际 image 一致；恢复后重新核对权限、队列、非终态输入与远端效果。同步 Make/Compose/脚本、Settings/startup、受影响服务和测试；只在公开字段或持久数据改变时同步契约/迁移。
