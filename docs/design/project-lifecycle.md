# 项目、成员与归档边界

本页负责项目身份、成员、选择、归档与删除；账户、Run 停止、资产清理分别见[认证](authentication.md)、[执行监督](run-supervision.md)、[文档资产](document-lifecycle.md)。现有缺口见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。

## 一个例子：归档不是停止或删除

项目 P 归档后不再出现在默认活动列表，新建 Run/回答/批准被拒绝；授权历史仍可读，已有 Run、成员、Schedule、附件不会一起停止或删除。恢复复用原 ID/key/配置，但不恢复已移除成员、不自动恢复 ERROR Schedule 或补跑错过触发。

停止执行、移除成员、归档、物理删除分别操作；归档成功不是进程已停止的证明。

## 项目身份与成员资格

| 对象 | 含义 |
| --- | --- |
| Project | organization_id 隔离，project_id 是身份；组织内 key 唯一，归档仍占 key |
| ProjectMember | ACTIVE/REMOVED 关系，无 Project ADMIN/OWNER 角色 |
| User | 组织账户 ACTIVE/DISABLED、ADMIN/USER；建账户不自动入项目 |
| ProjectSkillVersion | 精确版本启用，不授予成员或系统权限 |

本组织 ADMIN 不依赖 membership，移除其成员关系不会撤销 ADMIN 权限；跨组织仍拒绝。USER 后续请求重验账户/会话及 ACTIVE 关系，不改写旧 Run actor/权限/结果，也不承诺已有 SSE 立即结束。

### 成员管理的现状与目标

ADMIN API 可列出/添加同组织 ACTIVE 用户/移除成员；列表含两种关系状态，无分页和独立用户状态，不把 membership ACTIVE 显示成账户已启用。响应使用公开字段白名单与 no-store。

重新添加 REMOVED 复用行并刷新 joined_at；重复添加 ACTIVE 返回原行，不追加事件；重复移除为 404。移除账户已停用用户的关系仍允许，不改变账户本身。

