# 用户生命周期与安全管理

本页负责账号创建、资料/角色/状态、本人改密、会话撤销与审计；[认证](authentication.md)和[登录防护](login-protection.md)分别定义凭据与配额。账户属于 Organization，不依赖 Project。交付缺口见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。

## 一个例子：停用再启用，不恢复旧登录

U 版本 7、会话 S1/S2 → ADMIN 停用事务保存 DISABLED/版本 8、撤销 S1/S2、追加审计 → 启用事务保存 ACTIVE/版本 9 与审计。旧会话仍撤销，必须重新登录 S3；改回角色同理。

最后一个活动 ADMIN 的停用在首笔事务内拒绝，不先停用再补建。会话撤销不证明已有 SSE/Run/外部进程停止。

## 工作副本与公开入口

| 层 | 现状与缺口 |
| --- | --- |
| Backend | [users service/repository](../../PJM/backend/src/projectmind/users/)有锁后认证、版本、末位 ADMIN、撤销/审计；测试多为 mock transaction |
| DB | [models](../../PJM/backend/src/projectmind/db/models.py)、[0032](../../PJM/backend/migrations/versions/0032_user_lifecycle.py)有用户版本/安全事件，不证明已部署迁移 |
| HTTP/契约 | [users route](../../PJM/backend/src/projectmind/api/routes/users.py)已接 10 个操作、[Schema](../../PJM/contracts/users/v1/)/example；保存的 OpenAPI 尚未同步 |
| Web | [users client](../../PJM/web/src/api/users.ts)/validator/barrel 已有；UserAccountPanel/UserSecurityEvents/useUserRequest/account 文案是未接入 App 的草稿，账户 route/page 未完成 |
| 初始化/关联 | bootstrap 已追加 CREATED；middleware 生成服务器 UUID，不信任 X-Request-ID |

接续现有实现，不新造平行服务或 client；文件存在不证明真实事务、可访问页面或部署验收。

## 场景与非目标

所有用户可读本人账户/安全事件、验当前密码后改密、撤销本人全部会话；ADMIN 管理本组织用户。创建不自动加入 Project，移除成员不等于停用账户，ADMIN membership 不决定组织权限。

不提供第三角色、匿名注册、找回密码/MFA、硬删账户、管理员代改密码或登录 email 变更。ADMIN 受控提交初始密码仅存 hash，不等于邀请/强制首次改密或公网恢复协议。

## 用户操作与目标公开面

以下已在工作副本 /api/v1 路由中，跨层交付尚未完成：

| 操作 | 入口 |
| --- | --- |
| 本人读取 | GET /users/me/account、/users/me/security-events |
| 本人安全变更 | POST /users/me/password、/users/me/sessions/revoke |
| ADMIN 列表/创建 | GET /users、POST /users |
| ADMIN 精确读取/修改 | GET /users/{user_id}、PUT /users/{user_id} |
| ADMIN 撤销/审计 | POST /users/{user_id}/sessions/revoke、GET /users/{user_id}/security-events |

### 版本、查询与公开数据

既有账户 mutation 均要求正整数 expected_row_version，先验版本再判 no-op；旧版即使值相同也冲突。同版无变化 PUT 不增版/事件，改密/撤销即使撤销零行也增版审计。

email 组织内标准化唯一，不等于创建幂等；重复 email 不证明原请求成功，不覆盖密码/角色。用户 q 最长 200，limit 1–100 默认 25，offset 非负；按 email/ID 排序，通配符作普通字符，响应 items/total/limit/offset。审计按目标分页，不用首页过滤冒充完整历史；offset 稳定排序不是跨页快照。

ADMIN 冲突后读精确 ID，保留原版本与非敏感草稿供比较，用户确认后再提交。本人走 me/account，USER 不能借管理读取他人。

账户 allowlist 仅 ID/email/display_name/role/status/version/时间；变更另报本次 revoked_sessions 和当前会话是否失效。撤销数包括未标记撤销的过期行，不是在线人数。安全事件不返回密码/token/hash/任意 metadata，响应/client no-store，不推广全站缓存保证。

### 改密入口的配额

来源检查先于正文 → 请求/Origin/会话 CSRF → actor 账号/组合配额 → 管理事务验密/更新。共享登录配额/退避，成功不清零，不发匿名 challenge、不接受另传账号。Redis 未确认则 429/503 拒绝，不进入昂贵验密/管理变更，不与 DB 画成同事务。

## 事务与并发

