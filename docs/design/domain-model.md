# 领域模型与权限

本页是实体与边界地图，不重复各专题协议。术语见[词汇表](../overview/glossary.md)，实施状态见[计划](../planning/roadmap.md#当前执行状态)。平台不把特定业务字段固化为通用领域对象。

## 建模原则

- Skill 描述目标、资源、规则与产物，不授予权限；平台固定跨 Skill 控制协议，不预定义所有业务 Schema。
- Organization 保存可复用 Skill 资产，Project 显式启用精确版本并绑定资源。
- Run 固定目标与权限；业务续行追加 Segment，技术恢复追加 Attempt，模型 Session 只是执行载体。
- 已发布内容、Run snapshot、Result 不变；答复、评价、外部批准和审计各自追加。
- 外部写入必须 propose → 批准/适用预授权 → 执行 → 回读，不把 Agent 建议变成权限。

## 组织、项目与资源

| 对象 | 责任与实际边界 |
| --- | --- |
| Organization | 组织隔离；当前仅单组织部署，不等于多租户登录已实现 |
| User / AuthSession | ADMIN/USER、ACTIVE/DISABLED；浏览器会话保存 hash、期限、撤销、凭据版及登录角色，不是 AgentSession |
| UserSecurityEvent | 账户安全操作追加记录，不是登录尝试日志或 RunEvent |
| Project / ProjectMember | 项目资源边界与 ACTIVE/REMOVED 成员关系；不增加 Project ADMIN 角色 |
| Integration | 项目中的版本化 Provider 资源与 scope；内置文档不要求 Integration 行 |
| SecretReference | 定位凭据；MANAGED 材料独立加密保存，原值不进入 Run |
| ProjectDocument | ProjectKnowledge 的实际载体：元数据与 blob；自动同步/独立知识索引不是现有服务 |

[项目生命周期](project-lifecycle.md)负责成员、偏好、归档与删除；归档不停止 Run，移除成员不撤销账户全部会话。ADMIN 的本组织访问不依赖 ProjectMember；[用户管理](user-lifecycle.md)和[会话 v2](authentication.md#会话凭据-v2-与切换要求)分别负责账户变更与凭据。

SkillSource、Interpretation、SkillVersion、Manifest 归 Organization。同一版本可在多个 Project 使用，但每个 Project 必须显式启用精确 PUBLISHED 版本；新发布不自动启用或升级。资源就绪度按项目独立计算，启用关系不授予资源/Tool 权限。

## Skill 导入、解释与发布

| 对象 | 责任 |
| --- | --- |
| SkillSource | 不可变来源目录、附件、metadata、hash 与文件索引 |
| SkillInterpretation | 一次追加式解释；重新调整保留 parent lineage |
| CapabilityBlueprint | 能力、目标、资源、规则、交付物、交互/效果意图及来源依据 |
| ResourceRequirement / OutcomeDefinition | 抽象资源条件与交付/证据要求，不绑定 Project Secret |
| Skill / SkillVersion | 稳定业务身份 / 冻结内容的精确版本；生命周期状态与内容分开 |
| RuntimeManifest | 版本化运行投影，冻结蓝图、Tool/Task 与可选业务 Schema |
| SkillComposition / ProjectComposition | 精确版本的展示组合及项目启用；不是新系统角色或自动编排器 |
| ProjectSkillVersion | Project 对精确 PUBLISHED 版本的启停记录；当前停用关系不能重新启用 |

领域 capability 可是新概念，真实 Tool 调用才要求平台注册的 versioned capability。合法蓝图与发布门禁不能因 GUIDANCE_ONLY 或缺资源而省略。

发布、项目启用、readiness 是不同判断：同版在 A 项目可运行，在 B 项目缺资源；ACTIONABLE 不等于具体变更已批准。详见[Skill 契约](skill-contract.md#发布与就绪的判断顺序)与[解释实现](skill-interpretation.md)。

## 任务、Run 与会话

### 任务定义

ExecutableTask 从精确已发布蓝图投影，无独立 task 业务表，task_id 由服务端生成。ResourceBinding 将抽象要求绑定 Integration、文档或用户输入；Project/Task 配置在创建时冻结为 Run 资源。

AgentTaskBriefSnapshot 按 Segment 冻结目标、原 Skill、规则、资源、权限与交付要求。TaskSchedule 保存精确版本、输入及选择规则；每次 ONCE/CRON 触发再经普通创建服务生成 Run，必带 IANA timezone，见[调度](task-scheduling.md)。

### 执行对象

```text
Run：固定目标、版本、权限与资源
├── Segment 1：初始工作
│   ├── Attempt 1：Worker 领取
│   └── Attempt 2：同段技术恢复
├── Segment 2：业务答复/效果处理后续行
├── RunInputSnapshot：整个 Run 的准备回执
├── 主 AgentSession：顺序 resume/fork/replace
│   └── SUBAGENT / BRANCH：受限只读并行
└── Result、Evidence、Artifact、Interaction、Proposal 与审计
```

Brief 按 Segment 冻结，输入准备回执按 Run 复用，lease 按 Attempt 校验；不是同一种 checksum 对象。RunInputSnapshot 已有模型与部分接线，完整恢复仍见[资源快照](resource-snapshots.md)。RunBudgetAccount/Reservation/Receipt 有内部载体，但尚未接入主/子执行，不从实体存在推断共享预算已生效。

RunStep 是 STEP_* 事件的投影，不是独立持久表。RunEvent 是追加式审计/SSE 来源；TEXT_DELTA 可消耗 sequence 却不持久化，欠号不自动说明丢事件。

### 用户交互与外部效果

- UserInteraction/InteractionResponse：问题、期限、版本与原答复身份；CLARIFICATION/CHOICE/REVIEW 走[普通答复](user-interactions.md)，EFFECT_APPROVAL 必须关联 Proposal。
- ChangeProposal/Approval/EffectExecution：精确变更、决定和真实执行/回读事实；APPROVED 不证明 APPLIED 或模型已续行。
- Result：最多一个终态原始结果，可含通用 Outcome；失败/取消可没有 Result，SUCCEEDED 不证明业务完整。
- Evidence/Artifact：证据及产物引用；引用格式、可访问对象和实际内容验证不能合并。
- Evaluation：人工评分和修订建议；多条建议均相对同一原值，不顺次覆盖 Result，见[结果设计](results-evaluation.md)。

## 核心字段与实际载体

完整列见 [db/models.py](../../PJM/backend/src/projectmind/db/models.py)，公开数据需同时核对 [Schema](../../PJM/contracts/)、route 和 [OpenAPI](../../PJM/contracts/openapi/projectmind-api.v1.json)。概念不等于同名表，快照存在不证明与工作副本同步。

| 领域 | 持久化入口与限制 |
| --- | --- |
| 账户 | users / auth_sessions / user_security_events；账户 row_version 不覆盖偏好/登录时间，审计唯一键不等于 DB 禁止任意修改 |
| Skill | skill_sources / skill_interpretations / skills / skill_versions / runtime_manifests；Blueprint 内嵌于解释与 Manifest |
| 项目资源 | projects / project_members / integrations / resource_bindings / project_documents；字节保存与数据库提交不同 |
| 凭据 | secret_references / managed_secret_material；引用 key_version 与密文 kek_version 不是同层版本 |
| 任务配置 | project_skill_versions、组合表、task_schedules；当前启停/成员单行记录不代表完整历次审计 |
| 执行 | runs / run_skill_snapshots / run_segments / run_attempts / agent_task_brief_snapshots |
| Session | agent_sessions / agent_session_transcripts / agent_session_entries；transcript 追加镜像，不替代业务事实 |
| 输入与预算 | run_input_snapshots（0029）；run_budget_accounts/reservations/receipts（0030），有载体不等于消费者全部接齐 |
| 交互/效果 | user_interactions / interaction_responses / change_proposals / change_approvals / effect_executions |
| 结果/事件 | run_results / evaluations / evidence / run_events / outbox_messages；Artifact 不假设独立表 |

frontend_module_versions（0026）属于[生成展示](generated-modules.md)，不是 SkillComposition；构建、发布与 Host 尚未接通，不能混用版本回退。

Run 创建意图保存在 task_snapshot_json.creation_request；其身份与 runtime snapshot 不同。兼容 hash 与事务规则见[Run 创建](run-creation.md)，不改写旧快照补造请求。

## 权限模型

| 操作 | ADMIN | USER |
| --- | --- | --- |
| 管理组织用户、项目成员、Integration、Secret | 本组织允许 | 禁止 |
| 本人账户、安全历史、改密、撤销本人会话 | 本人范围 | 本人范围 |
| 导入/解释/发布/废弃 Skill，项目启停版本 | 本组织允许 | 禁止 |
| 执行任务、读 Run/结果、追加评价 | 仍受项目与业务硬约束 | 活动成员及对应业务条件 |
| 普通答复 | Project 写权限及交互版本/期限 | 同左 |
| 外部批准 | system ADMIN | 当前有 Project 写权限的 Run 发起人 |
| 低风险预授权 | 精确 scope；repository.write 除外 | 禁止 |
| 删除审计 | 禁止；仅独立保留策略清理 | 禁止 |

判定顺序：平台硬拒绝 → 当前身份/成员 → Project 策略/Integration scope → Skill/Task 要求 → Run 冻结上限 → 参数级 Tool 校验 → 精确批准/适用预授权。后一层不能覆盖前面的拒绝，用户文本和模型建议不扩权。

## 状态与不变量

### 状态流转

SkillInterpretation 为 ANALYZING → PREVIEW_READY → SUPERSEDED，失败为 FAILED；SkillVersion 为 DRAFT → PUBLISHED → DEPRECATED，废弃不可复活。readiness 的 GUIDANCE_ONLY/CONFIGURATION_REQUIRED/RUNNABLE/ACTIONABLE 不替代发布状态。

Proposal 从 DRAFT/PENDING_APPROVAL 到 APPROVED/APPLYING/APPLIED，也可 REJECTED/STALE/FAILED；EffectExecution 有独立 REQUESTED/LEASED/APPLYING/APPLIED 与失败/重试路径，见[受控写入](repository-effects.md)。

### Run 状态速查

| 事件 | 常见 Run 路径与执行变化 |
| --- | --- |
| 首次启动 | QUEUED → PREPARING → RUNNING；初始 Segment，claim 创建 LEASED Attempt |
| 等待用户/批准 | WAITING_FOR_INPUT/WAITING_FOR_APPROVAL；Segment WAITING，Attempt DEFERRED，释放 lease |
| 有效答复/独立期限处理 | 追加 Segment，重新 QUEUED；原答复和批准不覆盖 |
| lease 失效可恢复 | RETRY_PENDING → PREPARING；原 Attempt LEASE_EXPIRED，同段追加 Attempt |
| 结束 | SUCCEEDED/FAILED/CANCELLED；terminal RUN_SNAPSHOT，终态不重新打开 |

允许转换与 enum 以 [runs/domain.py](../../PJM/backend/src/projectmind/runs/domain.py)为准；WAITING_PERMISSION 仅旧协议兼容。RUNNING 可能先于资源物化，不代表输入 READY 或 Session 已启动。取消受理、终态、进程退出和用量结清分别举证。

### 执行不变量

- 等待释放 lease，不计 engine wall timeout；普通交互期限独立，过期以 INTERACTION_TIMEOUT 新段携带缺失事实，不代答推荐项。批准过期不执行 Provider。
- 普通答复/效果处理追加 Segment；可恢复 lease 失效追加同段 Attempt。engine wall timeout 按 FAILED，不能泛化为可重试。
- aggregate 锁顺序固定 Run → Segment → Attempt 或 Proposal/Interaction/Effect；不先锁子对象。
- terminal RUN_SNAPSHOT 是最后持久事件；PostgreSQL 为正本，状态与 Outbox 同事务，消费幂等。
- 改目标、项目、精确版本、权限上限或已用资源须新 Run；Session 变化不改变冻结边界。
- 预算需跨主/子/续行共享；[内部账本](run-budgets.md)和局部限制均不证明此目标已落实。
- 外部执行重验版本、binding、scope、前置条件与批准，Provider 必须有可验证并发/幂等和 read-back。
- Result 保持原值，Evaluation 只追加。

## 实施兼容说明

新 Run 显式创建 Segment 1 和 immutable binding；旧 Run 按隐式 Segment 只读投影，不回填虚构事实。旧版本、Run 和业务结果继续只读审计，不恢复业务专用 Schema/seed/renderer 为通用规则来源。

模型、migration、repository、公开契约与 Web 必须一起核对；真实锁竞争、事务回滚、历史重放、引用删除保护及跨 Project 拒绝分别验收，不由 DTO 或测试文件存在推断完成。