成员写入复用[用户事务](user-lifecycle.md#事务与并发)的 Organization → User（actor/target 去重排序）→ 原 AuthSession，再锁 Project → ProjectMember。锁后与 flush 后以新时刻复查原会话、ADMIN 和 CSRF；目标账户在持锁状态下检查 ACTIVE，锁保持到提交。偏好写入和项目删除也先取同一 Organization gate，避免 User → Project 引用与 Project → User 清理形成反向锁环。这不是全站授权或真实并发验收的证明。

关系变化与 project_member_events 一起提交或回滚：保存组织/项目/关系/目标/actor ID、ADDED/REMOVED、前后 status/joined_at、服务器生成 request UUID 与时间，不记录密码或任意正文。0033 只记录新操作，不由现有关系补造旧历史；有任何事件即拒绝降级丢表。request UUID 不是幂等键，当前没有成员审计查询 API，当前关系与原操作结果须区分。

### 成员管理页面

项目管理的成员页签仅向 ADMIN 提供；以精确已授权项目为边界，归档项目可读/移除但不可添加。候选复用组织账户的服务端搜索和分页，显示账户角色/状态，停用账户不可添加，不过滤第一页冒充全量。成员列表单独标明关系状态与 ADMIN 权限例外。

添加、重新添加和移除先确认原项目/用户，每次只允许一个写请求。超时、中断、断连或成功响应损坏均为结果未知，不自动重放，也不把当前关系符合目标解释为原请求成功。用户显式重读原成员列表与精确账户、两项均成功后才能人工解除新写门禁；解除不执行原动作。切换页签或同项目资格刷新保留未知状态，重新确认资格期间暂停交互并中断在途写为未知；换 actor/项目或离页不迁移旧请求，页面不承诺跨刷新恢复。

## 项目选择与失效链接

preference 只是上次选择，不是授权。服务端只返回当前可访问的 ACTIVE preference，否则 null，不保证立即清掉旧引用；删项目才显式清理。

| 进入方式 | 要求 |
| --- | --- |
| URL 无项目 | 可选有效 preference、活动首项；无项目时保留平台入口 |
| URL 明确无效/无权项目 | 保留目标、统一不可访问，不静默换项目或暴露原因差异 |
| 主动切换或当前资格失效 | 确认/隔离旧草稿，阻止新提交，丢弃旧响应；不迁移未知写请求 |

[App](../../PJM/web/src/App.tsx)通过 [useProjectContext](../../PJM/web/src/hooks/useProjectContext.ts)分开读取活动列表、偏好与精确项目详情。空值、重复参数及非法 UUID 不是“未指定”；授权与不存在统一提示，不展示服务器内部原因。首次无参数可以选有效偏好或活动首项，一旦目标确定，列表变化、归档或重新读取都不把它换成别的项目。

进入另一个页面、切换目标或重新读取时，详情未确认前不挂载项目业务页面；平台入口保留。每次读取有独立身份与等待上限，返回曾访问过的 ID 也不能复用旧授权结果。主动切换清除旧 Run/Task 参数并隔离原页面；未知写入不自动迁移或重放。只有已确认 ACTIVE 的项目保存为偏好。

详情成功不代表列表也成功：列表失败仍提示并可重新读取，授权归档详情仍可显示。偏好失败不丢掉成功列表。上述检查不替代每次业务请求的服务器授权，也不承诺权限变化会即时推送到已打开页面。

## 归档的实际边界

归档/恢复只修改 status，保留配置；同状态重复操作不刷新时间，不调用 Run/Schedule/Integration/Session 停止服务。

| 入口 | 当前行为 |
| --- | --- |
| 列表/preference | 默认只 ACTIVE；include_archived 可读授权归档项目 |
| Project/Run 历史 | 保留当前授权读取；Web 活动选择器不是完整审计入口 |
| ProjectWriteActor | 409 project_archived，覆盖创建、回答、评价、批准与 Schedule 修改 |
| Run 取消 | WriteActor + Run 项目访问，无 ACTIVE 门槛；取消受理不证明进程停止 |
| ADMIN 维护 | 编辑、归档/恢复、删除、移除成员走独立用例；新增成员要求 ACTIVE，否则 404 |
| Schedule 触发 | 重新检查创建者/活动项目，但不与归档或 Run 创建同事务，不等于立即 PAUSED |

### 并发修改不能只看有无行锁

metadata/归档/恢复锁 Project，但没有 expected_row_version 或独立审计；行锁不能识别管理员过时草稿。目标定义统一版本/冲突协议，同步 DB/DTO/API/Web，保留非敏感草稿让用户比较，不自动换版本重发。

## 删除与数据保留

当前 ADMIN 先锁 Organization、再锁 Project，要求 ARCHIVED 且无 Run/TaskSchedule/成员审计，删除 preference 与列举配置再删除项目；成功 204。拒绝发生在任何关系删除之前：

| 阻止条件 | Problem code |
| --- | --- |
| 未归档 | project_delete_requires_archive |
| 存在 Run | project_delete_blocked_by_runs |
| 存在任意状态/发火/认领阶段的 Schedule | project_delete_blocked_by_schedules |
| 存在成员变更审计 | project_delete_blocked_by_member_audit |

不为释放 key 删除审计或放宽 FK RESTRICT；这些前置不是完整可删证明，也不是回收站。

| 已知缺口 | 风险 |
| --- | --- |
| 现有 Run/Schedule 检查不是完整引用证明 | 冻结输入、在途认领与并发新增仍须按统一提交协议验证 |
| 删 ProjectDocument 行不清 blob | 204 不证明附件、Run 副本或备份清除 |
| retention_days 仅配置 | 不保证自动清理、恢复或保留期定时器 |
| 引用检查与新增引用不同协议 | 归档/认领/创建/删除竞争尚需真实事务验证 |

### 删除门禁的修正要求

默认保留归档项目；物理删除只用于证明无执行/审计及在途引用的空项目。

1. 纳入所有 Run/Schedule（含暂停/归档/未触发）、冻结输入和在途认领；有引用明确冲突，不静默删规则/审计释放 key。
2. 所有新增引用、旧 Worker/恢复共用锁或条件提交，阻止检查后新增；关系删除同事务回滚，不拆 FK/宽泛 CASCADE。
3. 字节清理使用[资产协议](document-lifecycle.md#删除与历史引用)，精确身份、回执、失败重试独立于 DB；同步整项目与单文件路径。
4. 冲突同步 Problem/OpenAPI/client/三语，不把任意 DB 错误归为仍有 Run。未知先核对，不自动重删或建同 key 项目。

恢复归档与[备份恢复](../operations/backup-recovery.md)不同；修改 retention_days 不等于授权清理。

## 开发接续与验收

入口：[项目实现](../../PJM/backend/src/projectmind/projects/)、[ProjectsPage](../../PJM/web/src/pages/ProjectsPage.tsx)、[契约](../../PJM/README.md#contracts)。成员页复用账户查询和共用请求边界；项目元数据、归档/恢复、删除的统一版本与完整未知结果处理继续接续，不以成员页回归代替。

- 失效成员、ADMIN membership、跨组织分别授权，旧 Run 身份不变。
- 实 DB 验证成员加入/禁用、归档/Run/Schedule/删除竞争、版本冲突与审计回滚。
- 明确无效链接不换目标；无参数/空项目正常进入，切换和同 tick 双提交不消费旧结果。
- ARCHIVED 无 Run 但有 Schedule 明确拒绝，无部分删除；真空项目删除与附件失败保持真实语义。
- 三语、键盘、窄屏用真实组件验；不对用户项目执行删除探针，真实 DB 必须专用授权。