```text
入口认证事务结束
  → 管理事务锁 Organization → User（ID 升序）→ AuthSession（ID 升序）
  → 锁后重新验证原凭据/权限/期限、目标版本、活动 ADMIN
  → 账户 + 撤销 + 追加审计一起 commit 或 rollback
```

- 与 bootstrap 共用组织串行化点；actor/target User 去重排序。正常登录 User → AuthSession，不反向获取 Organization。
- 所需锁结束后取新时间，核对原 token/CSRF、期限、撤销、账户/角色；耗时验密 await 后、应用变更前再验期限。
- 授权判定点是持锁时最终检查，不承诺物理 commit 瞬间未过期；合法自撤销后不能再用“会话未撤销”否定自身。
- 降级/停用活动 ADMIN 在组织锁内确认仍有另一个活动 ADMIN；所有管理写入遵循同序，不能外推任意 SQL。
- 改密、角色/状态变化、主动撤销使目标所有未撤销会话持久失效；只改 display_name 不撤销。User 锁与新登录协调，避免漏撤销。
- 新用户先 flush 父行再插审计，flush 不是 commit；任何审计失败整体回滚。组织锁保守串行化，优化须有实 DB 数据。

## 审计与请求关联

row_version 仅账户/安全链路，不随偏好、语言或登录时间变。UserSecurityEvent 保存 actor/target、CREATED/UPDATED/PASSWORD_CHANGED/SESSIONS_REVOKED、版本、前后角色/状态、撤销数、时间/UUID；每目标版本最多一条，不是全部字段差异历史。

服务器生成关联 UUID，header/Problem/审计一致；不沿用客户端 ID。request ID 不是幂等键，重发产生新 ID；拒绝/no-op 未必有事件，查不到不能证明请求未到达。

bootstrap 用新 ADMIN 自身作 actor、CLI UUID 作关联，同事务保存；任何 ADMIN（含 DISABLED）存在均拒绝，不是恢复通道。追加式是用例约束，FK/唯一键不证明 DB 已禁所有 UPDATE/DELETE，不开放普通删改审计 API。

## 生效与界面

账户入口独立于 Project，本人安全与 ADMIN 管理分开；隐藏按钮不代替授权。

| 返回 | 处理 |
| --- | --- |
| 成功且当前会话已撤销、401 失效 | 清本地身份/密码，要求重登；不额外 logout，不自动重放原动作 |
| 400 current_password_rejected | 密码错误，保留有效登录态 |
| 403/404 | CSRF/ADMIN 权限与目标不可访问分别提示，不切目标/提权/重试 |
| 409 user_version_conflict | 读精确账户，保留草稿，由用户比较后采用新版本 |
| 409 last_active_admin / user_email_conflict | 保留末位 ADMIN / 不覆盖原账户；刷新版本不能自动解决 |
| 422 | 修参数，不展示可能含密码的原始校验输入 |
| 429/503 | 有限等待或不可用，不自动发密码 |
| timeout/断连/abort | 结果未知，核对当前身份、原目标/版本及授权审计，不推断回滚 |

当前账户/新版本本身不证明原操作造成，无专用重放时不承诺 exactly-once。表单同步 ref 防双提交，换 actor/Project、卸载后丢弃旧结果；密码/token 不进 URL/storage/日志，email 搜索不入遥测，三语/键盘/窄屏均覆盖。

## 迁移与历史兼容

0032 给旧用户 row_version=1，不补造 CREATED；有任意安全事件拒绝 downgrade 丢表，不删事件凑条件。0031 会话协议是另一迁移，按[发布审查](../operations/deployment.md#迁移与回退审查)分别验收。

备份可能恢复旧角色/密码/会话，按[恢复后验证](../operations/backup-recovery.md#恢复后验证)重核安全事实，不提供未经审查的 SQL 清表/撤权。

## 开发接续与验收

先合跑 users/API/contracts，核对错误与 Schema 后用 exporter 同步 OpenAPI；复用 client/组件草稿接平台入口、分页、版本表单与三语。最后在专用授权环境验证：

- 双 ADMIN 互停用/降级、bootstrap、登录/撤销竞争，末位保护与完整回滚。
- 锁等待过期/撤权、角色改回、停用后启用，旧会话不复活。
- 旧版本/no-op/重复 email/审计失败，冲突无部分变更，唯一键不冒充幂等。
- 改密错误/配额/Redis 不可用，拒绝阶段准确、不误注销；可信 UUID 与 DTO 无泄漏。
- 实组件的未知、同 tick 双提交和换 actor；真实双浏览器/HTTPS/DB 另验。
- 0031/0032 升降级与备份恢复，不伪造旧审计；mock 或离线文档检查不替代真实事务。
