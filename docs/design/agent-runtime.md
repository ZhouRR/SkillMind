# Agent Runtime 与持续执行

本页负责已有 Run 的执行、会话与事件协议。创建身份见[Run 创建](run-creation.md)，输入一致性见[资源快照](resource-snapshots.md)，状态与剩余工作见[计划](../planning/roadmap.md#r07-run-与审计)。

## 设计结论

Run 是持续业务线程，不是一次 SDK query、Session 或进程。默认引擎为 Claude Agent SDK，业务层只依赖 AgentEngine；平台控制资源、权限、交互、效果与终态，PostgreSQL 保存审计正本。

## 责任边界

| 责任方 | 负责什么 |
| --- | --- |
| Skillmind | 从发布版本的 Blueprint 生成任务/Brief，冻结资源与权限；管理 lease、恢复、预算、交互、效果、证据、结果与事件 |
| AgentEngine | 在 Brief 内规划、选择允许的 Tool、维持会话，产生公开文本、用量、交互和结果候选 |
| Agent 无权决定 | 扩大 Project/scope、取得 Secret、增加 Tool、自批提案、绕过限额/校验或直接调用未注册 Provider |

Skill、Ticket、代码或模型建议都是输入，不是授权。业务规则不放进 route、queue job 或 SDK adapter。

## AgentEngine 抽象

[AgentEngine](../../SKM/backend/src/skillmind/agent/domain.py)提供 execute / resume / fork / interrupt / health；前三者返回异步事件迭代器，Adapter 将 SDK message 映射为 AgentEvent，不泄漏 SDK 类型。

每 Run 只允许一个活动 PRIMARY；SUBAGENT/BRANCH 共用父 RunAttempt，汇总到主 Session 的 ToolCall/Evidence，不另分 RunEvent sequence。详见[子分析](subagents.md)。

## AgentTaskBrief

每个 Segment 启动前冻结一份 [AgentTaskBrief](../../SKM/contracts/agent-task-brief/v1.schema.json)：

| 内容 | 必须说明什么 |
| --- | --- |
| 身份与目标 | Run、Segment、Task、精确 SkillVersion/checksum，目标和成功条件 |
| 指导与资源 | Skill 必需规则、建议步骤、质量要求与禁止事项；合法 binding、来源与路径 |
| 策略 | capability/参数/scope、限额、交互和效果批准边界 |
| 输出与续行 | 交付要求、可选 Schema、checkpoint、已确认事实及 Evidence/Proposal 引用 |

Brief 保留必需指导，不含凭据、内部配置或跨 Project 数据。Checkpoint 可压缩上下文，但须保留原 transcript/source trace，不改用户事实或批准范围。

## 自主执行等级

ExecutionProfile 合成策略，不表示各选项已有 Web 控件。

| Profile | 范围 |
| --- | --- |
| GUIDED | 优先显式步骤，关键歧义询问 |
| SUPERVISED | 默认；自主选择读取、验证和报告方式，业务取舍或外部效果前询问 |
| DELEGATED | 在明确预授权的资源/低风险效果内扩大自主循环，仍受硬拒绝限制 |

Skill 可推荐等级，system ADMIN 设置项目上限，发起人只能收窄；不存在 Project ADMIN。Agent 可调整分析顺序、交叉验证和生成产物；关键事实、业务选择或批准缺失、规则冲突时暂停。

原 scope 内补对象 ID/偏好可续行；改 Project、增加 Integration/文档/权限/预算须新建 Run。必需资源缺失拒绝创建，可选未选不在对话中隐式启用。

## Tool 与工作区边界

### Tool 分类

| 等级 | 边界 |
| --- | --- |
| observe | 参数校验后读取已绑定资源 |
| workspace | 仅在隔离 workspace/output 写临时文件、报告或补丁 |
| propose | 保存外部变更候选，不提交远端 |
| apply | 独立 Effect Worker 执行；默认人工批准，仅允许明确低风险预授权 |

能力必须匹配版本 ID 与注册 handler，凭据仅由 Provider 经 SecretReference 取得。读取、workspace、交互、提案、子分析与 Effect 的可用范围以 registry 和 [Tool 契约](../../SKM/contracts/tools/)共同判定，不按同名放行。

### 文件、Shell 和 Web

- SDK 内置 Read/Glob/Grep/Bash/Write/Edit/Web hard deny，受控能力由平台提供。
- input/ 冻结只读；workspace.write 只写 workspace/、output/，不覆盖文档库或外部目标。
- cwd 不是隔离；mount、路径、sandbox、Gateway/scope 共同拒绝 path/symlink 逃逸。
- Shell、sandbox command、通用网络未开放；须先定义威胁模型、允许命令/镜像/限额及注册网络 Provider，禁止暴露 Docker socket、Secret 和无限制网络。

### PreToolUse 判定

hook、Gateway、binding/Provider 和 Effect Worker 分担检查：注册 capability 与 Skill/Project/Run 权限、binding/scope/revision、参数与敏感字段、局部限额、apply 的批准。Run 累计限额仍待[共享预算](run-budgets.md)。

Provider 前校验请求，返回后校验 Schema/敏感信息/大小；失败内容不交 Agent，只留脱敏审计，禁止 bypassPermissions。另按[调用提交与重放](run-supervision.md#tool-调用的提交与重放)验证原 Worker 权限：首次许可一次消费，未决/失败不重跑，成功只读重放。

### 资源快照的物化

repository 按冻结授权物化至 input/&lt;requirement_key&gt;/，打开时解析 revision；document 按创建清单映射至 input/documents/。全部根取得[可信准备回执](resource-snapshots.md#输入准备与可信缓存)后才交 Brief；后续只复用原世代，Tool 校验实际读取字节。分支名、公开清单都不能替代固定内容或授权。

## 持续 Run 与多会话

### 生命周期

```text
Run + Segment 1 + dispatch Outbox
  → Worker 准备输入 / 冻结 Brief → Session 执行
  → 持久化 checkpoint 与等待 → 普通答复或效果处理
  → Segment 2 + dispatch Outbox → 准备新的 Brief
  → resume / fork / replace → Result 与终态 RUN_SNAPSHOT
```

等待非终态，状态及 DEFERRED 见[速查](domain-model.md#run-状态速查)。答复先存 Segment/Outbox，后续 Worker 才准备并冻结 Brief。

### Segment、Attempt 与 Session 的区别

Segment 是新业务阶段，Attempt 是同段技术重试，Session 是模型上下文。故障不制造新决策，答复不算技术重试；均不得改变 Run 的 Skill、资源与权限上限。

### 会话延续策略

| 模式 | 条件 |
| --- | --- |
| resume | 引擎/模型兼容、目标未分叉，transcript/workspace 完整 |
| fork | 明确比较方案或保留分析分支 |
| replace | 上下文损坏、不兼容或需重压缩；新 Session 接收可审计 checkpoint |

保存 mode、parent、模型、checkpoint checksum；transcript 用于恢复，平台事件/ToolCall/Evidence/Interaction 用于审计。资料不完整时不得静默重跑。

### 从领取到模型启动的边界

当前 [Executor](../../SKM/backend/src/skillmind/worker/executor.py)按以下顺序执行：

```text
有效 claim → heartbeat / 取消监督
  → Run RUNNING → ContextBuilder（独立准备 timeout）
  → 验证 context 身份/sequence
  → 短事务冻结 Brief（重验 lease/取消）
  → 短事务启动校验（再验状态/lease/取消）
  → 事务外 execute / resume / fork
```

context 必须匹配 claim 的 Run/Attempt/Project/actor，sequence 由 repository 分配。按 Run → Segment → Attempt 取锁后，用当前时间和 DB lease 判定；网络、转换、模型调用在锁外。

RUNNING、输入 READY、Brief 冻结与 Session 活动分别证明状态推进、输入提交、指导固定和模型活动，均不证明成功。

### 取消、超时与失去执行权

取消意图、业务终态、进程退出与用量结清分别处理。等待拒绝和取消终态是两个事务，后者重新 fencing；阶段与恢复规则见[执行监督](run-supervision.md)。

## 用户交互协议

interaction.request 只接受 CLARIFICATION/CHOICE/REVIEW，答复在冻结范围内追加 Segment，不代替 Evaluation 或效果批准。期限、推荐、原作者确认及历史悬空批准统一按[普通交互](user-interactions.md)处理。

## 外部效果协议

observe → Evidence → propose → 精确批准/允许的预授权 → 独立 Effect Worker apply → read-back。Agent 不自批或直接写；repository.write 始终人工批准、不 force。冻结、审批身份、陈旧目标和部分成功恢复统一见[受控写入](repository-effects.md)，其可靠性缺口不由 Runtime lease 覆盖。

## 结果与输出验证

候选经 validator 后，终态事务再验 lease/取消；失败、取消可无 Result。结构、引用、业务正确性分别判断，见[结果与评价](results-evaluation.md)及[子输出责任](subagents.md#子任务指令与结果的边界)。

## 事件、状态与可靠性

- [RunEvent 契约](../../SKM/contracts/events/run-event/v1.schema.json)覆盖 Segment、Session、Interaction、checkpoint 与 Effect；Run 内 sequence 严格递增。
- 状态变更与 Outbox 同事务。Queue 重投由数据库领取抑制，Tool 重放与 Effect 幂等各用自身协议，不承诺模型/远端全局精确一次。
- 只有有效 lease 可提交；失效由 Recovery 接管，同段重试受 SKILLMIND_RUN_MAX_ATTEMPTS 限制。
- 终态 RUN_SNAPSHOT 是最后持久事件，SSE 据此结束。终态不可恢复，新目标创建新 Run。
- 模型 wall timeout 不含准备、排队、人工等待或全部 Attempt 累计；各计时器见[预算说明](run-budgets.md#现有计时器的覆盖范围)。

停写须覆盖所有 job/cron/实例，dispatch 开关不等于全局停止；见[发布边界](../operations/deployment.md)。

## Claude Agent SDK 实现要求

锁定 SDK/CLI 版本并记录模型/options checksum，SessionStore 镜像 transcript。Hook 执行平台策略，SDK allowed tools 与 structured output 不替代参数和结果校验。人工等待须收束收费进程，不长期占 Worker。

升级或新增引擎先验消息映射、resume/fork/interrupt、旧 Session 恢复、Tool 拒绝/MCP、等待与错误分类；使用同一 conformance suite。

## 安全测试矩阵

覆盖指导/权限不漂移、缺资源拒绝、Segment/Attempt 分路、Session 续行、Tool/效果/路径与同 Run 引用边界、终态不可续行。监督按阶段观察 Run/Session/Result/Outbox；专项条件见各规则正本。

## 当前实现边界

执行入口已有接线，但完整资源/历史续行、持久停止证明与共享预算尚未闭合；逐项状态和验收登记在[计划](../planning/roadmap.md)。
