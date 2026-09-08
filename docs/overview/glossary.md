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

## 资源与权限

| 术语 | 含义 |
| --- | --- |
| Domain capability | 业务能力名称，允许平台未知的业务概念 |
| Tool capability | 注册的版本化工具协议，例如 `repository.read/v1` |
| ResourceRequirement | Skill 提出的抽象资源前提 |
| Integration | 项目中实际配置的资源实例、Provider 和范围 |
| ResourceBinding | requirement 到资源的选择；Project/Task 层配置在 Run 创建时冻结 |
| 内容快照 / 物化副本 | 具体 ID、hash、revision 与实际文件；不能从授权 binding 自动推导创建时的全部内容 |
| SecretReference | 凭据定位或托管密文的引用；公开响应不包含 Secret |
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
| UserInteraction | 澄清、选择、Review 或外部变更批准请求 |
| ChangeProposal | 可审查的具体变更；它本身不是批准 |
| EffectExecution | 平台实际执行已批准变更及回读验证的记录 |
| Evidence | 对取得的事实、内容版本和位置的引用 |
| Artifact | 报告、补丁等文件产物 |
| Result / OutcomeEnvelope | 不可变原始结果 / 通用结果包络 |
| Evaluation | 对 Result 的追加式人工评分与修订 |
| Task Flow / Run Flow | 待实现的计划展示 / 实际事件展示；不引入第二个执行控制器 |
