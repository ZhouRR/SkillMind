# 资源快照与 Run 工作区

> 用途：定义一次 Run 可以读取哪些资源、何时固定内容，以及重试时如何保持一致。前置阅读：[领域模型](domain-model.md)、[Runtime 文件边界](agent-runtime.md#62-文件shell-和-web)。实现进度与验收证据只维护在[计划 R01](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)。

先按问题阅读：[选择哪些文档](#文档选择与冻结设计) → [仓库固定哪个版本](#仓库授权与内容版本) → [Agent 看到的路径](#产物与访问) → [输入何时可以使用](#输入准备与可信缓存)。选择清单、文件准备完成和 Run 成功是三个独立事实。

## 先区分三种“冻结”

| 层次 | 固定什么 | 不能由此推导什么 |
| --- | --- | --- |
| 授权快照 | Project、资源身份、capability、允许的 scope、Integration 配置版本 | 冻结分支名不等于固定 commit；获得读取权不代表内容始终可达 |
| 内容快照 | 文档 ID/hash/成员清单，或仓库某次读取解析出的具体 revision | checksum 不是授权凭据；不可变引用也不是 blob 的永久备份 |
| 物化副本 | 本次实际取得的文件、转换结果、索引与 skipped，写入只读 input | 首次准备的副本不自动证明“Run 创建时已经取得全部内容” |

这三个时点必须分别记录。不能用“binding 已冻结”概括所有资源的内容一致性。

## 实际资源范围

| 资源 | 读取与物化入口 | 当前能依据代码确认的边界 |
| --- | --- | --- |
| repository | Run binding → repository source/client → `input/<requirement_key>/`；也可调用 `repository.read/v1` | 创建时冻结授权与 revision 选择规则；具体 commit/SVN revision 在打开资源时解析，物化 manifest 记录该次解析值 |
| document | Run sources → 文档快照 → document source/物化器 → `input/documents/` | 创建时固定 ID/hash 与成员；即时/调度表单显式选择，Run detail 展示服务端验证后的原清单，不从当前目录补齐 |
| issue | `issue.read/v1` 在合法 binding 范围内逐条读取 | 不物化文件树；每次读取形成带来源/版本的 Evidence，不承诺外部票据内容永久不变 |

旧 document 实现于首次准备时读取 Project 全集；工作副本已改为消费 Run 的冻结清单。公开选择/投影有既有局部回归，但输入回执重构正在改变物化接口，当前不能把旧回归视为整条执行链可用的证明。接线差距见[计划](../planning/roadmap.md#13-当前执行状态)；核对部署时须另外记录镜像/版本。

## 文档选择与冻结设计

### 用户选择的是范围，不是 Provider 名称

| 选择 | Run 创建时的含义 | 后续变化 |
| --- | --- | --- |
| 单份文档 | 固定一个 Project 内的 document ID 与内容 hash | 同路径删除再上传的新 ID 不替代原文档 |
| 文档集合 | 固定所选 ID 集合；拒绝重复项、规范排序并校验归属 | 不追加未选文档 |
| 显式项目全集 | 用户确认后固定该次创建读取到的成员集合 | 排队期间与之后新增文档不进入本 Run |
| 可选 requirement 不选择 | 没有该槽位的文档读取授权 | 不隐式退回全集或任意默认 Provider |

必需 requirement 缺少合法选择时拒绝创建，不先创建一个权限不完整的 Run 再在对话中扩权。多个文档槽位可以共享 `input/documents/` 下的去重并集；Agent 的文档可见范围是这个 **Run 级并集**，不是槽位之间互相隔离。manifest 仍须保留各 requirement 的成员关系，重复 ID 的元数据必须一致。

### 一次执行的时序

```text
Preflight 展示候选/范围（暂态，不是冻结证据）
  → 服务端创建事务：校验 actor/Project/选择，保存文档清单与 hash
  → Worker 准备：按原 ID 取得原内容，验证实际字节，再写 manifest
  → Agent 读取：只读冻结范围，Evidence 引用实际内容
  → 新 Segment / Attempt：继续使用同一清单，不重新枚举 Project
```

冻结信息由服务端生成，至少覆盖 Project、requirement、选择模式、document ID、路径、MIME、size、content hash 和快照 checksum。客户端不能自行提交一个 checksum 就成为可信快照。

内部快照保存在 Run 的 `selected_sources_json`。下面定义现有创建请求和读取响应的语义；精确形状及代表数据见[契约](../../PJM/contracts/README.md#run-文書契約を読む)。不要给普通用户暴露选择串，也不要把内部快照 JSON 直接变成公开响应。当前回归与部署的区别见[计划](../planning/roadmap.md#13-当前执行状态)。

### 公开选择与读取投影的实施契约

采用现有 `sources` 字符串字段，不新增平行创建 API。用户看到文件名和范围；编码由客户端依据本槽位候选形成，服务端仍须独立校验归属与有效性。

| 用户确认 | 提交语义 | 约束 |
| --- | --- | --- |
| 单份 | `document:<UUID>` | 使用 TaskCatalog 的文档候选键；恰好一个 ID |
| 集合 | `documents:<UUID>,<UUID>…` | 同槽位候选中的 2–5000 个不同 ID；不是任意路径列表 |
| 全集 | `project-documents:all` | 显式确认；创建事务固定当时的 1–5000 个成员，空项目或超量均拒绝，不截断 |
| 不使用 | 不提交该可选槽位 | 必需槽位不允许；空串和未完成的集合不代替合法选择 |

文档不自动选择首个候选，即使只有一个文件。即时执行与 Schedule 保存使用同一输入/范围组件，允许配置实际输入，不从任务列表直接拿 `{}` 与首个候选代替用户确认。候选刷新、切换选择模式或删除已选文档后，保留可解释的草稿状态并要求修正；不偷偷改选全集、替换 ID 或把只剩一个成员的集合当成有效集合。原请求的重发另用已发送内容，不受后来草稿有效性影响，见[创建恢复](run-creation.md#提交结果未知时的界面责任)。

### 用一个例子理解冻结边界

假设项目起初只有 A、B 两份文档，用户选择“全集”并成功创建 Run 1，随后上传 C：

| 接下来的操作 | 应看到的文档范围 | 原因 |
| --- | --- | --- |
| Run 1 排队结束，或同段故障重试 | A、B | Worker 沿用 Run 1 已保存的成员，不重新枚举 |
| 创建响应丢失，重发原请求 | 仍是 Run 1 的 A、B | 这是确认原创建，不是重新授权 |
| 用户明确新建 Run 2，仍选全集 | A、B、C | 新 Run 独立校验并冻结新的集合 |
| 同一 Schedule 的下一次 occurrence | 当次新 Run 创建时的全集 | Schedule 保存规则，每次成功触发各自冻结 |

如果 A 被删除后以同一路径上传成 A′，Run 1 的原 ID 不随路径替换；缺少原内容时明确失败。查看 Run 1 的历史详情仍展示其创建时的清单，不表示 A 今天仍可下载。

### 读取清单和资源摘要

Run detail 使用 `document_snapshots` 逐槽位投影，包含 `requirement_key`、`status` 和 `snapshot`：

| 状态 | 可公开的 snapshot | 界面应该说明什么 |
| --- | --- | --- |
| `FROZEN` | 经 Project/slot/版本/checksum 与成员一致性验证的 v1 清单 | 创建时确认的模式、成员、路径与校验信息；不是当前 Project 目录 |
| `LEGACY_UNAVAILABLE` | null | 旧记录没有可验证清单；不推测当时读了哪些文件 |
| `INVALID` | null | 清单校验失败，不能展示未验证成员；其他合法历史结果仍可读取 |

这三种状态仅描述清单的可验证性，不证明 blob 仍可达、物化已完成或 Run 成功。当前格式下空数组表示没有识别到文档来源；旧响应缺少字段不能自动当作空数组。旧记录未知范围、响应版本不匹配和合法的不选是不同情况。

`selected_sources` 在 detail/history 都只公开资源摘要：对象仅包含 `provider / capability / resource_kind / access`，已知旧字符串形状保留兼容。不原样发布内部 JSON、binding scope、Secret locator 或未来新增字段；完整成员只在 detail 的清单投影中提供，history 不复制大清单。公开摘要不成为 Provider 的新授权输入。

服务端负责重算并验证快照 checksum；Web 的[运行时 validator](../../PJM/web/src/api/runResources.ts)检查响应形状、Project/slot、成员与状态关联，不另算一套服务端身份。当前 Web 对缺少必需 `document_snapshots` 的旧 API 响应报契约错误，而不是显示空清单；“新 API 读取旧 Run”则使用上表的明确历史状态。两端版本兼容与发布顺序见[公开契约联调](../development/contract-workflow.md)。

后续修改须继续同步 Backend response model、Schema/example/OpenAPI、Web validator、Run 详情和三语。没有 DB schema 变化时不机械新增 migration；现有 Run 创建快照及 hash 不改写。可信缓存回执与跨根总量按后文继续联调和验收，不混入创建请求的公开投影。

### 失败、缓存与历史

| 情况 | 设计要求 |
| --- | --- |
| 首次读取时 ID 已删除、blob 缺失、元数据或实际字节 hash 不符 | 准备失败；不换成同路径的新文档，也不当作普通 skipped 悄悄继续 |
| 内容合法但格式不支持或单文件过大 | manifest 记录 skipped 及原因，结果说明未覆盖范围 |
| 已存在完整物化副本 | 校验 Project/Run、冻结集合、来源 hash、实际文件及平台生成文件后复用；不要求重新读取当前 Project 全集 |
| 副本不完整、被改写或混入额外文件 | 明确失败；不静默删除现场并重建 |
| live document Tool 无法取得原 ID | 返回不可用；不能回退读取更新内容。已有的合法物化证据与 live 可达性是两回事 |
| 历史终态 Run 没有文档清单 | 保持原记录可读，标明范围信息缺失，不补造历史选择 |
| 旧非终态 Run 无可信清单 | 不自动授权项目全集；升级前明确处置，需要新输入时创建新 Run |

删除 Project 文档不是清除既有 Run 证据的同义操作；历史 blob/workspace 的保留和删除必须由独立保留策略决定，不能借恢复流程改写历史。

文档缓存的内容校验仍须覆盖原 ID/hash、槽位成员和转换来源。所有资源共用的回执、完整文件树与崩溃规则统一见[输入准备协议](#输入准备与可信缓存)，不能只校验同一目录中的 manifest 与文件彼此一致。

### 创建重放与调度

Run 创建的幂等重放必须保留首次成功创建的快照。“用户选择全集”是稳定的请求意图，“首次冻结了哪些成员”是执行事实，两者不能混成同一份动态 hash 输入。工作副本已分别保存版本化意图与首次快照，并在当前资源解析前查询原请求；Web 已保留原内容/原键进行确认。身份格式、授权、并发胜者、旧记录证明范围与客户端内存边界统一见[Run 创建与幂等](run-creation.md)，不能仅删去 hash 中的安全字段来解决冲突。

Schedule 保存的是选择规则；每个 occurrence 经普通创建服务产生独立 Run。显式“全集”在每个新 Run 创建时冻结，而不是 Schedule 保存时就永久固定成员。同一 occurrence 的重放则必须回到同一个 Run/快照。选择已失效时按[调度失效语义](task-scheduling.md#保存和执行边界)停止，不改用其它来源。

## 仓库授权与内容版本

`scope.paths` 是访问硬边界；`scope.revisions` 是允许读取的 revision 范围。分支名或 `HEAD` 可能解析为不同 commit，空 revision allowlist 也不表示“只许读第一次物化的 revision”。具体选择与检查在 [repository_source.py](../../PJM/backend/src/projectmind/agent/repository_source.py)。

物化器在首次打开时固定该副本的具体 revision 并记录 manifest。需要复现同一基线时，优先读取该副本；额外的 live 仓库读取仍须满足冻结授权，并在 Evidence 中记录实际 revision。跨版本对比必须明确标注两份版本，不能把最新 live 内容与原副本混写为同一个事实。

若任务要求“创建瞬间的仓库内容”，应在创建前选择具体 commit/SVN revision 并验证可达性，或另行实现创建时解析与持久化；仅保存分支名不满足此要求。仓库副本复用时的 binding/Run 身份与额外文件检查也须单独验收，不能把已有 hash 校验当作全部验证。

## 产物与访问

下面是 Agent/Tool 使用的逻辑路径，不要求磁盘上也有一个可替换的同名 `input/` 目录：

```text
Tool 路径空间
├── input/                         Agent 只读
│   ├── documents/
│   │   ├── <原目录>/<文件或转换文本>
│   │   └── .projectmind/          manifest.json、files.txt
│   └── <repository requirement>/
│       ├── <scope 内的文件>
│       └── .projectmind/          manifest.json、files.txt、history.txt
├── workspace/                     Agent 可写的临时工作
└── output/                        Agent 可写的报告/补丁
```

每个物化根有自己的 manifest；document 不生成仓库 history。路径由物化器返回给 Brief，Agent 先读索引和跳过原因，再按需 search/read。

工作副本采用 `RunWorkspace.input_dir = <Run root>/.projectmind-inputs/<snapshot_id>/` 保存不可覆盖的物理世代。`input/documents/a.md` 由平台映射到该世代的 `documents/a.md`，不向 Agent 开放 `.projectmind-inputs/`，也不通过改 `input` symlink 切换。旧 `<Run root>/input/` 的现场不搬入新世代或自行补签。物化、ContextBuilder 与 Tool 已有对应调用；仍须在 Brief、read、search、Evidence 和恢复中成套验证，局部读写测试不覆盖整条准备链。

- 写入仅经 `workspace.write/v1`，不得修改 `input/`、Project 文档库或外部资源。
- `max_files/max_bytes` 保持逐根限制；新总量策略另用独立字段，不能把旧字段悄悄解释为全 Run 额度，见[跨根计量](#跨根总量的修正口径待实现)。
- 单根总体超限使准备失败，不截断后伪装完整成功。单文件与二进制的 skipped 不等于源文件不存在。
- xlsx/xlsm/docx 由[统一转换器](../../PJM/backend/src/projectmind/agent/binary_text.py)转文本，保留源 hash 与位置；PDF 未支持。
- 凭据不写入文件、manifest、Evidence 或 Agent 上下文。只读是 Tool/路径边界共同强制，不能只靠目录名。

## 输入准备与可信缓存

本节同时适用于 document 和 repository，不是文档槽位独有的能力。准备回执描述“整个 Run 的输入是否完整可用”，与 Run 创建时的资源快照分开保存。

### 可信缓存的修正设计（待实现）

本标题保留旧锚点；工作副本已有回执、物化、Worker/Tool 接线与准备监督，尚未完成消费者联调和真实恢复验收。目标威胁模型是 workspace 文件可被改写，而数据库与受控 Worker 身份仍可信；不声称抵抗同时控制数据库和 Worker 的攻击者。只读 Tool、路径隔离和授权检查继续保留，数据库摘要不替代这些边界。

设计单位明确为**一个 Run 一份覆盖全部资源根的回执**，而不是各根分别发布成功。这样最后一个仓库失败或总量超限时，不会把前面生成的文档当作完整输入交给 Agent。每个资源根仍保留自身的 manifest；其摘要和文件摘要一同进入 Run 级完成事实。

| 保存在哪里 | 负责固定的事实 | 不负责证明 |
| --- | --- | --- |
| Run 创建快照 | Project、选择清单、来源 hash、冻结 binding | 文件已经落盘、分支在创建时已解析 |
| 各根 manifest | 实际 revision、来源与转换关系、生成器版本、文件与 skipped | 自己没有被连同文件一起改写 |
| PostgreSQL 回执 | 准备世代、所属 Run/Project/Attempt、冻结来源摘要、全部文件的 path/size/hash、完成时间和总量 | live 内容仍可达、整个 Run 已成功 |
| RunWorkspace / Brief | 受控物理世代与逻辑 `input/` 路径的对应 | 凭据、额外授权或可写的输入目录 |

已有 [DTO/port](../../PJM/backend/src/projectmind/runs/input_snapshot.py)、[repository/store](../../PJM/backend/src/projectmind/runs/repository_inputs.py)和 [0029 migration](../../PJM/backend/migrations/versions/0029_run_input_snapshots.py)以 Run 唯一约束承载这份记录。`PREPARING / READY` 是回执状态，不是新增 Run 状态或公开 API。`READY` 的空文件集合可以表示合法的无物化输入；缺少回执不能投影成同样的空集合。剩余消费者与恢复验证见[实施入口](#已知差距与后续设计)。

### 一次准备的提交边界

```text
有效 Attempt：持续 heartbeat / 取消监督
  ↓
短事务 A：校验来源与执行权 → PREPARING
  锁顺序：Run → Segment → Attempt
  ↓
事务外：独占创建准备世代，按根读取/转换
  检查完整树和总量，同步文件与目录，封闭只读候选
  ↓
短事务 B：重验 lease / 取消 / 准备身份
  提交 READY
  ↓
确认同一世代的完成回执 → PreparedInput
  ↓
冻结 Brief → 启动前再次校验执行权 / 取消
  ↓
Agent / Tool 使用逻辑 input，实际读取仍校验字节
```

候选文件落盘不叫“向 Agent 发布”。只有数据库已提交 `READY` 且副本验证通过，才能完成输入交接；不持锁等待网络、转换或文件同步。来源摘要从 Run 冻结数据产生，文件摘要从受控生成的字节产生，不从一个未知旧目录重新采样来“证明”它可信。

首次准备在封闭候选时校验文件，提交后检查返回回执的世代、状态、来源与文件清单一致；复用分支另外验证现有完整树、字节、总量与 manifest。它们都不是给文件加了永久锁，Tool 返回前仍遵守[同字节读取](#读取时的完整性边界)。`PreparedInput` 携带新的受控 workspace 和资源摘要，消费者不能继续读取传入物化器前的旧 workspace 或把返回值当作资源 tuple。

同一 Run 的后续 Segment/Attempt 只复用已完成且验证通过的世代，不因新 Attempt 获得一次重新选内容的机会。完成提交必须校验准备身份和当前 lease；重复完成只能返回相同事实，不能换 hash 或覆盖胜者。慢 Worker 即使在取消后仍完成一段文件 I/O，也不能提交完成事实或启动 Agent。

准备期 heartbeat、取消、Brief 和最终启动校验属于 [Runtime](agent-runtime.md#74-从领取到模型启动的边界)责任；回执事务的 fencing 是另一层防线，两者缺一不可。ContextBuilder 现有独立[准备 timeout](run-budgets.md#现有计时器的覆盖范围)，到期可能落在回执提交中；超时不证明事务一定回滚，也不许可删除已提交的世代。

### 准备中断与再次使用

下表是目标处理协议，不是现成的运维修复命令。文件系统与数据库不能共同回滚，因此不靠删除现场来伪装两者原子提交。

| 观察到的状态 | 处置与放行条件 |
| --- | --- |
| 没有回执，也没有旧输入或候选目录 | 当前有效 Attempt 可以认领首次准备；此刻还不能交给 Agent |
| 没有回执，却有旧输入或同名候选 | 不使用，保留现场；拒绝覆盖、移动复用或自行补签 |
| `PREPARING`，文件缺失、部分存在或看似完整 | 不使用；仅原有效准备继续正常流程，接管不接着“签完”别的 Attempt；当前无自动修复入口 |
| `READY`，来源、完整树、总量和实际字节均匹配 | 在当前执行授权下可交给 Agent 复用；不再次下载或枚举 Project |
| `READY`，缺文件、出现额外目录/文件或任何 hash 不符 | 不使用，保留回执与文件现场；明确失败，不降级到 live 新内容 |
| 完成提交返回超时/断连，结果未知 | 暂不使用，重读原 Run 的回执确认；只接受已提交的相同世代，不创建第二份输入 |
| lease 已失效或取消已成立 | 阻断完成写入和 Agent 启动，按 Runtime 正常恢复/取消流程处理 |

历史终态 Run 保持原记录可读；旧非终态 Run 缺可信输入时不能自动授权全集或重建。确实需要新输入时显式创建新 Run。恢复必须把数据库、对应世代文件及 transcript 作为同一[恢复点](../operations/runbook.md#一致恢复点包含什么)核对，不能用较晚的 workspace 为较早的 DB 补签。`PREPARING` 接管、提交结果未知和孤立目录均需故障注入与独立运维处置，不能据表推导已有自动恢复器。

特别是“提交结果未知”：当前 store 以事务成功退出作为返回完成回执的条件，但没有独立的结果未知重读/恢复流程。上表的重读是待补齐的协议，不是已有自动重试。恢复开发应以原 Run、原世代与原文件摘要确认一次提交的结果，不能通过第二次 `begin` 自行发放新输入。

### 读取时的完整性边界

回执不是运行期文件锁。`workspace.read/search` 返回的内容与 Evidence 必须来自**已对照回执校验的同一份字节**；“先 hash 一个路径，再重新打开它读取”仍有竞争窗口。使用锚定 Run 根且不跟随中间/末端 symlink 的描述符、有界读取、普通单链接文件检查；只读权限位不能替代这些检查。

search 选择 `input/` 时先验证所选世代的完整树，再按可信清单搜索；未知文件、缺失、hardlink、FIFO 或改写不归为普通 binary skipped。正常的搜索文件数/结果数截断仍须显式返回 `truncated`，零匹配只说明实际搜索范围内未命中，不能证明整个 Run 输入没有相关事实。

`workspace/` 是 Agent 自己的可变工作文件，不要求伪造一份输入回执；仍有独立路径和读取上限。live document/repository Tool 继续验证自己的冻结来源与授权，并记录实际来源版本，不借缓存回执扩大读取范围。

### 跨根总量的修正口径（待实现）

总量累计、Settings、.env.example 与 Worker 注入已有工作副本代码；默认/边界/自定义配置和 startup 注入已有局部测试，整条准备链与跨根故障验收仍待完成。保留本标题供旧引用使用；本地注入测试不证明部署已采用正确配置，实际证据见[计划](../planning/roadmap.md#13-当前执行状态)。

总量按最终可见文件的实际落盘字节累计，不按文本字符数计算；包含转换物、manifest、索引与 history，原文若也保留则另计一次。多个文档槽位共享的物理文件只计一次，不同 repository 根各自落盘的副本分别计量。skipped 的未落盘原文不计文件字节，描述它的 manifest 仍计。

| 限制 | 作用 | 不能替代 |
| --- | --- | --- |
| 单文件限制 | 原文件/转换结果可安全读取；平台必需的 manifest/索引超限则准备失败，不能跳过它们 | 一个资源根或整个 Run 的存量 |
| `max_files / max_bytes` | 各物化根的文件数和字节数，平台生成文件也计入 | 多个根相加后的总量 |
| `max_total_files / max_total_bytes` | 整个输入世代的最终存量 | 准备临时空间、模型用量或一次 search 的扫描上限 |

例如三个根各有 4 MiB 的最终文件，逐根上限为 5 MiB、总量上限为 10 MiB：三个根各自合法，但 12 MiB 的整份输入必须失败，Agent 不能只获得前两个根。这个例子不是项目默认配置。

所有根完成并满足总量后才把输入交给 Agent；不能让前面的根先变成“完整成功”，最后一个根超限后继续执行部分输入。恢复复用已计量副本，不再次消耗一份存量额度；并发准备不能各自拿到完整 Run 上限。临时目录峰值占用仍需磁盘/准备阶段独立限额，最终文件预算不是磁盘耗尽防线，也不是[模型消费预算](run-budgets.md)。

## 已知差距与后续设计

| 差距 | 实施与审查入口 |
| --- | --- |
| 创建重放与真实事务验收 | 已有[冻结解析](../../PJM/backend/src/projectmind/documents/binding.py)、[Run 创建](../../PJM/backend/src/projectmind/runs/service.py)、[意图](../../PJM/backend/src/projectmind/runs/creation_request.py)/[兼容](../../PJM/backend/src/projectmind/runs/creation_replay.py)；仍需真实 DB 验证并发胜者、首次快照与完整事务回滚 |
| 历史续行与版本升级 | 新 API 已区分合法清单、历史缺失与损坏；旧非终态 Run、旧 API/Web/Worker 混合版本和回退安全仍需专项处置，不能把历史可读当成可继续执行 |
| 回执与准备交接 | [物化器](../../PJM/backend/src/projectmind/agent/workspace_materializer.py)调用 begin/complete、返回 PreparedInput，[ContextBuilder](../../PJM/backend/src/projectmind/agent/context_builder.py)消费 workspace/resources，[Worker startup](../../PJM/backend/src/projectmind/worker/settings.py)注入必需 store；现有物化测试调用仍待同步，不能把构造器依赖改为可选来绕过 |
| 仓库缓存授权与运行期字节 | [repository source](../../PJM/backend/src/projectmind/agent/repository_source.py)的 inspect 与 open 分别验证授权/取得内容；[workspace Provider](../../PJM/backend/src/projectmind/agent/workspace_provider.py)已调用[input_workspace](../../PJM/backend/src/projectmind/agent/input_workspace.py)与[安全 I/O](../../PJM/backend/src/projectmind/agent/materialization_storage.py)。局部读写回归之外，继续真实世代、仓库 fake、准备复用和故障场景验收 |
| 准备期监督与提交失败 | [Executor](../../PJM/backend/src/projectmind/worker/executor.py)已有准备期 heartbeat/取消、独立 timeout 与启动校验；继续验证回执提交结果未知、真实锁竞争、旧 Attempt 与首事件前取消，不能以监督 fake 代替真实 store |
| 所有物化根的共同总量限制 | [Settings](../../PJM/backend/src/projectmind/core/settings.py)、.env.example、Worker 与物化器已有字段/累计/注入；保留已有配置回归，继续按[计量口径](#跨根总量的修正口径待实现)验证 manifest、共享文档并集、不同仓库副本及最后一根超限 |

上表为工作副本接续入口，不是验收通过清单。局部 Tool 测试通过与旧物化 fixture 构造失败可以同时成立；后者尚未进入文件范围断言，不能据此判断范围校验正确与否。日期、实际结果和剩余验收统一见[计划](../planning/roadmap.md#13-当前执行状态)，文档整理不修补应用实现或测试来替代联调。

现有公开链路按[选择组件](../../PJM/web/src/components/DocumentSourceField.tsx) → [安全投影](../../PJM/backend/src/projectmind/runs/resource_projection.py) → [Web validator](../../PJM/web/src/api/runResources.ts) → [清单展示](../../PJM/web/src/components/RunDocumentSnapshots.tsx)定位；不再把已有入口重复登记为待开发。上述剩余责任及各轮验证统一见[计划 R01](../planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)。

## 验收条件

| 场景 | 必须观察到的结果 |
| --- | --- |
| 单份/集合/全集与可选不选 | 实际可读成员与用户确认范围一致；槽位并集可解释 |
| 创建后新增、删除、同路径重传 | 不增加输入，不替换原 ID；缺失清楚失败 |
| 跨 Project、伪造 ID/hash、路径逃逸 | 越权/越界在读取前拒绝；实际 hash 不符的内容不得交给 Agent |
| 相同请求重发与并发创建 | 一个幂等身份只得到原 Run/快照；不同输入仍冲突 |
| Segment/Attempt、缓存篡改与旧 Run | 快照不漂移；额外文件/错误来源拒绝；历史不补造 |
| manifest 与派生文件同时被改写、发布中崩溃 | 独立回执不匹配即拒绝；无回执/缺文件不继续，失效 Worker 不能发布完成事实 |
| 校验后替换路径、篡改 search 输入 | 只返回本次验证的同一份字节；完整性失败不伪装成 skipped 或零命中 |
| 准备慢于一个 lease、取消与完成提交结果未知 | 心跳持续；旧 Attempt 不提交 READY；只按原回执确认结果，不另建或补签世代 |
| 仓库分支移动与多 revision | 输入基线与 live Evidence 各自版本可追溯，不混淆为创建时固定内容 |
| 文件转换、skipped 与额度 | 原文可追溯；共享文档只计一次；必需生成文件纳入限制；最后一根超限时没有 Agent 启动 |
| 真实资源与模型 | HTTPS Git/SVN 凭据、断连与错误脱敏；Agent 可按 Brief 找到输入并说明限制 |
