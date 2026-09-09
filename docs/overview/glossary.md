# 核心术语

## 从工作方法到执行

| 术语 | 回答的问题 | 容易混淆的边界 |
| --- | --- | --- |
| SkillSource | 导入了哪些原始说明与附件？ | 保存来源，不执行其中的脚本 |
| SkillInterpretation | 这一次如何理解来源？ | 调整产生新记录，不覆盖旧解释 |
| CapabilityBlueprint | 能做什么、需要什么、怎样才算完成？ | 语义来源；不授予 Tool 权限 |
| RuntimeManifest | 发布版本携带哪些运行投影与约束？ | 不从它反推新蓝图 |
| SkillVersion | 本次使用哪个精确版本？ | PUBLISHED 后内容不可改；新版本不自动启用 |
| ProjectSkillVersion | 哪个项目可以发现这个版本？ | 启用不等于资源就绪 |
| Task / ExecutableTask | 用户可以启动哪项工作？ | 从版本投影的任务描述，不必是一张独立业务表 |
| SkillComposition | 哪些 SkillVersion 组合展示？ | 虚拟角色不是 ADMIN/USER 权限角色 |

“模块”也可能指生成界面。现有 module API 管理 SkillComposition；FrontendModuleVersion 则是尚无投放链路的生成展示版本。另有禁脚本的文档预览和平台 ViewSpec，详见[概念区分](../design/generated-modules.md#先分清三种模块与预览)。它们不能按同名复用安全边界或回退操作。

“可用”不是一个通用状态：[兼容级别](../design/skill-contract.md#71-兼容级别)说明如何适配来源，发布状态属于版本，启用关系属于项目，readiness 属于任务投影。具体[判断顺序](../design/skill-contract.md#发布与就绪的判断顺序)不能省略，`native` 或 `gate_passed` 都不是执行许可。

## 资源与权限

| 术语 | 含义 |
| --- | --- |
| Domain capability | 业务能力名称，允许平台未知的业务概念 |
| Tool capability | 注册的版本化工具协议，例如 `repository.read/v1` |
| ResourceRequirement | Skill 提出的抽象资源前提 |
| Integration | 项目中实际配置的资源实例、Provider 和范围 |
| ResourceBinding | requirement 到资源的选择；Project/Task 层配置在 Run 创建时冻结 |
| 内容快照 / 物化副本 | 具体 ID、hash、revision 与实际文件；不能从授权 binding 自动推导创建时的全部内容 |
| 输入回执 / RunInputSnapshot | 数据库保存的 Run 级准备记录；READY 绑定完整文件集合，PREPARING 尚不能使用。不是 manifest 的自证或公开文档清单，验收状态见[计划](../planning/roadmap.md#13-当前执行状态) |
| 逻辑 input / 物理世代 | Agent 使用的 `input/...` 路径 / 平台保存的隔离副本目录；由平台映射，不由 Agent 选目录 |
| User / ProjectMember | 平台账户 / 某项目的成员关系；创建账户不自动加入项目，项目偏好也不授予成员资格 |
| AuthSession / CSRF | 浏览器会话 / 与该会话绑定的写请求校验值；[两页面例子](../design/authentication.md#一个例子同一账号打开两个页面)说明 v2 正常读取返回稳定值，换会话或失效仍会拒绝。不是 AgentSession，也不单独授予业务权限 |
| 账户版本 / 安全事件 | 用户管理的并发版本 / 一次接受的安全操作事实；[停用再启用的例子](../design/user-lifecycle.md#一个例子停用再启用不恢复旧登录)说明旧会话为何仍须失效。内部载体不等于公开管理功能，事件不是全部字段差异或登录日志 |
| 登录配额 / 退避 | 进入密码验证前的短期请求计数 / 超限后的有限等待；[一次登录的例子](../design/login-protection.md#一个例子一次登录两次入口请求)区分来源请求数和账号尝试数。不是账号停用、会话失效或 Run 预算 |
| SecretReference | 外部 Provider 凭据定位或托管密文的引用；不是登录会话，公开响应不含 locator 或原值 |
| key_version / kek_version | 引用 metadata / 密文使用的部署密钥版本；[存储轮换](../design/secret-storage.md#轮换不是更换外部凭据)不等于更换外部凭据 |
| Readiness | 根据已安装 Provider 和项目配置计算的任务就绪度；不替代最终权限校验 |

## 一次 Run 内部

```text
Run（一个目标、固定权限与输入）
└── Segment（一次连续工作；回答/批准后追加）
    ├── Attempt 1（首次领取）
    └── Attempt 2（同段故障重试）
        ├── PRIMARY Session（一个活动主会话）
        └── SUBAGENT Session（可选的并行只读分析）
```

| 术语 | 作用 |
| --- | --- |
| AgentTaskBrief | 每段传给 Agent 的目标、规则、资源与策略快照 |
| lease / fencing | 当前 Attempt 的限时执行权 / 提交时拒绝已失效持有者；有 heartbeat 不代表可以省略提交校验 |
| 准备 timeout / wall timeout / job timeout | 分别约束 ContextBuilder、模型事件等待、整个 Worker job；[覆盖范围](../design/run-budgets.md#现有计时器的覆盖范围)不同，不相加成 Run 累计预算 |
| 限额 / 预留 / 消耗 | 冻结上限 / 尚未转成确认消耗或可靠释放的占用 / 已确认消耗；[数字例子](../design/run-budgets.md#一个例子已用占用与可用)把待核对量留在预留中。[内部账本与执行接入](../design/run-budgets.md#持久账本的当前载体)分开判断，不把已有组件当作运行保证 |
| usage / cost / 分支 outcome | 当前用量报告 / 费用摘要 / 执行结论是不同事实；[计量入口](../design/run-budgets.md#用量现在流向哪里)不等于累计账本，[子结果](../design/subagents.md#当前返回值的可信边界)也不证明用量结清 |
| UserInteraction | 澄清、选择、Review 或外部变更批准请求 |
| ChangeProposal | 可审查的具体变更；它本身不是批准 |
| EffectExecution | 平台实际执行已批准变更及回读验证的记录 |
| Evidence | 对取得的事实、内容版本和位置的引用 |
| Artifact | 报告、补丁等文件产物 |
| Result / OutcomeEnvelope | 不可变原始结果 / 通用结果包络 |
| Evaluation | 对 Result 的追加式人工评分与修订 |
| Task Flow / Run Flow | 待实现的版本化计划 / 本 Run 的实际观察投影；[示例](../design/task-flow.md#一个例子计划不等于执行事实)区分建议、required 与无关联动态活动，不引入第二个执行控制器 |

## 创建与调度恢复

| 术语 | 含义与区别 |
| --- | --- |
| Idempotency-Key / 请求身份 | 键定位一次创建，身份判断请求内容是否相同；不是持有键就获得授权 |
| 创建重放 | 确认首次成功创建的 Run，不再冻结资源或追加执行；[创建设计](../design/run-creation.md)区分后端入口、客户端待确认提交与剩余验收 |
| occurrence | Schedule 算出的计划时刻，不是 Worker 实际开始时刻，也不是 Run 的终态结果 |
| missed / overlap | 错过计划时刻 / 重叠而跳过。当前只查本 Schedule 的 last_run_id，不扫描同 Task 全部执行；[实际范围](../design/task-scheduling.md#重叠检查到底看谁)不能扩大成全局串行保证 |
| Schedule 摘要 / 触发账本 | last_*、run_count 是可覆盖的摘要；[示例](../design/task-scheduling.md#一个例子规则触发与执行分别看)说明时间、原因和 Run ID 未必属于同次触发。持久 occurrence 记录仍待实现 |
| 在途触发恢复 / 历史补跑 | 处理已经认领但未结算的一次 / 为过去未处理时刻新造执行；[调度修正](../design/task-scheduling.md#可靠性修正要求待实现)不自动授权后者 |
