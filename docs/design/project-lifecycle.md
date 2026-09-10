# 项目、成员与归档边界

本页定义项目身份、成员、选择、归档与删除。账户、执行停止、资产清理分别见[认证](authentication.md)、[执行监督](run-supervision.md)、[文档资产](document-lifecycle.md)；缺口集中在[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。

## 一个例子：归档不是停止或删除

归档 P 后，默认活动列表不再展示，新建 Run/回答/批准被拒绝；授权历史仍可读，Run、成员、Schedule、附件不会一并停止或删除。恢复保留原 ID/key/配置，不恢复已移除成员或 ERROR Schedule，不补跑错过触发。停止、移除成员、归档与物理删除是独立操作。

## 项目身份与成员资格

| 对象 | 边界 |
| --- | --- |
| Project | organization_id 隔离；project_id 是身份，组织内 key 唯一，归档仍占 key |
| ProjectMember | ACTIVE/REMOVED 关系，无 Project ADMIN/OWNER 角色 |
| User | ACTIVE/DISABLED、ADMIN/USER；建账户不自动入项目 |
| ProjectSkillVersion | 启用精确版本，不授予成员或系统权限 |

本组织 ADMIN 不依赖 membership，移除其关系不撤销 ADMIN 权限；跨组织仍拒绝。USER 后续请求重验账户/会话与 ACTIVE 关系，不改旧 Run 身份/权限/结果，也不保证已有 SSE 立即结束。

### 成员管理的现状与目标

ADMIN 可列出、添加同组织 ACTIVE 用户、移除成员。列表含 ACTIVE/REMOVED，无分页和独立账户状态；membership ACTIVE 不能显示为账户已启用，响应仅公开白名单并 no-store。

重新添加 REMOVED 复用行、刷新 joined_at；重复添加 ACTIVE 返回原行且不追加事件，重复移除为 404。允许移除已停用用户关系，不改变账户。

写入锁序为 Organization → User（actor/target 去重排序）→ 原 AuthSession → Project → ProjectMember。锁后及 flush 后取新时间复核原会话、ADMIN/CSRF，持锁检查目标 ACTIVE，锁保持到提交；凭据校验复用[用户事务](user-lifecycle.md#事务与并发)。偏好写入、项目删除也先取同一组织 gate，避免 User/Project 反向锁环。

关系变更与 project_member_events 原子提交：组织/项目/关系/目标/actor、ADDED/REMOVED、前后 status/joined_at、服务器 request UUID/时间；不存密码或任意正文。0033 不回填历史，任意事件阻止降级丢表。request UUID 不是幂等键；尚无成员审计查询 API，当前关系不证明原操作结果。

### 成员管理页面

仅 ADMIN 的成员页签管理精确授权项目；归档可读/移除，不可添加。候选复用组织账户服务端搜索/分页并显示角色/状态，不过滤首页冒充全量；成员另显关系状态及 ADMIN 例外。

每次写入确认原项目/用户并同步防重。超时、断连、abort、损坏成功响应均为未知，不重放、不以当前关系符合目标推断成功。用户重读原成员列表和精确账户均成功后，才可人工解除新写门禁，解除不执行原动作。

页签切换或同项目资格刷新保留未知；资格重验暂停交互并把在途写中断视为未知。换 actor/项目或离页隔离旧请求，不承诺跨刷新恢复。

## 项目选择与失效链接

preference 仅记录上次选择，不是授权。服务端返回当前可访问的 ACTIVE preference，否则 null；旧引用可保留，删项目才显式清理。

| 进入方式 | 要求 |
| --- | --- |
| URL 无项目 | 首次选有效 preference/活动首项；空项目保留平台入口 |
| URL 明确无效/无权项目 | 保留原目标、统一不可访问，不静默换项目 |
| 主动切换/资格失效 | 隔离草稿、阻止新提交、丢弃旧响应，不迁移未知写入 |

[App](../../SKM/web/src/App.tsx)经 [useProjectContext](../../SKM/web/src/hooks/useProjectContext.ts)独立读列表、偏好、精确详情。空值/重复参数/非法 UUID 不算“未指定”；目标一旦确定，列表变化、归档或重读不换目标。

每次进入页面/切换/重读均有独立请求身份和等待上限，详情确认前不挂载业务页；返回曾访问 ID 不复用旧授权。主动切换清旧 Run/Task 参数，只有确认 ACTIVE 才存偏好。

列表、偏好与详情失败独立呈现：详情成功不隐藏列表失败，偏好失败不丢列表，授权归档仍可读。前端检查不替代逐请求服务端授权，也不承诺实时推送撤权。

## 归档的实际边界

归档/恢复保留配置；同版同状态不增版/时间，不调用 Run/Schedule/Integration/Session 停止服务。

| 入口 | 边界与专题 |
| --- | --- |
| 列表/preference、历史 | 默认 ACTIVE；include_archived 及精确详情可读授权归档，活动选择器不是完整审计入口 |
| ProjectWriteActor | 409 project_archived：创建、回答、评价、批准、Schedule 修改 |
| Run 创建/确认 | [业务事务](run-creation.md#创建与确认的授权事务)重验原会话、当前 Project/成员，已有同键也不绕过归档 |
| Run 取消 | WriteActor + Run 项目访问，不要求 ACTIVE；受理不证明停止 |
| ADMIN 维护 | 编辑、归档/恢复、删除、移除成员独立用例；新增成员要求 ACTIVE，否则 404 |
| Schedule | [保存](task-scheduling.md#管理写入的授权事务)及发火/原 Run 关联均在各自事务复核当前资格；归档不立即改成 PAUSED |
| 组合 | [保存/解绑](skill-contract.md#共享更新与删除的区别)要求原 ADMIN/ACTIVE；不等于全局删除或替其他项目启用版本 |
| 文档 | [上传/删除](document-lifecycle.md)有原资格、占用和清理协议；原上传查询允许授权归档读取，清理/结算未完成 |

### 并发修改不能只看有无行锁

项目 CRUD 的 DB/API/Web 使用同一版本协议：

- row_version 为 1–2147483647，仅覆盖 metadata/归档，不覆盖成员、偏好、资源/Run，也不是审计。
- PATCH 必带原 expected_row_version 和至少一个非 null 可修改字段；归档/恢复 POST 正文、DELETE query 均必带版本。缺失/非法为 422，不补当前值。
- 创建/修改/归档/恢复/删除按 Organization → User → 原 AuthSession → Project，锁后及 flush 后取新时间复核原 ADMIN/CSRF，持锁至提交。
- 先验版本再判 no-op/删除前置。旧版同值也返回 409 project_version_conflict；同版无变化不增版/时间，有变化只增一次。上限后有变化为 409 project_version_exhausted，不回绕。
- 列表/详情必带版本并 no-store；key 冲突不是创建幂等，删除 204、越权/不存在 404。版本通过不免除归档、引用或审计门禁。

页面固定原 ID/key/版本与非敏感草稿，同步防重。冲突后精确重读，展示原值/草稿/当前值；人工采用新版本后须另行提交，不能自动改版重放。

未知先核对原目标：既有项目读精确 ID，创建读含归档的完整列表并比较原 key。404/无匹配只说明当前未发现，目标值相符也不证明原请求成功。成功读取或精确 project_not_found 404 后，用户可人工解除新写门禁；解除不重发，删除未知不按同 key 重建。

页签/列表刷新不消除未知，换 actor/项目或离页不迁移原动作，不承诺跨刷新恢复。首次无参数默认选择不算人工切换，不清创建草稿/确认/未知。独立 metadata/归档审计载体尚未完整定义，不伪造历史或削弱现有审计保护。

0034 给旧项目版本 1，不代表旧修改次数。新旧 API/Web 不混跑；降级前全实例停写并停止旧页面/在途请求，任意版本 > 1 拒绝丢列。

## 删除与数据保留

仅当前 ADMIN 按上述事务先验原版本，再检查全部前置；拒绝必须先于任何关系删除。通过后清 preference 和列举配置，删除项目返回 204。

| 阻止条件 | Problem code |
| --- | --- |
| 未归档 | project_delete_requires_archive |
| 任意 Run | project_delete_blocked_by_runs |
| 任意状态/发火/认领阶段 Schedule | project_delete_blocked_by_schedules |
| 成员变更审计 | project_delete_blocked_by_member_audit |
| 文档、上传意图或清理记录，含元数据已删 | project_delete_blocked_by_document_uploads |

默认保留归档项目；这不是回收站，retention_days 只是配置，不自动授权清理。已有检查仍需补全冻结/在途引用与并发验证；持久清理尚无可靠结算，不能删要求/占用换取项目可删。

### 删除门禁的修正要求

1. 物理删除须证明无执行、审计、冻结输入和在途引用；暂停/归档/未触发 Schedule 也保护，不删审计释放 key。
2. 新增引用、旧 Worker/恢复共用锁或条件提交，防检查后新增；关系删除同事务回滚，不拆 RESTRICT 或扩大 CASCADE。
3. 新旧文档均走[单文件资产协议](document-lifecycle.md#删除与历史引用)，精确字节清理/回执/恢复独立于 DB；整项目不清空占用。
4. 冲突同步 Problem/OpenAPI/client/三语，不把任意 DB 异常归为“有 Run”；未知不自动重删或重建。

归档恢复与[备份恢复](../operations/backup-recovery.md)不同。

## 开发接续与验收

入口：[项目实现](../../SKM/backend/src/skillmind/projects/)、[ProjectsPage](../../SKM/web/src/pages/ProjectsPage.tsx)、[契约](../../SKM/README.md#contracts)。验证成员/账户与 ADMIN 例外、跨组织、版本/未知、精确链接/切换/防重，以及归档/创建/调度/删除竞争和审计回滚。

实组件覆盖三语/键盘/窄屏；真实事务、完整引用与字节清理另验，module 配置不由 CRUD 回归证明。删除探针只用于获准专用目标。
