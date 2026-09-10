# 用户生命周期与安全管理

本页负责组织账号的创建、资料/角色/状态、本人改密、会话撤销与审计，不依赖 Project。凭据和配额见[认证](authentication.md)、[登录防护](login-protection.md)，缺口见[计划 R05](../planning/roadmap.md#开发任务)。

## 一个例子：停用再启用，不恢复旧登录

U 版本 7、会话 S1/S2 → 停用事务保存 DISABLED/版本 8、撤销 S1/S2、追加审计 → 启用事务保存 ACTIVE/版本 9 与审计。旧会话仍须重新登录，改回角色同理。最后一个活动 ADMIN 在停用事务内拒绝；撤销不证明 SSE/Run/外部进程已停止。

## 工作副本与公开入口

复用 [users service/repository](../../SKM/backend/src/skillmind/users/)、[users route](../../SKM/backend/src/skillmind/api/routes/users.py)、[Schema](../../SKM/contracts/users/v1/)及[账户页](../../SKM/web/src/pages/AccountsPage.tsx)。页面经 #/accounts 接入 App，使用现有 client、版本表单和请求 hook，不新造平行服务。[0032](../../SKM/backend/migrations/versions/0032_user_lifecycle.py)定义版本/安全事件；文件存在不等于迁移部署或真实事务验收。

## 场景与非目标

用户可读本人账户/安全事件，验当前密码后改密、撤销本人全部会话；ADMIN 管理本组织用户。创建不自动加入 Project，移除成员不等于停用账户，ADMIN 不依赖 membership。

不提供第三角色、匿名注册、找回密码/MFA、硬删账户、管理员代改密码或 email 变更。ADMIN 提交初始密码只存 hash，不等于邀请、强制首次改密或公网恢复协议。

## 用户操作与目标公开面

路由统一位于 /api/v1：

| 操作 | 入口 |
| --- | --- |
| 本人读取 | GET /users/me/account、/users/me/security-events |
| 本人安全变更 | POST /users/me/password、/users/me/sessions/revoke |
| ADMIN 列表/创建 | GET /users、POST /users |
| ADMIN 精确读取/修改 | GET /users/{user_id}、PUT /users/{user_id} |
| ADMIN 撤销/审计 | POST /users/{user_id}/sessions/revoke、GET /users/{user_id}/security-events |

### 版本、查询与公开数据

- 既有账户 mutation 必带正整数 expected_row_version，先验版本再判 no-op；旧版即使值相同也冲突。同版无变化 PUT 不增版/事件，改密/撤销即使撤销零行也增版审计。
- 标准化 email 组织内唯一，但不构成创建幂等；重复 email 不证明原请求成功，不覆盖密码/角色。
- 用户查询 q 最长 200，limit 1–100（默认 25）、offset 非负，按 email/ID 排序，通配符作普通字符；返回 items/total/limit/offset。审计按目标分页，不能过滤首页冒充历史；offset 排序不保证跨页快照。
- 冲突后 ADMIN 读精确 ID、本人读 me/account，保留原版本和非敏感草稿供人工比较；USER 不借管理读取他人。
- 账户仅公开 ID/email/display_name/role/status/version/时间；mutation 另报本次 revoked_sessions、当前会话是否失效。撤销数含未标记撤销的过期行，不是在线人数。安全事件不返密码/token/hash/任意 metadata；响应/client no-store，不外推全站。

### 改密入口的配额

来源 gate → 请求/Origin/会话 CSRF → actor 账号/组合 gate → 管理事务验密。共享登录配额且成功不清零，不用匿名 challenge/另传账号。429/503 拒绝不进入验密或账户变更，Redis 与 DB 不属同一事务；计数细节见[登录防护](login-protection.md#保护范围与执行顺序)。

## 事务与并发

```text
入口认证事务结束
  → Organization → User（actor/target 去重、ID 升序）
  → AuthSession（ID 升序）
  → 原凭据/权限/期限、目标版本、活动 ADMIN
  → 账户 + 会话撤销 + 审计一并 commit / rollback
```

- 与 bootstrap 共用 Organization 串行化点；登录只按 User → AuthSession，不反向取组织锁。
- 锁后及耗时验密 await 后、应用变更前取新时间复核原 token/CSRF、期限、撤销、账户/角色。最终持锁判定不承诺物理 commit 瞬间未过期；合法自撤销不能再被“会话已撤销”否定。
- 停用/降级活动 ADMIN 须在组织锁内确认另有活动 ADMIN。全部管理 writer 同序；不能外推任意 SQL。
- 改密、角色/状态变化、主动撤销持久撤销目标全部未撤销会话；仅改 display_name 不撤销。User 锁与新登录协调，防止漏撤销。
- 新用户先 flush 父行再插审计，flush 不等于 commit；审计失败整体回滚。组织锁优化须有真实 DB 依据。

## 审计与请求关联

row_version 仅覆盖账户/安全变更，不随偏好、语言或登录时间变化。UserSecurityEvent 保存 actor/target、操作类型、版本、前后角色/状态、撤销数、时间/UUID；每目标版本最多一条，不是全字段差异历史。操作类型为 CREATED/UPDATED/PASSWORD_CHANGED/SESSIONS_REVOKED。

服务器生成 UUID 并用于 header/Problem/审计，不信任客户端 X-Request-ID。它不是幂等键，重发换 ID；拒绝/no-op 未必有事件，查不到不证明请求没到达。

bootstrap 以新 ADMIN 自身和 CLI UUID 同事务追加 CREATED；已有任意 ADMIN（含 DISABLED）即拒绝。审计追加式是用例约束，FK/唯一键不保证 DB 禁止任意 UPDATE/DELETE，不开放普通删改 API。

## 生效与界面

本人安全与 ADMIN 管理分开，无 Project 或列表加载时仍可访问。首次项目读取不清草稿，用户实际换项目时隔离旧表单；隐藏按钮不代替授权。

| 返回 | 处理 |
| --- | --- |
| 当前会话撤销成功 / 401 失效 | 清身份与密码、要求重登；不额外 logout 或自动重放 |
| 400 current_password_rejected | 保留有效登录态 |
| 403/404 | 区分 CSRF/ADMIN 拒绝与目标不可访问，不换目标或重试 |
| 409 user_version_conflict | 精确重读，人工比较草稿后采用新版本 |
| 409 last_active_admin / user_email_conflict | 保留末位 ADMIN / 原账户；刷新版本不自动解决 |
| 422 | 修参数，不展示可能含密码的原始校验输入 |
| 429/503 | 有限等待或不可用，不自动发密码 |
| timeout/断连/abort | 结果未知；核对当前身份、原目标/版本和授权审计，不推断回滚 |

当前值或新版本不证明原操作造成，无专用重放协议不承诺 exactly-once。同步 ref 防双提交；换 actor/Project、卸载后丢弃旧结果。密码/token 不进 URL/storage/日志，email 搜索不入遥测。

## 迁移与历史兼容

0032 将旧用户初始化为 row_version=1，不补造 CREATED；任意安全事件阻止 downgrade 丢表，不删事件凑条件。0031 会话迁移按[发布审查](../operations/deployment.md#迁移与回退审查)独立验收。

备份可恢复旧角色/密码/会话，按[恢复后验证](../operations/backup-recovery.md#恢复后验证)重核，不用未经审查 SQL 清表/撤权。

## 开发接续与验收

合跑 users/API/contracts 并核对错误、Schema/OpenAPI 与账户页。重点覆盖双 ADMIN/末位保护、bootstrap/登录/撤销竞争、锁等待过期和旧会话不复活、版本/no-op/email 冲突、审计失败回滚、改密配额/错误不误注销、UUID/DTO 无泄漏。

实组件验证未知、同 tick 防重、换 actor、三语/键盘/窄屏；真实 DB、双浏览器/HTTPS、0031/0032 及备份恢复分别举证，见[本地验证](../development/local-development.md#ブラウザ回帰)。
