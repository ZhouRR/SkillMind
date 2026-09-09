# 备份、恢复与版本回退

[运维入口](runbook.md#按问题找入口) · [发布与迁移](deployment.md) · [Secret 与旧密钥](../design/secret-storage.md#切换与恢复的顺序)

本页负责恢复点、备份检查、数据库替换和恢复后的放行证据。命令均在目标部署目录 `PJM/` 执行，需要已确认的环境和获批维护窗口；不知道目标或缺少恢复材料时停止，不试跑破坏性命令。

## 先选路径

| 目的 | 应走的步骤 |
| --- | --- |
| 更新前保留退路 | [一致恢复点](#一致恢复点包含什么) → [数据库备份](#取得并检查数据库备份) → 保存完整受控资产 |
| 只替换应用版本 | 先做[兼容审查](deployment.md#迁移与回退审查)，再选[版本回退路径](#应用版本回退)；不默认恢复数据库 |
| 替换损坏或不兼容的数据 | [恢复前置](#恢复前的停止条件) → [数据库替换](#替换数据库) → [关联与放行验证](#恢复后验证) |

备份清单不含 Secret 值，不代表备份资产不敏感。dump 可含账号密码 hash、加密材料和业务记录，blob/workspace 也可能含业务正文；它们均需访问控制和受控保管，不能嵌入文档浏览版、截图或公开附件。KEK 独立保护，不能仅因数据库中保存的是密文而公开 dump。

## 一个例子：恢复不能抹掉后来的事实

```text
10:00 取得完整恢复点
10:05 一次会话被撤销，或用户权限被收回
10:07 外部仓库接受了已批准的 commit
10:10 决定恢复 10:00 的本地数据
```

恢复后，数据库可能重新含有旧的会话/权限状态，但远端 commit 仍然存在。两类事实都不能从“dump 导入成功”得出安全结论：前者需要防止旧权限重新开放，后者需要按原 Proposal/Effect 身份对账，防止重复写入。

因此先保全当前安全/外部变更事实，再恢复，在关联核对完成前保持业务隔离。当前没有通用的会话恢复撤销、Effect 补账或跨存储恢复 CLI；不能靠直接 SQL、重跑 bootstrap 或换幂等键补造完成。会话目标见[失效与权限变化](../design/authentication.md#会话失效与权限变化)，外部对账见[Effect 恢复](runbook.md#87-incident-と-recovery)。


## 配备前备份

先阻止新业务请求与调度触发，等待正在执行的外部 Effect 结算；结果不明时转到[Effect 恢复](runbook.md#87-incident-と-recovery)。随后停止 API/Worker 等写入者，在停写窗口内取得 DB、blob 和必要 workspace 的同一恢复点。仅停止新 Run 创建、仅关闭业务 dispatch 开关或只给文件相同时间戳，都不能证明已无写入。

### 一致恢复点包含什么

| 保存对象 | 用途与必须核对的关联 |
| --- | --- |
| DB dump | PostgreSQL 的业务事实、快照、Outbox、批准与审计；核对数据库身份、migration revision 和停写时点 |
| blob snapshot | object storage 中的 SkillSource、Project 文档及其他 DB 引用；保留原对象键、字节与 hash，不能只备份最终报告 |
| Run 文件 | 必要 workspace/transcript、输入副本和会话恢复材料；对应 Run/Session，缺文件不能标为可续行 |
| 镜像与配置版本 | 以兼容代码读取恢复数据；核对 Backend/Web image ID、依赖 image 和 context path，配置凭据另行保护 |
| KEK 及版本 | 用于解封 MANAGED 凭据；由独立受控保管系统保留，不与 DB dump 放在一起 |
| 外部对账记录 | 防止重做已发生的 Effect；保留原 Proposal/Effect 身份、目标 revision 与 read-back 状态 |

Redis 不是上述数据的替代品。恢复时如何隔离旧队列、处理 Outbox 重投和调度在途，应纳入专用演练；不能对未知 Redis 使用全库清空命令，也不能假定恢复旧 DB 后现有队列自然一致。

备份清单只记录恢复点 ID、UTC 停写窗口、上述受控资产引用/校验值、操作者、验证结果和未覆盖项。配置与 KEK 的清单记录版本引用，不记录值。保留期限、RPO（最多允许丢失的数据时间）和 RTO（目标恢复耗时）由环境负责人明确；当前没有自动跨存储备份、统一保留清理或已验证的 RPO/RTO 保证。

### 取得并检查数据库备份

下面是经确认的停写窗口内的 DB 备份示例，不替代其他存储的 snapshot。`mktemp` 创建本次独立目录，避免覆盖同名备份；创建失败不执行 dump，变量为空也拒绝输出。父目录和资产存放位置应由操作者控制，密码不写入命令行。两段示例在同一 shell 执行；新开终端先明确原备份目录，不猜测变量。

```bash
umask 077
PJM_BACKUP_DIR="$(mktemp -d ./projectmind-backup-XXXXXXXX)" &&
docker compose --env-file .env exec -T postgres sh -ceu \
  'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
  > "${PJM_BACKUP_DIR:?Backup directory is required}/database.dump"
```

只有 `pg_dump` 退出码为 0 才继续。失败留下的文件保留为故障现场，不标记为有效备份。随后检查非空、记录并核对字节校验值，验证 archive 目录可解析；单凭这些检查仍不能代替实际恢复演练。

```bash
(
  set -eu
  : "${PJM_BACKUP_DIR:?Specify the directory created for this backup}"
  test -s "$PJM_BACKUP_DIR/database.dump"
  docker compose --env-file .env exec -T postgres pg_restore --list \
    < "$PJM_BACKUP_DIR/database.dump" > "$PJM_BACKUP_DIR/database.toc"
  docker image inspect projectmind/backend:0.1.0 projectmind/web:0.1.0 \
    --format '{{.RepoTags}} {{.Id}}' > "$PJM_BACKUP_DIR/images.txt"
  cd "$PJM_BACKUP_DIR"
  sha256sum database.dump > database.dump.sha256
  sha256sum --check database.dump.sha256
)
```

此检查块在子 shell 中遇错即停，不更改当前终端的工作目录。TOC 可能含业务对象名，只保存在受控目录。将 DB、blob/workspace snapshot、配置引用和镜像纳入同一备份清单并转存到批准的保管系统。checksum 文件使用目录内相对路径，便于整体转存；它用于发现字节变化，不是备份来源的真实性证明。

完整恢复点就绪前不删除旧镜像或启动 migration。`make bootstrap-admin` 会删除所有 volume，不属于备份、更新或常规管理员创建步骤。

## 数据库恢复

这是删除并替换指定数据库的破坏性恢复，会丢失恢复点之后的本地数据。先获得环境负责人确认并保全当前现场；通常先在隔离环境演练，不直接试生产。恢复旧数据库不会撤销已经写到 Redmine、Git/SVN 或 forge 的内容。

### 恢复前的停止条件

以下任一项不满足，就不执行后面的数据库替换：

- 环境、Compose project、DB 名称/角色、目标镜像和预期 revision 都已明确，且不是凭目录名推断。
- dump 来源可信、checksum 与 archive 检查成功，对应 blob/workspace、配置版本及必要 KEK 齐全。
- 当前现场另有可恢复备份；维护窗口覆盖 API/Worker、调度和外部 Effect，新写入已停止。
- 已记录恢复点以后可能发生的外部变更，并有原幂等身份的对账方案。

把本次选定的完整备份目录设置为 `PJM_RESTORE_DIR`。只读校验块不会修改数据库；需要整体保留[数据库备份](#取得并检查数据库备份)生成的文件名和相对路径。

```bash
(
  set -eu
  : "${PJM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$PJM_RESTORE_DIR/database.dump"
  docker compose --env-file .env exec -T postgres pg_restore --list \
    < "$PJM_RESTORE_DIR/database.dump" > /dev/null
  cd "$PJM_RESTORE_DIR"
  sha256sum --check database.dump.sha256
)
```

校验通过后停写，并在受控终端核对当前连接的数据库与角色；不要把完整连接设置贴到公共报告：

```bash
docker compose --env-file .env stop api worker migrate
docker compose --env-file .env exec -T postgres sh -ceu '
  psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc \
    --set=ON_ERROR_STOP=1 --command="SELECT current_database(), current_user"
'
```

### 替换数据库

只有操作者确认上述结果与目标完全一致后，才执行此块。例子以原应用角色恢复对象；自定义多 owner/额外权限的环境需先制定对应恢复方案。子 shell 遇错即停，失败后保持业务关闭。

```bash
(
  set -eu
  : "${PJM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$PJM_RESTORE_DIR/database.dump"
  docker compose --env-file .env exec -T postgres sh -ceu '
    dropdb --force --username="$POSTGRES_USER" "$POSTGRES_DB"
    createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" "$POSTGRES_DB"
  '
  docker compose --env-file .env exec -T postgres sh -ceu '
    pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
      --no-owner --single-transaction --exit-on-error
  ' < "$PJM_RESTORE_DIR/database.dump"
)
```

[PostgreSQL 17 的 pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html) 默认遇到 SQL 错误会继续处理；这里指定单事务与遇错退出，避免把部分导入当成成功。单事务只覆盖导入，不撤销此前的 dropdb/createdb，更不回滚 blob 或外部系统；失败时可能只剩空数据库。大型数据库若需其他导入策略，先在恢复演练中验证，不临场删掉错误保护参数。

### 恢复后验证

恢复与 dump 对应的 object storage/workspace，加载兼容 image 和配置，让所需旧 KEK 可解封。先核对 revision，再依[迁移审查](deployment.md#迁移与回退审查)决定是否 forward migration；保持 Worker 与外部写入隔离，不能直接运行 `make deploy` 自动放开后台任务。

| 验证层 | 放行前应取得的证据 |
| --- | --- |
| 基础与权限 | 目标 migration head、DB/Redis 连接；恢复的用户/成员/Session 状态已经核对，不把过期权限重新开放 |
| 数据关联 | 抽样 Skill/文档/Artifact blob 可读且内容匹配；Run/Result/Evidence 引用一致 |
| 可续行性 | 终态历史可读与非终态可继续分别验证；缺快照/回执/transcript 时明确拒绝，不补签或重新授权 |
| 外部与在途工作 | 对账恢复点后的 Effect；确认 Outbox/队列、Schedule 在途和旧 lease 的处置，不重发重复写入 |
| 专项与交接 | 专用环境 smoke/恢复场景、实际恢复耗时、数据损失范围、残余问题和放行责任人 |

任一不明项保持隔离，不能以 `preflight=ready` 代替业务放行。当前无统一恢复编排 CLI；上述关联验证和外部对账仍需经过授权的运维流程完成。

## 应用版本回退

先选择回退路径，不把 image 回退和数据恢复混成同一条命令：

| 已确认的条件 | 可采用的路径 |
| --- | --- |
| 旧 API/Web/Worker 理解当前 schema、数据、队列和非终态快照 | 保持数据，按维护流程切换到兼容旧镜像并重新验证 |
| 旧代码不理解新数据，或兼容性未确认 | 不启动旧 Worker；评估完整恢复点和外部对账，经确认后按[数据库恢复](#数据库恢复)处理 |
| 没有可验证的完整恢复点 | 停止回退；保全现场并选择 forward 修复，不试跑破坏性 downgrade |

只有第一行且允许整体重启、旧队列/在途工作均已核对时，可指定已经保留并验证的旧 archive。以下 deploy 会立即启动 Worker；后面的 preflight/smoke 是重启后的检查，不能作为启动前闸门。仍需分阶段放行时改走[发布手册](deployment.md#启动与放行)，不运行此块：

```bash
make deploy IMAGE_ARCHIVE=images/projectmind-previous.tar
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
docker compose --env-file .env exec -e PROJECTMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" api python -m projectmind.ops.smoke
```

`make deploy` 会先移除容器和旧应用 image，再 load archive 并重启；如果 archive 缺失必要 image，服务不会自动回到原版本。必须事先保管可用的前后两代 archive/image ID，不把保留同名 tag 当作回退方案。
