# Agent Runtime 与持续执行

本页负责已有 Run 的执行、会话与事件协议。创建身份见[Run 创建](run-creation.md)，输入一致性见[资源快照](resource-snapshots.md)，状态与剩余工作见[计划](../planning/roadmap.md#r07-run-与审计)。

## 设计结论

Run 是持续的业务线程，不等于一次 SDK query、Session 或进程。默认引擎为 Claude Agent SDK，业务层只依赖 AgentEngine。Agent 可以自主规划只读分析，但资源、权限、交互、外部效果与终态由平台控制；PostgreSQL 是审计正本。

## 责任边界

| 责任方 | 负责什么 |
| --- | --- |
| ProjectMind | 从发布版本的 Blueprint 生成任务/Brief，冻结资源与权限；管理 lease、恢复、预算、交互、效果、证据、结果与事件 |
| AgentEngine | 在 Brief 内规划、选择允许的 Tool、维持会话，产生公开文本、用量、交互和结果候选 |
| Agent 无权决定 | 扩大 Project/scope、取得 Secret、增加 Tool、自批提案、绕过限额/校验或直接调用未注册 Provider |

Skill、Ticket、代码或模型建议都是输入，不是授权。业务规则不放进 route、queue job 或 SDK adapter。

## AgentEngine 抽象

[agent/domain.py](../../PJM/backend/src/projectmind/agent/domain.py)定义 execute / resume / fork / interrupt / health；前三者直接返回异步事件迭代器。Adapter 把 SDK message 映射为平台 AgentEvent，不向业务服务泄漏 SDK 类型。

一个 Run 同时只有一个活动 PRIMARY Session。子 Session 使用 SUBAGENT / BRANCH，共用父 RunAttempt；数据库唯一约束只针对 PRIMARY。子活动汇总为主 Session 的 ToolCall/Evidence，不另写 RunEvent sequence。权限、输出与预算规则见[子分析](subagents.md)。

## AgentTaskBrief

每个 Segment 启动前冻结一份 [AgentTaskBrief](../../PJM/contracts/agent-task-brief/v1.schema.json)：

| 内容 | 必须说明什么 |
| --- | --- |
| 身份与目标 | Run、Segment、Task、精确 SkillVersion/checksum，目标和成功条件 |
| 指导与资源 | Skill 必需规则、建议步骤、质量要求与禁止事项；合法 binding、来源与路径 |
| 策略 | capability/参数/scope、限额、交互和效果批准边界 |
| 输出与续行 | 交付要求、可选 Schema、checkpoint、已确认事实及 Evidence/Proposal 引用 |

完整指导不能退化为短 prompt。Checkpoint 可压缩上下文，但保留原 transcript/source trace，不改用户事实或批准范围。Brief 不含明文凭据、内部配置或其他 Project 数据。

## 自主执行等级

ExecutionProfile 是策略合成模型，不表示全部选项已有独立 Web 控件。

| Profile | 范围 |
| --- | --- |
| GUIDED | 优先显式步骤，关键歧义询问 |
| SUPERVISED | 默认；自主选择读取、验证和报告方式，业务取舍或外部效果前询问 |
| DELEGATED | 在明确预授权的资源/低风险效果内扩大自主循环，仍受硬拒绝限制 |

Skill 可推荐等级；system ADMIN 设置项目上限，发起人只能选更保守的等级。不存在独立 Project ADMIN 角色。

Agent 可调整分析顺序、交叉验证、组织证据并在 workspace/output 生成产物。缺少关键事实、业务选择、有效批准，或规则相互冲突时应暂停。

原 scope 内补充对象 ID/偏好可以续行；新增 Integration、改 Project、扩文档集合、权限或预算须新建 Run。必需资源缺失在创建时拒绝，可选资源未选也不能在对话中隐式启用。

## Tool 与工作区边界

### Tool 分类

| 等级 | 边界 |
| --- | --- |
| observe | 参数校验后读取已绑定资源 |
| workspace | 仅在隔离 workspace/output 写临时文件、报告或补丁 |
| propose | 保存外部变更候选，不提交远端 |
| apply | 独立 Effect Worker 执行；默认人工批准，仅允许明确低风险预授权 |

当前注册 issue/repository/document 读取、workspace read/search/write、interaction.request、change.propose、subagent.dispatch；Effect Provider 为 issue.update/repository.write。所有能力使用版本 ID 与平台注册 handler，凭据仅由 Provider 经 SecretReference 取得。[Tool 契约](../../PJM/contracts/tools/)不等于任意同名能力已开放。

### 文件、Shell 和 Web

- SDK 内置 Read/Glob/Grep/Bash/Write/Edit/Web 仍 hard deny；平台 capability 提供受控等价功能。
- input/ 是冻结只读证据；workspace.write 仅写 workspace/、output/，不覆盖 Project 文档库或外部目标，不存在自由 document.write。
- cwd 不是隔离边界；依赖 mount、路径检查、sandbox、Gateway 与 scope，拒绝 path/symlink 逃逸。
- 任意 Shell、sandbox command、通用网络尚未开放。后续须有独立威胁模型、允许命令/镜像/资源上限及注册 fetch/search Provider；不暴露 Docker socket、Secret 或无限制网络。

### PreToolUse 判定

检查由 hook、Gateway、binding/Provider 和 Effect Worker 分担，不是一个 hook 已覆盖全部：

1. 注册 capability，且满足 Skill requirement、Project policy 与 Run 权限快照。
2. binding、Integration、路径、对象/revision 和网络范围匹配。
3. 参数通过 Tool contract，拒绝 credential-like 字段。
4. 执行路径的局部限额满足；Run 累计强制仍待[共享预算](run-budgets.md)接入。
5. apply 有有效批准或精确预授权。

请求侧检查在 Provider 前执行；响应 Schema、敏感信息与大小检查在返回后执行，失败内容不交给 Agent。错误仅存脱敏审计，禁止全局 bypassPermissions。

Tool 审计还须核对原 Worker 执行权：首次许可只消费一次，未决/失败不自动重跑，成功只读重放；私有 scope、锁内检查与提交未知规则见[调用提交与重放](run-supervision.md#tool-调用的提交与重放)。

### 资源快照的物化

repository 按冻结授权物化至 input/&lt;requirement_key&gt;/，具体 revision 在打开资源时解析；document 使用创建时冻结的 ID/hash/成员，映射至 input/documents/。分支名不等于固定 commit，公开清单不是 Provider 的授权输入。

全部资源根通过[输入准备回执](resource-snapshots.md#输入准备与可信缓存)后，Brief 才取得可用逻辑路径；后续 Attempt 只复用同一可信世代，不静默重建。Tool 读取仍校验同一份实际字节。

## 持续 Run 与多会话

### 生命周期

```text
Run + Segment 1 + dispatch Outbox
  → Worker 准备输入 / 冻结 Brief → Session 执行
  → 持久化 checkpoint 与等待 → 普通答复或效果处理
  → Segment 2 + dispatch Outbox → 准备新的 Brief
  → resume / fork / replace → Result 与终态 RUN_SNAPSHOT
```

等待不是终态；精确状态及 Attempt 的 DEFERRED 见[状态速查](domain-model.md#run-状态速查)。答复事务先保存 Segment/Outbox，Brief 由后续 Worker 冻结，不在回答时提前宣称准备完成。

### Segment、Attempt 与 Session 的区别

Segment 表示新的业务阶段；Attempt 表示同段技术重试；Session 表示模型对话上下文。故障不制造新决策，用户答复不伪装技术重试。三者均不改变 Run 的 Skill、资源与权限上限。

### 会话延续策略

| 模式 | 条件 |
| --- | --- |
| resume | 引擎/模型兼容、目标未分叉，transcript/workspace 完整 |
| fork | 明确比较方案或保留分析分支 |
| replace | 上下文损坏、不兼容或需重压缩；新 Session 接收可审计 checkpoint |

记录 continuation mode、parent、模型与 checkpoint checksum；transcript 用于恢复，平台事件/ToolCall/Evidence/Interaction 才是审计事实。不完整时不无声从头执行。

### 从领取到模型启动的边界

当前 [Executor](../../PJM/backend/src/projectmind/worker/executor.py)按以下顺序执行：

```text
有效 claim → heartbeat / 取消监督
  → Run RUNNING → ContextBuilder（独立准备 timeout）
  → 验证 context 身份/sequence
  → 短事务冻结 Brief（重验 lease/取消）
  → 短事务启动校验（再验状态/lease/取消）
  → 事务外 execute / resume / fork
```

context 匹配 claim 的 Run/Attempt/Project/actor，sequence 由 repository 分配。锁顺序 Run → Segment → Attempt；获锁后使用当前时间和数据库 lease，不使用 claim 或等锁前的旧到期判断。网络、转换和模型调用不在锁内等待。

RUNNING 只说明执行状态推进，READY 只说明输入回执已提交，Brief 冻结只说明该段指导已固定，Session 活动才说明记录了模型活动；任何一项都不代表 Run 已成功。

### 取消、超时与失去执行权

取消意图、业务终态、进程退出与用量结清分别处理。已有准备期/首事件前取消、锁内终态复查及等待/普通事件取消检查；等待拒绝和取消终态仍是两个事务，后者重新 fencing。详细阶段、故障与验收见[执行监督](run-supervision.md)。

## 用户交互协议

普通答复只在冻结范围内补充事实，成功答复追加 Segment。REVIEW 不等于 Evaluation，推荐不是默认回答，required=false 不自动跳过；410 也可能伴随已提交的过期续行。

普通 interaction.request 只接受 CLARIFICATION/CHOICE/REVIEW；无 Proposal 的历史批准等待保持只读，不经普通答复或过期恢复解锁。原作者重放、期限竞争与页面原请求确认统一见[普通交互](user-interactions.md)，不与外部批准混用。

## 外部效果协议

observe → Evidence → propose → 精确批准/允许的预授权 → 独立 Effect Worker apply → read-back。Agent 不直接写外部系统，也不批准自己的提案。

提案必须冻结目标、patch/操作、Evidence、前置 revision、风险/可逆性、幂等身份和来源 Session。批准者限发起人或 system ADMIN；审批时复验 actor、Project、version/checksum、Integration 与 scope。repository.write 永远人工批准且不 force。

陈旧目标不覆盖，原身份重试，不重复副作用；写入后验证失败需明确核对，不静默成功。Provider 已存在，但精确 CAS、远端身份、阶段回执及独立 Effect 监督仍有缺口，唯一正本见[受控写入](repository-effects.md)。

## 结果与输出验证

候选先经平台 validator，再交终态事务锁后检查 lease/取消；失败和取消可以没有 Result。结构通过不证明业务正确、附件可读或模型效果摘要已核验。Outcome、引用与人工修订统一见[结果与评价](results-evaluation.md)；子任务有独立的[输出派生责任](subagents.md#子任务指令与结果的边界)。

## 事件、状态与可靠性

- [RunEvent 契约](../../PJM/contracts/events/run-event/v1.schema.json)覆盖 Segment、Session、Interaction、checkpoint 与 Effect；Run 内 sequence 严格递增。
- 状态变更与 Outbox 同事务。Queue 重投由数据库领取抑制，Tool 重放与 Effect 幂等各用自身协议，不承诺模型/远端全局精确一次。
- 只有有效 lease 可提交；失效由 Recovery 接管，同段重试受 PROJECTMIND_RUN_MAX_ATTEMPTS 限制。
- 终态 RUN_SNAPSHOT 是最后持久事件，SSE 据此结束。终态不可恢复，新目标创建新 Run。
- 模型 wall timeout 不含准备、排队、人工等待或全部 Attempt 累计；各计时器见[预算说明](run-budgets.md#现有计时器的覆盖范围)。

停写须覆盖所有 job/cron/实例，dispatch 开关不等于全局停止；见[发布边界](../operations/deployment.md)。

## Claude Agent SDK 实现要求

锁定 SDK/CLI 版本并记录模型/options checksum，SessionStore 镜像 transcript。Hook 执行平台策略，SDK allowed tools 与 structured output 不替代参数和结果校验。人工等待须收束收费进程，不长期占 Worker。

升级或新增引擎先验消息映射、resume/fork/interrupt、旧 Session 恢复、Tool 拒绝/MCP、等待与错误分类；使用同一 conformance suite。

## 安全测试矩阵

验收覆盖：指导完整且不可扩权；缺资源拒绝创建；答复新增 Segment、故障只新增 Attempt；顺序 Session 不漂移；未批准/stale/scope 外效果拒绝；路径逃逸拒绝；同 Run 引用与 Secret 边界；终态不可续行。

监督专项还需慢准备、锁等待过期、Brief/首事件前取消、提交不明与真实进程清理；分别观察 Run/Session/Result/Outbox，而非只测异常映射。

## 当前实现边界

已有 Brief、持续 Run、顺序主会话、只读子分析、workspace 与受控效果入口，以及输入回执和准备监督接线。剩余是完整资源/历史续行、真实事务和停止证明；共享预算只有内部账本，未接创建及主子执行。精确接续范围见[计划](../planning/roadmap.md)，不重复维护测试数字。
