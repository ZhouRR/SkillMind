# 产品概览

Skillmind 把目录式 Skill 中的工作方法转成可选择、配置、执行的项目任务，统一提供资源访问、持续执行、人工参与、证据和审计。
[当前状态](../planning/roadmap.md#当前执行状态)记录实际完成度，本页是完整产品范围，不是首版必做清单；首版先验证一个真实 Skill 和一种只读资源。

## 一次使用过程

```text
导入 Skill → 解释与预览 → 发布精确版本 → 项目启用与资源配置
                                              ↓
                                      任务中心选择任务
                                              ↓
                                      工作空间观察 Run
                                              ↓
                              分析 ↔ 回答问题 / 审查变更
                                              ↓
                                  结果 + 证据 + 人工评价
```

发布表示版本通过平台校验，项目启用决定可发现版本，资源就绪判断能否执行；三者不是一个“可用”开关。
需要修改外部系统时，平台展示具体 Proposal，获得批准后执行并回读验证。

## 用户与页面

| 使用者 | 主要工作 |
| --- | --- |
| ADMIN | 管理组织 Skill、精确版本、项目和资源 |
| 项目成员 USER | 选择任务、提供输入、查看过程和反馈结果 |
| Run 发起人或 ADMIN | 审查具体外部变更；权限由平台重新校验 |

任务中心选择“运行什么”，工作空间观察“一次 Run”。页面与未接入的管理入口见 [Workspace](../design/workspace.md)。
Project 隔离资源与执行，不是系统角色；归档、撤权、取消和删除分别处理，见[项目生命周期](../design/project-lifecycle.md)。

## 产品边界

- 业务规则来自 Skill，平台只固定通用协议；业务输入/输出 Schema 可选。
- Integration 与 ResourceBinding 限定资源，Agent 不接触凭据；输入与权限按运行协议冻结。
- 一个 Run 可包含多个 Segment/Session，技术恢复记为 Attempt。Result 不可改，人工修订追加 Evaluation。
- 外部写入采用 observe → propose → apply；repository 始终需要人工批准。
- 调度复用普通 Run 创建，子分析只读有界；共享预算、Task Flow、生成界面仍有未完成链路，不能把设计当作可用功能。

开放市场、条件监控、任意宿主 Shell/网络、自动升级 SkillVersion、自动合并 PR、多仓库原子变更及完整多租户计费不在当前范围。
继续阅读[系统结构](architecture.md)、[术语](glossary.md)或[开发入口](../development/change-guide.md)。
