# 领域模型与权限

本页是实体与边界地图，术语见[词汇表](../overview/glossary.md)，状态见[计划](../planning/roadmap.md#当前执行状态)。详细协议由各专题维护，不把特定业务字段固化为通用模型。

## 建模原则

- Skill 定义目标/资源/规则/产物，不授权；平台固定跨 Skill 控制协议，不预定义全部业务 Schema。
- Organization 保存可复用资产，Project 显式启用精确版本并绑定资源。
- Run 固定目标/权限；业务续行追加 Segment，技术恢复追加 Attempt，模型 Session 仅是执行载体。
- 发布内容、Run snapshot、Result 不变；答复、评价、批准和审计独立追加。
- 外部写入经 propose → 批准/适用预授权 → 执行 → 回读，模型建议不成为权限。

## 组织、项目与资源

| 对象 | 责任 |
| --- | --- |
| Organization | 组织隔离；当前单组织，不代表多租户登录 |
| User / AuthSession | ADMIN/USER、ACTIVE/DISABLED；会话保存 hash、期限、撤销、凭据版/登录角色，不是 AgentSession |
| UserSecurityEvent | 账户安全审计，不是登录尝试或 RunEvent |
| Project / ProjectMember | 项目边界及 ACTIVE/REMOVED 关系，无 Project ADMIN 角色 |
| ProjectMemberEvent | 关系变更与原 actor/request 同事务审计，不补造历史 |
| Integration | 项目版本化 Provider 资源/scope；内置文档无需 Integration 行 |
| SecretReference | 凭据定位，MANAGED 材料独立加密，原值不进 Run |
| ProjectDocument | 元数据/blob，是 ProjectKnowledge 的实际载体，非自动同步或独立知识索引 |

[项目](project-lifecycle.md)负责成员/偏好/归档/删除；归档不停止 Run，移除成员不撤销全部会话，本组织 ADMIN 不依赖 membership。[用户管理](user-lifecycle.md)和[会话 v2](authentication.md#会话凭据-v2-与切换要求)负责账户/凭据。

SkillSource/Interpretation/Version/Manifest 归组织；同版跨 Project 复用须分别显式启用精确 PUBLISHED，发布不自动启用/升级，readiness 独立计算且不增加权限。

## Skill 导入、解释与发布

| 对象 | 责任 |
| --- | --- |
| SkillSource | 不可变目录/附件/metadata/hash/文件索引 |
| SkillInterpretation | 追加解释与 parent lineage |
| CapabilityBlueprint | 能力/目标/资源/规则/交付/交互/效果及来源 |
| ResourceRequirement / OutcomeDefinition | 抽象资源与交付/证据要求，不绑 Secret |
| Skill / SkillVersion | 稳定身份 / 冻结版本；状态与内容分开 |
| RuntimeManifest | 冻结蓝图/Tool/Task 与可选业务 Schema |
| SkillComposition / ProjectComposition | 精确版本展示组合/项目关联，不是角色或编排器 |
| ProjectSkillVersion | 精确 PUBLISHED 启停记录，当前停用关系不可重新启用 |

新领域 capability 不要求注册，真实 Tool 调用才须注册 versioned capability。缺资源/GUIDANCE_ONLY 不豁免蓝图和发布门禁。发布、启用、readiness、具体批准分别判断，见[Skill 契约](skill-contract.md#发布与就绪的判断顺序)、[解释实现](skill-interpretation.md)。

## 任务、Run 与会话

### 任务定义

ExecutableTask 从精确发布蓝图投影，task_id 服务端生成，无独立 task 表。ResourceBinding 绑定 Integration/文档/用户输入，在创建时冻结；AgentTaskBriefSnapshot 按 Segment 保存原 Skill、目标、规则、资源/权限和交付。

TaskSchedule 固定版本/输入/选择规则，ONCE/CRON 每次经普通创建服务生成 Run，必带 IANA timezone，见[调度](task-scheduling.md)。

### 执行对象

```text
Run：目标、版本、权限与资源
├── Segment 1：初始工作
│   ├── Attempt 1：领取
│   └── Attempt 2：同段技术恢复
├── Segment 2：答复/效果后的业务续行
├── RunInputSnapshot：整个 Run 的准备回执
├── 主 AgentSession：顺序 resume/fork/replace
│   └── SUBAGENT / BRANCH：受限只读并行
└── Result / Evidence / Artifact / Interaction / Proposal / 审计
```

Brief、Run 输入回执、Attempt lease 不是同一 checksum 对象。输入完整恢复见[资源快照](resource-snapshots.md)，主/子/续行共享计量见[预算](run-budgets.md)；内部实体存在不代表消费者已接齐。

RunStep 只是 STEP_* 事件投影，无独立表。RunEvent 是追加审计/SSE 来源；TEXT_DELTA 可用 sequence 而不持久化，欠号不自动表示事件丢失。

### 用户交互与外部效果

| 对象 | 不可混淆的事实 |
| --- | --- |
| UserInteraction / InteractionResponse | 问题/期限/版本及原答复；[普通答复](user-interactions.md)与必须关联 Proposal 的 EFFECT_APPROVAL 分路 |
| ChangeProposal / Approval / EffectExecution | 精确变更、决定、真实执行/回读；APPROVED 不证明 APPLIED 或已续行 |
| Result | 最多一个终态原始结果；失败/取消可无 Result，SUCCEEDED 不证明业务完整 |
| Evidence / Artifact | 引用格式、对象可访问、实际内容须分别验证 |
| Evaluation | 多条修订均相对原 Result，不顺次覆盖，见[结果](results-evaluation.md) |

## 核心字段与实际载体

完整字段以 [models](../../SKM/backend/src/skillmind/db/models.py)、[Schema](../../SKM/contracts/)、route/[OpenAPI](../../SKM/contracts/openapi/skillmind-api.v1.json)为准；概念不必有同名表。

| 领域 | 持久入口 / 说明 |
| --- | --- |
| 账户 | users、auth_sessions、user_security_events；row_version 不覆盖偏好/登录时间 |
| 项目/资源 | projects、project_members/events、integrations、resource_bindings、project_documents；项目版本只覆盖 metadata/归档，DB/blob 分别提交 |
| Skill | skill_sources/interpretations、skills/versions、runtime_manifests；Blueprint 内嵌解释/Manifest |
| 凭据 | secret_references、managed_secret_material；key_version 与 kek_version 不同层 |
| 任务配置 | project_skill_versions、组合表、task_schedules；单行启停不是历次审计 |
| 执行 | runs、run_skill_snapshots、run_segments/attempts、agent_task_brief_snapshots |
| Session | agent_sessions/transcripts/entries；transcript 镜像不代替业务事实 |
| 输入/预算 | run_input_snapshots、run_budget_accounts/reservations/receipts；接入状态见所属专题/计划 |
| 交互/效果 | user_interactions/responses、change_proposals/approvals、effect_executions |
| 结果/事件 | run_results、evaluations、evidence、run_events、outbox_messages；Artifact 不假设独立表 |

frontend_module_versions 属[生成展示](generated-modules.md)，不是 SkillComposition，构建/投放/Host 尚未接通。Run 创建意图位于 task_snapshot_json.creation_request，与运行快照身份不同；按[创建协议](run-creation.md)兼容，不回填请求。

## 权限模型

| 操作 | ADMIN | USER |
| --- | --- | --- |
| 管理用户/成员/Integration/Secret | 本组织 | 禁止 |
| 本人账户/安全历史/改密/撤销 | 本人 | 本人 |
| 导入/解释/发布/废弃 Skill、版本启停 | 本组织 | 禁止 |
| 任务、Run/结果、评价 | 仍受项目及业务硬约束 | 活动成员及业务条件 |
| 普通答复 | Project 写权限、交互版本/期限 | 同左 |
| 外部批准 | system ADMIN | 有 Project 写权限的 Run 发起人 |
| 低风险预授权 | 精确 scope，排除 repository.write | 禁止 |
| 删除审计 | 禁止，独立保留策略清理除外 | 禁止 |

判定依次为平台硬拒绝 → 当前身份/成员 → Project 策略/Integration scope → Skill/Task 要求 → Run 冻结上限 → Tool 参数 → 精确批准/适用预授权。后层不覆盖前层拒绝，用户文本/模型建议不扩权。

## 状态与不变量

### 状态流转

SkillInterpretation：ANALYZING → PREVIEW_READY → SUPERSEDED，失败 FAILED；SkillVersion：DRAFT → PUBLISHED → DEPRECATED，废弃不复活。readiness 的 GUIDANCE_ONLY/CONFIGURATION_REQUIRED/RUNNABLE/ACTIONABLE 不替代发布状态。

Proposal 的 DRAFT/PENDING_APPROVAL、APPROVED/APPLYING/APPLIED 与 REJECTED/STALE/FAILED，和 EffectExecution 的 REQUESTED/LEASED/APPLYING/APPLIED、失败/重试分别记录，见[受控写入](repository-effects.md)。

### Run 状态速查

| 事件 | Run / 执行变化 |
| --- | --- |
| 首次启动 | QUEUED → PREPARING → RUNNING；初始 Segment，claim 创建 LEASED Attempt |
| 等待用户/批准 | WAITING_FOR_INPUT/WAITING_FOR_APPROVAL；Segment WAITING、Attempt DEFERRED、释放 lease |
| 有效答复/独立期限处理 | 追加 Segment、重新 QUEUED，不覆盖答复/批准 |
| 可恢复 lease 失效 | RETRY_PENDING → PREPARING；原 Attempt LEASE_EXPIRED，同段追加 Attempt |
| 结束 | SUCCEEDED/FAILED/CANCELLED；terminal RUN_SNAPSHOT，终态不重开 |

enum/转换以 [runs/domain.py](../../SKM/backend/src/skillmind/runs/domain.py)为准，WAITING_PERMISSION 仅旧协议兼容。RUNNING 可先于物化，不证明输入 READY/Session 启动；取消受理、终态、进程退出和结清分别确认。

### 执行不变量

- 等待释放 lease、不计 engine wall timeout；普通过期以 INTERACTION_TIMEOUT 新段携带缺失事实，不代答；批准过期不调用 Provider。
- 业务续行加 Segment、可恢复失 lease 加 Attempt；engine wall timeout 为 FAILED，不泛化重试。
- aggregate 锁 Run → Segment → Attempt 或 Proposal/Interaction/Effect，不先锁子对象。
- PostgreSQL 为正本，状态/Outbox 同事务、消费幂等，terminal RUN_SNAPSHOT 为最后持久事件。
- 改目标/项目/精确版/权限上限/已用资源须新 Run；Session 变化不扩冻结边界。
- 主/子/续行预算共享、Provider 参数/批准/并发幂等与 read-back 仍须按[预算](run-budgets.md)及[效果](repository-effects.md)协议验证，局部限制不代替完整保证。
- Result 不变，Evaluation 只追加。

## 实施兼容说明

新 Run 显式 Segment 1/immutable binding；旧 Run 仅隐式段只读投影，不造历史。旧版本/Run/结果保留审计，不把业务专用 Schema/seed/renderer 恢复为平台规则。

模型、migration、repository、契约与 Web 成套核对；真实锁/回滚、历史重放、删除引用与跨 Project 拒绝分别举证。
