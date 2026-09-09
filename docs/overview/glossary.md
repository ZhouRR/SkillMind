# 核心术语

这里只解释词义与易混淆的边界；完整规则见[设计索引](../design/README.md)。

## 从工作方法到执行

| 术语 | 含义 |
| --- | --- |
| SkillSource / SkillInterpretation | 导入来源 / 一次解释记录；导入不执行来源脚本，重解释不覆盖旧记录 |
| CapabilityBlueprint | 目标、规则、资源和完成条件的语义来源，不授予 Tool 权限 |
| RuntimeManifest | 精确版本的运行投影与约束，不用来反推新蓝图 |
| SkillVersion / ProjectSkillVersion | 内容不可变的发布版 / 项目显式启用关系；发布、启用与 readiness 分开 |
| Task / ExecutableTask | 从版本投影的可启动工作，不必对应独立数据库表 |
| SkillComposition | 多 SkillVersion 的展示组合；虚拟角色不是权限角色 |
| FrontendModuleVersion / ViewSpec | 生成界面版本 / 平台声明式展示；与业务 module API、文档预览分开 |

## 资源与权限

| 术语 | 含义 |
| --- | --- |
| Domain / Tool capability | 业务能力名称 / 注册的版本化 Tool 协议，前者不能直接调用后者 |
| ResourceRequirement / Integration | 抽象资源需求 / 实际 Provider、实例和范围 |
| ResourceBinding | requirement 到资源的选择；配置与 Run 冻结记录不是同一层 |
| 内容快照 / 物化副本 | ID/hash/revision / 实际文件；授权固定不等于全部内容已取得 |
| RunInputSnapshot | Run 级准备回执；READY 绑定完整文件集合，PREPARING 不可使用 |
| 逻辑 input / 物理世代 | Agent 所见路径 / 平台隔离存储位置，由平台映射 |
| User / ProjectMember | 账户 / 项目成员关系；账户创建和项目偏好不授予 membership |
| AuthSession / CSRF | 浏览器会话 / 会话绑定的写请求校验值，不是模型 AgentSession |
| 账户版本 / 安全事件 | 管理并发版本 / 接受的安全操作；不是在线人数或完整登录日志 |
| 登录配额 / 退避 | 短期计数 / 有限等待，不等于停用账户或撤销会话 |
| SecretReference | 外部凭据引用，公开响应不含原值、locator 或密文 |
| key_version / kek_version | 引用 metadata 版本 / 部署加密密钥版本，均不代表外部凭据已更新 |
| Readiness | 由 Provider 和配置计算的就绪状态，执行时仍须重新校验权限 |

## 一次 Run 内部

```text
Run（固定目标、权限与输入）
└── Segment（连续工作；业务答复后追加）
    └── Attempt（一次技术执行；重试后追加）
        ├── PRIMARY Session（同一时刻一个活动主会话）
        └── SUBAGENT Session（有界只读分支）
```

| 术语 | 含义 |
| --- | --- |
| AgentTaskBrief | 该 Segment 的目标、指导、资源与策略快照 |
| lease / fencing | 限时执行权 / 提交时拒绝失效持有者 |
| 准备 / wall / job timeout | 分别约束准备、模型等待和 Worker job，不是 Run 累计预算 |
| 限额 / 预留 / 消耗 | 上限 / 未确认结清的占用 / 已确认用量；usage、cost 和成功状态分别判断 |
| UserInteraction / ChangeProposal | 普通问题或批准交互 / 具体变更提案；回答、批准与评价不是同一种提交 |
| EffectExecution | 已批准变更的执行和回读记录，批准不保证远端已经完成 |
| Evidence / Artifact | 可定位、带版本的事实引用 / 报告或补丁文件 |
| Result / OutcomeEnvelope | 不可变原结果 / 通用输出包络，执行成功不等于业务判断全对 |
| Evaluation | 追加式评分和修订，原值来自 Result，不覆盖原结果或续行 Run |
| Task Flow / Run Flow | 版本化计划 / 实际活动投影，不是第二个执行控制器 |

## 创建与调度恢复

| 术语 | 含义 |
| --- | --- |
| Idempotency-Key / 请求身份 | 定位一次提交 / 判断其内容相同；持有键不授予访问权 |
| 创建重放 | 确认首次创建的 Run，不重新冻结资源或追加执行 |
| occurrence | 计划触发时刻，不等于 Worker 开始时间或 Run 终态 |
| missed / overlap | 错过发火 / 重叠跳过；当前重叠判断限于本 Schedule 摘要 |
| Schedule 摘要 / 触发账本 | 可覆盖的 last_*、run_count / 待完整实现的逐次持久记录 |
| 在途恢复 / 补跑 | 完成已经认领的一次 / 为过去未处理时刻新造执行，前者不授权后者 |
