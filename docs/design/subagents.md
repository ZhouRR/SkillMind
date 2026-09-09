# 并行子分析

> 定位：现行实现核对与子分析的修正设计。并行子分析属于一个 Run，不改变其目标、主 Session 或审批链。共享预算由[预算设计](run-budgets.md)负责，实施与验证状态见[计划](../planning/roadmap.md#13-当前执行状态)。

按问题阅读：[一组结果怎么读](#一个例子完成的是哪一层)、[当前实现](#当前返回值的可信边界)、[子任务的指令和输出](#子任务指令与结果的边界)、[保存与兼容](#提交顺序与版本兼容)、[开发接续](#开发接续顺序)。

## 责任与数据流

```text
PRIMARY Session ── subagent.dispatch/v1
                          ├── 子分析 A（只读）
                          ├── 子分析 B（只读）
                          └── 子分析 C（只读）
                                      ↓
                         结果/失败摘要 + 子 Session ID
                                      ↓
                     主 Session 的一条 ToolCall + Evidence
```

这是职责关系，不是另一个 Run 执行器。子 Session 使用 `SUBAGENT / BRANCH`，共享父 RunAttempt。数据库仅限制 PRIMARY 同时唯一活动，不把子分支当作新的主执行。子 Agent 不独立写 RunEvent sequence；Provider 汇总后，Gateway 验证并保存 Tool response / Evidence，才能返回主 Session。

## 一个例子：完成的是哪一层

主任务要评审一次配置变更，派 A 检查配置、B 检查日志。A 返回有效结论，B 在读取后失败；两支的 Session 都保存成功，Gateway 也完成了结果校验与审计。

| 看到的状态 | 只能说明什么 |
| --- | --- |
| Tool `status=success` | 这一组结果已按 Tool 契约返回，不是每支都成功 |
| A 为 COMPLETED，B 为 FAILED | A 有通过验证的结论，B 没有；主任务必须保留日志未检查完的限制 |
| 子 Session 已关闭 | 生命周期记录已落库，不证明分析全面、进程停止或用量结清 |
| 主 Run 仍在执行 | 主 Agent 尚需综合证据并提交自己的最终 Result |

若同样的 A/B 结论已经生成，但 Session 保存无法确认，现行 v1 只能返回 Tool 错误；不能省略必需 ID 后宣称成功。结果不可见也不等于没有消费，不能自动再跑一组来补齐。可保留结论的审计降级属于后续版本设计，见[提交顺序与版本兼容](#提交顺序与版本兼容)。

## 能力与故障边界

- 子能力不得超出主能力集合，且排除 write、effect/propose、interaction 与再次 dispatch。
- 唯一决定点为 [resolve_subagent_capabilities](../../PJM/backend/src/projectmind/agent/subagent.py)；显式请求禁止能力时拒绝整个请求，不能静默裁掉后继续。
- 单次最多 4 个分支；每个分支有独立超时。取消应清理整组，但 coroutine 结束不证明 SDK 进程已经停止，按 [Runtime 取消边界](agent-runtime.md#75-取消超时与失去执行权)单独验收。
- 单路执行失败应保留其余结论；整组 Session 保存或 Gateway 审计失败是另一层错误。隔离分支异常不等于所有情况下都能返回部分结果。

[Provider](../../PJM/backend/src/projectmind/agent/subagent_provider.py) · [Session 记录](../../PJM/backend/src/projectmind/agent/subagent_sessions.py) · [Tool 契约](../../PJM/contracts/tools/subagent.dispatch/v1/request.schema.json)

## 当前返回值的可信边界

以下为 2026-09-09 对已有工作副本的只读核对，不是本轮文档整理实现的功能。终端/结果校验、必需 Session 记录、主 validator 测试接线与整组清理已有改动及局部回归；旧的主 Executor fixture 失败本次未再复现。结果与未覆盖范围见[计划](../planning/roadmap.md#13-当前执行状态)，不据此记为全链路完成。

| 观察什么 | 当前实际结果 |
| --- | --- |
| 成功终端 | 收集器要求唯一有效终端及正常收尾，再调用共享 ResultValidator；中途文字不是成功摘要 |
| 失败或缺少终端 | 失败/禁止等待/空流/终端冲突按 FAILED 返回；实际 Gateway 的局部回归已覆盖 |
| 分支超时或整组取消 | 自身 deadline 对应 TIMED_OUT；TaskGroup 持有整组 task，取消后等待各支清理，不保证还能返回结果 |
| Session 保存 | recorder/validator 为必需依赖；保存异常或 ID 数量/格式不符按 unavailable 拒绝，不再省略 ID |
| 子指令与输出 | prompt 已换为分支目标，但仍继承父任务 Schema、Brief 和 checksum；职责对齐尚未完成 |
| 摘要长度 | 使用已验证结果的 summary，最多 20,000 字符；不再使用 Run 列表的 280 字符预览。Gateway 的 UTF-8 响应字节限制仍独立生效 |
| budget / cost | 每支分配值 / 空费用，不是已用或余额；Schema 的说明已改为分配，仍无共享账本 |

[SubagentTranscript](../../PJM/backend/src/projectmind/agent/subagent_result.py)检查 Run/Attempt、SDK Session identity 与本支 sequence，拒绝 ENGINE_FAILED、SESSION_INTERRUPTED、非法等待/提案和成功终端后的事件。通过终端检查后才用 [ResultValidator.validate_context](../../PJM/backend/src/projectmind/agent/result_validation.py)验证结果；身份/顺序、无效结构与引用的负向用例已进入[实际 Gateway 回归](../../PJM/backend/tests/agent/test_subagent_lifecycle.py)，不是只有 Provider 单测。

主/子共用 [stream 清理入口](../../PJM/backend/src/projectmind/agent/stream_lifecycle.py)，Engine 的 execute/resume/fork 也向内层传递关闭。整组清理和主执行首事件前取消已有 fake 回归；它们不证明真实 SDK 进程已停。主/子的清理与终态保存顺序并不相同，按[执行监督设计](run-supervision.md#收尾终态与晚到信息)分别验证，不能把共用 close 函数理解成完全相同的生命周期。

## 完成、失败与审计如何表示

成功的判断点是通过验证的成功终端，不是“没有 Python 异常”或“生成了文字”。以下为设计要求；工作副本覆盖到哪一步只按上节和计划举证，不在此新增公开字段。

| 观察到的事实 | 目标处理 |
| --- | --- |
| 唯一有效成功终端，结果满足该执行的输出要求 | 才能把 branch 标为 COMPLETED；业务结论不等于资源全部检查完毕 |
| engine 失败、非法 defer、成功终端缺失或终端冲突 | 归为失败，保留安全原因与已有真实 Session identity，不把中途文本当成成功摘要 |
| 本支 deadline 或整组取消 | 分别表达超时/取消并清理执行；SDK 的 interrupted 事件还需结合原因，不能自行认定为用户取消 |
| 结论已得到，但 Session 记录不完整 | 目标允许保留已验证结论并明确审计缺口；不得伪造可查询 ID，不能连带忽略预算保存失败 |

执行 outcome、Session 审计完整性、用量是否已结清是三个判断。Session 保存成功不证明内容全面或预算结清；无 Session ID 也不抹去已发生的调用。预算执行身份必须在收费前取得，不能等事后 Session 行产生 ID 才开始记账。

### 子任务指令与结果的边界

当前 [_derive_child_context](../../PJM/backend/src/projectmind/agent/subagent_provider.py)只替换 prompt、权限、tools 和局部分配，没有生成新的任务指令依据或输出契约。SDK 的 [output_format](../../PJM/backend/src/projectmind/agent/claude.py)和平台 validator 都读取继承的 result_schema；保留的 task_brief/checksum 却仍对应父 prompt。

由此推导出的风险是：局部分析被要求产出父任务的完整结果，或缺少父 Skill 的必需规则；父 Brief 的 hash 也不能证明子模型实际收到的指令。Session recorder 还复用了父 engine_options_checksum，不能据此证明缩额/缩权后的实际子 options。简单的空 Schema 测试不能证明这种组合正确。本轮只修正文档中的责任设计，不修改既有 prompt、Schema 或历史快照。

后续采用“共享安全校验，分别定义任务产出”的边界：

| 责任 | 设计要求 |
| --- | --- |
| 子任务指令 | 从冻结父来源派生、保留必需规则与出处，只收窄目标和能力；持久记录原 dispatch、branch 和实际送入模型的指令依据/校验值 |
| 子任务结果 | 使用平台定义、版本化的只读分析结果，表达结论、证据与未覆盖范围；不要求子支生成父任务的全部交付或外部效果 |
| 共同校验 | 复用敏感信息、引用所有权和结构校验；不复制一套更宽松的 validator，不接受模型自选 Schema 来绕开限制 |
| 主任务结果 | 汇总各支并保留失败/未知范围，仍满足原冻结的 OutcomeEnvelope 与可选 task-specific Schema |

子指令不是新的 Run 或 Segment，也不覆盖父 Brief。分支 prompt、SDK 输出要求、平台 validator、审计记录和汇总 consumer 必须引用同一个已确定的子契约版本。新 Schema 的精确字段及持久载体在实施时同步，不能先把说明性字段塞进现行 v1 response。语义变化同样需要兼容审查，不能以 response 外形未变为由直接切换旧 Worker。

### 提交顺序与版本兼容

```text
每支：终端检查 → 关闭 stream → 结果校验
  ↓ 全组收束，形成结论或失败摘要
事务 A：保存整组子 Session
  ↓
Provider：response + Evidence 草稿
  ↓
Gateway：分配引用、校验敏感信息 / Schema / 大小
  ↓
事务 B：保存 Tool response + Evidence
  ↓
返回主 Session
```

这是分阶段提交，不是一个跨 SDK、Session 和 Tool 审计的大事务。某支在检查点失败时仍需清理，再转为失败摘要；不在 DB 锁内等待模型。A 已提交而 B 失败时可能已有子行却没有可重放的成功 Tool 结果，不能自动删除这些子行或重跑收费执行。

现行 v1 每支都要求保存后的 agent_session_id；SDK Session UUID 与平台行 ID 不能互相替代，UUID 的格式检查也不能证明数据库中真实存在。[Tool response 与消费者](../../PJM/contracts/README.md#子分析と用量の契約を読む)必须验证整条保存/读取链。v1 不支持审计缺失的成功返回，保持拒绝边界。

目标中的审计降级需采用能显式表达缺口的版本契约，并同步 registry、Provider、Gateway、Evidence、Web 和历史读取。旧消费者只理解 v1 时不得向其发送降级结构。成功 Tool 审计仍不可省略；若其提交结果未知，应确认原调用而不是换键重跑。Session 的重放身份、在途取消记录和跨事务恢复也需补齐，不能只删 required。

## 预算现状与修正设计

预算由主/子执行共同拥有，不应仅在子 Agent 规范内定义。计量口径、持久账户、原子预留、结算和恢复统一见[Run 预算与执行限额](run-budgets.md)；本节保留旧链接入口。

当前 `split_budget` 只把每次传入的父快照上限整除分配。dispatch 返回的 turns/output 是分配值，不是实际消耗；美元上限也没有按分支拆分。连续 dispatch、主子混合执行和跨 Attempt/Segment 均不能由此得到累计保证。

工作副本已有[账本组件](run-budgets.md#持久账本的当前载体)，但 Provider 还未调用，不把“有整组预留方法”写成“dispatch 已先预留”。接续复用内部 store；可信计量、核对授权和局部上界须与主执行一起接入，不能只替换子分配公式。

子分析接入共享预算时，全组先取得原子预留再逐支启动，并在完成、超时、取消后结算或保留未知占用。主模型等待 Tool 返回不表示其未用额度已释放；[主子预留规则](run-budgets.md#原子性与重复请求)禁止复制主执行仍可花费的额度。预算持久化失败必须关闭新收费入口，即使未来版本允许 Session 审计降级，也不能据此把预算变为 best-effort。计量来源、晚到报告和升级门禁只在预算规范维护。

## 开发接续顺序

先按[Backend 接线](../../PJM/backend/README.md#予算と子分析の接続を追う)检查已有实现，不重新开发终端收集器，也不把必需依赖退回 optional。

1. 保持已接通真实 validator 的主 Executor 成功、无效结果和 Review/fork 回归，以及[实际 Gateway 回归](../../PJM/backend/tests/agent/test_subagent_lifecycle.py)的终端、身份/顺序、摘要和整组清理，不再重做旧 fixture 修正。
2. 补齐[停止原因与核对](run-supervision.md#兼容与开发接续)、Session/Tool 未知提交和重放证据；真实 DB 与 SDK 进程另行验证。复杂父输出要求仍是子指令/Schema 的设计缺口。
3. 核对已有预算 DTO/repository/store，再补齐真实事务、计量来源与创建/主子执行的接入，验收原子预留、启动与结算；可同时评审子指令/输出、审计降级与重放的版本设计，但不能先向 Worker 开放新协议。
4. 预算门禁满足后，按[契约工作流](../development/contract-workflow.md)接入新版本及消费者/历史读取，再验连续 dispatch、主子同时占用、Attempt/Segment 续行和未知用量。不覆盖已保存结果，不因修正 v1 异常路径而扩大能力或额度。

## 验收

| 范围 | 必须证明 |
| --- | --- |
| 权限 | 子能力不超出父集合，拒绝写入/交互/递归 |
| 预算 | 单次分配与全 Run 累计分别验证；内部账本尚未接入执行，不能据其局部测试认定本项完成 |
| 失败 | 部分失败不会伪造全面完成；取消清理子执行 |
| 终端事件 | ENGINE_FAILED / SESSION_INTERRUPTED、空 stream、冲突终端不误报完成；不仅测试 Python 异常 |
| 子指令/输出 | 父任务复杂 Schema 不迫使子支伪造完整交付；必需规则保留，实际 prompt 与记录的指令依据相符 |
| 审计 | v1 的可查询 ID 真实存在，保存失败不伪造成功；目标降级经版本化 Gateway/消费者验证，重放不重跑收费分支 |
| 联动 | 成功结论、审计缺口和未知用量分别显示；失败或超大响应不能使已发生消费归零 |
| 模型效果 | 适时拆分独立工作，引用证据汇总，并写出未覆盖范围 |
