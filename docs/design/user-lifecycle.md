# 用户生命周期与安全管理

> 定位：账号创建、资料/角色/状态变更、本人改密、持久撤销与安全审计的设计正本。Backend 路由和 Schema 已有工作副本，OpenAPI 快照与 Web 尚未接齐；本文不能作为部署环境的操作保证。当前接线与验证状态见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。

前置阅读：[认证与会话](authentication.md)定义当前身份、Origin/CSRF 和期限；[登录入口防护](login-protection.md)定义密码验证前的配额。账户不属于某个 Project，外部 Provider 凭据另由[Secret 设计](secret-storage.md)负责。

[看具体例子](#一个例子停用再启用不恢复旧登录) · [分清已有与目标](#工作副本与公开入口) · [操作边界](#用户操作与目标公开面) · [事务](#事务与并发) · [失败与界面](#生效与界面) · [验收](#开发接续与验收)

## 一个例子：停用再启用，不恢复旧登录

用户 U 在两个浏览器登录，已有会话 S1、S2。ADMIN 停用 U，稍后又重新启用：

```text
初始：U ACTIVE，版本 7
      S1 / S2 未撤销
    ↓ ADMIN 按版本 7 停用
事务一：U DISABLED，版本 8
        撤销 S1 / S2
        追加审计
    ↓ ADMIN 读版本 8 后启用
事务二：U ACTIVE，版本 9
        追加审计
    ↓
S1 / S2 保持撤销
U 重新登录 → 新会话 S3
```

此例说明目标行为，不表示现有页面已提供停用按钮。只改 User.status 再改回来，无法记住中间发生过的撤权；角色改回原值也有同样问题。新会话不清除旧 revoked_at，账户版本不等于浏览器会话版本。

若 U 是最后一个活动 ADMIN，停用必须在第一笔事务内拒绝，不先停用再补建管理员。会话撤销与[Run 停止](run-supervision.md)是两条链路；例子不能证明在途 SSE、模型或外部进程已立即停止。

## 工作副本与公开入口

以下是 2026-09-09 续整时核对到的调用关系。先辨认断点，再进入目标规则；测试、发布和部署状态统一在计划登记。

```text
工作副本 Backend（10 个路由操作）：
  HTTP 路由 → UserService
                 ↓
             repository
                 ↓
          账户 / 会话 / 审计

交付面：响应模型 + Schema/example
  ├─ OpenAPI 快照：待同步
  ├─ Web client / validator：已有工作副本
  └─ 账户页面 / route / 三语交互：待接入
```

| 层次 | 已找到的载体与限制 |
| --- | --- |
| 内部用例 | [users/domain.py](../../PJM/backend/src/projectmind/users/domain.py)、[service](../../PJM/backend/src/projectmind/users/service.py)、[repository](../../PJM/backend/src/projectmind/users/repository.py)提供 DTO、锁后认证、版本检查、末位 ADMIN 保护与撤销/审计写入；[专属测试](../../PJM/backend/tests/users/test_user_service.py)使用 mock transaction，不证明真实回滚/竞争 |
| 持久化 | [models](../../PJM/backend/src/projectmind/db/models.py)与[0032 migration](../../PJM/backend/migrations/versions/0032_user_lifecycle.py)定义用户版本和安全事件；未据此执行迁移或声称已部署 |
| 密码入口 | [API middleware](../../PJM/backend/src/projectmind/api/main.py)已把本人改密路径纳入正文前的来源检查；route 再调用 `AuthService.admit_password_change` 检查 actor 的账号/组合配额。登录 challenge 不用于改密 |
| HTTP 与契约 | API startup 已装配 UserService；[users route](../../PJM/backend/src/projectmind/api/routes/users.py)保留 preference / UI language，工作副本有下表的 10 个操作。[管理 Schema/example](../../PJM/contracts/README.md#ユーザー管理の公開面を準備する)与专属 HTTP 测试已有文件，保存的 OpenAPI 尚未同步 |
| Web 消费 | [users client](../../PJM/web/src/api/users.ts)与 barrel 已覆盖下表 10 个操作，含响应校验、目标 ID、分页、CSRF/no-store 和 await 前后 abort 检查；[专属测试](../../PJM/web/tests/api/users.test.ts)使用 mock fetch。账户 route/page 与三语交互尚未接入；画面仍须验证当前 actor/请求及本人响应的目标。接续位置见 [Web README](../../PJM/web/README.md#アカウント管理を接続する) |
| 初始化与追踪 | [bootstrap](../../PJM/backend/src/projectmind/auth/bootstrap.py)已写入首个 ADMIN 的 CREATED 事件；middleware 每次生成新的 UUID，不沿用客户端 X-Request-ID。调用已存在，不据此宣称 CLI、真实数据库或代理已验收 |

工作区另有未接入 App 的 UserAccountPanel / UserSecurityEvents、useUserRequest 与 account 文案草稿，入口见上表 Web README；本轮文档整理保留这些文件，没有验证或继续实现它们。组件存在不等于账户页面已可访问，也不应被当作完全不存在而重复创建。

按精确 ID 读取单个用户及专属测试现已存在，旧 API fake 的调用也已修正；[§84 的局部核对](../history/delivery-history.md#841-只读核对与设计纠偏)记录了当时复跑结果，不把客户端验证或 HTTP 替身延伸为账户页面、真实数据库或部署已验收。

实现者应接续这些部件，不新造平行用户服务。不能因快照缺失而称为“没有路由”，也不能因路由可生成 OpenAPI 而称为“契约已交付”。完整的接续顺序见[开发与验收](#开发接续与验收)，当前失败与证据见[工作登记](../planning/roadmap.md#r05-领域与身份安全)。

## 场景与非目标

所有用户可以查看本人账户与安全事件、验证当前密码后改密、撤销本人全部会话；ADMIN 另可管理本组织用户。创建用户不自动创建 ProjectMember，也不因为前端选中了某个 Project 而改变组织范围。[成员关系](project-lifecycle.md#项目身份与成员资格)的移除/恢复是独立操作；移除 ADMIN 的 membership 不会撤销其组织权限。

本链路不增加第三种系统角色、匿名注册、密码找回、邮件验证、MFA、账号硬删除或 ADMIN 代用户改密。初始密码由获准 ADMIN 在受控表单中提交，只写入 hash；这不是邀请/强制首次改密流程，也不能当作公网账号恢复方案。更强的身份恢复与交付方式须另行设计。

## 用户操作与目标公开面

以下操作已进入工作副本的 `/api/v1` 路由，说明它们应保持的设计责任；尚未完成跨层交付。标题保留旧锚点，精确请求/响应从[契约文件映射](../../PJM/contracts/README.md#ユーザー管理の公開面を準備する)进入，不把内部 DTO 自动序列化为 API。

| 操作 | 入口与权限 |
| --- | --- |
| 本人资料与安全历史 | GET /users/me/account；GET /users/me/security-events。只能读取当前 user |
| 本人改密 | POST /users/me/password。当前密码 + 新密码策略；撤销本人全部旧会话，包括当前会话 |
| 本人撤销全部会话 | POST /users/me/sessions/revoke。成功后重新登录，不自动补发密码 |
| ADMIN 查询与创建 | GET /users；POST /users。组织内搜索、分页；新用户为 ACTIVE，明确选择 ADMIN 或 USER |
| ADMIN 读取单个账户 | GET /users/{user_id}。在同一组织/锁后认证边界读取精确 ID，供编辑前和冲突后确认；复用 account 响应，不从列表首页猜测目标已不存在 |
| ADMIN 修改资料/状态/角色 | PUT /users/{user_id}。仅 display_name、status、system_role；本链路不更改登录 email |
| ADMIN 撤销与审计 | POST /users/{user_id}/sessions/revoke；GET /users/{user_id}/security-events。不存在与跨组织目标同为 404 |

### 版本、查询与公开数据

- 变更既有账户均携带正整数 expected_row_version，包括改密和撤销。先检查版本，再判断是否实际变化；旧版本即使碰巧与当前资料相同也返回冲突，不能自动换版本重发。
- 资料完全相同且版本匹配的 PUT 是 no-op，不新增版本/事件。改密和主动撤销是安全操作，接受一次就增加版本并记录；“撤销数量为零”不代表无需审计。
- 创建依赖组织内标准化 email 的唯一性，不重复创建账号；唯一性不是完整的请求幂等回放协议。重复 email 不能证明刚才的请求成功，也不能静默覆盖已有密码/角色。
- 用户列表按组织在服务端搜索 email/display_name。现有 route 接受 q（最多 200 字符）、limit（1–100，默认 25）、非负 offset；repository 按 email、ID 排序，并将查询中的通配符视为普通字符。响应为 items、total、limit、offset。审计按目标用户分页，不能用第一页前端过滤冒充完整历史。

稳定排序不等于跨请求的数据库快照。翻页之间可能有新增账户或审计事件，Web 应保留查询条件并允许显式刷新；不能把 total 当作“整个浏览期间不变的总数”，也不承诺 offset 分页绝不重复或遗漏。需要一致性导出时另定义游标/快照协议，不复用管理列表作审计导出证明。

单账户读取是管理列表的精确查询，不授予额外权限。编辑页保留用户非敏感草稿与原版本，显式读取最新账户后并列比较；用户确认采用新版本才可再次提交，不因刷新成功自动覆盖草稿或重放旧动作。本人仍使用 /users/me/account，普通 USER 不能借管理读取访问其他账户。

公开账户只允许 user ID、email、display name、role、status、row_version 和创建/更新时间。变更结果另说明本次新标记的 revoked_sessions 数量及调用者当前会话是否失效。该数量包含尚未标记撤销的过期行，不是“在线人数”或“已关闭浏览器数”。

安全事件只返回允许公开的操作事实，不含密码、token、hash 或任意 metadata。账户响应与已处理错误使用 no-store；当前 account router 复用 auth tag，改密路径还受正文前 middleware 保护。客户端也须禁止缓存敏感数据，不把该实现推广为未匹配路径、未处理异常或代理自产错误的全站保证。

### 改密入口的配额

当前接线顺序为：来源检查 → 请求与 Origin/会话 CSRF → 从当前 actor 取得账号并检查账号/组合配额 → 管理事务内验证当前密码与修改。来源在正文校验前计数，账号不能由另传 email 覆盖；在昂贵密码计算和管理行锁前确认配额。入口会话认证本身可能取得行锁，与后面的管理事务分开。

复用登录的同一配额与有限退避，不另发匿名 challenge；成功不清零。取舍是登录尝试与改密共享额度，可能互相限流，但不能通过切换入口绕过账号保护。Redis 不可用则拒绝本次改密，不补偿扣减，也不把 Redis 与用户事务描绘成原子提交。429/503 沿用[公开防护错误](login-protection.md#公开响应与客户端责任)；HTTP 替身验证的拒绝顺序仍需与真实 Redis/事务分别联验。

## 事务与并发

一个管理操作只有一个用例事务。路由仍先通过统一 actor dependency，但提交链路重新验证原会话，而不是仅信任入口时的 AuthenticatedActor。

```text
入口认证（独立事务）
    ↓
管理事务依次取锁：
  Organization
    → User（ID 升序）
    → AuthSession（ID 升序）
    ↓
锁后重验：原凭据、权限、期限
检查：目标版本、活动 ADMIN
    ↓
账户变更 + 撤销会话 + 审计
    ↓
一起提交或回滚
```

1. 与 bootstrap 共用 Organization 行作为管理串行化点，操作者与目标 User 去重后按 ID 升序锁定；然后锁当前会话和所需目标会话。正常登录/认证只走 User → AuthSession，不在持有用户锁后反向取得组织锁。
2. 全部所需锁等待结束后取新时间，核对原 token/CSRF、期限、revoked_at、用户状态和登录角色快照；管理员操作另核对当前 ADMIN。密码计算等耗时 await 后、应用变更前再次检查期限。
3. 明确授权判定点：在原行锁仍受本事务持有时作上述最终校验。不能用事务开始时间，也不宣称保证物理 commit 完成瞬间尚未过期；后续请求仍独立认证。不可在主动撤销自身后再次按“当前会话须未撤销”校验而否定自己的合法操作。
4. 降级或停用活动 ADMIN 前，在组织锁内确认另有活动 ADMIN。串行化让两个 ADMIN 互相停用/降级时不能同时通过；这一保证依赖所有管理写入遵循同一锁顺序，不能外推到任意直接 SQL。
5. 改密、角色/状态变化和主动撤销将目标所有尚未撤销会话写为 revoked_at；只改 display_name 不要求撤销。目标 User 锁还要与新登录的写入顺序一致，防止并发生成会话漏过撤销；真实竞争须专项验证。
6. User 版本、会话失效和追加事件原子保存。新用户须先 flush 父记录，再插入引用它的事件；flush 不是提前 commit。审计写入失败时不能只保留账号变更。

组织级锁是当前受控内部部署的保守取舍，简单但会串行化该组织的管理操作；现有内部读用例也经过它。后续性能优化须以实 DB 锁等待/并发数据为依据，不能仅删除锁或把最后 ADMIN 检查移到事务外。

## 审计与请求关联

User.row_version 只覆盖本链路的账户/安全变更，不随 UI language、Project preference 或 last_login_at 递增。它不证明 User 所有字段都受同一乐观版本管理。

UserSecurityEvent 记录 actor/target、动作、变更后版本、前后角色/状态、撤销数量、时间和关联 UUID。当前内部动作有 CREATED、UPDATED、PASSWORD_CHANGED、SESSIONS_REVOKED；这是安全操作记录，不是包含旧密码、旧 display_name 等全部字段的差异历史。每个目标用户与版本最多一条事件。

关联标识由服务器生成 UUID：当前 HTTP middleware 不采纳客户端 X-Request-ID，响应 header 与 Problem 使用本次服务器 ID，route 把同一值交给审计用例。客户端即使发送合法 UUID 也不能指定该身份；排障使用收到的响应 ID。UUID 类型校验本身只能验证格式，不能证明来源可信。

request ID 不是 Idempotency-Key。重复发送得到新的 request ID，不恢复原决定；服务器确已接受的安全操作才有事件。校验/权限拒绝和 no-op 不保证存在对应审计行，不能因“查不到事件”断言网络失败的请求从未到达。

bootstrap 是唯一无需已有 actor 的首 ADMIN 创建路径。现有用例以新建 ADMIN 自身作为 actor、CLI 生成的操作 UUID 作关联，先保存父 User 再追加 CREATED；两者须同事务失败回滚，不向 CLI 伪造 HTTP 请求。bootstrap 发现任何 ADMIN（包括 DISABLED）就拒绝，不是失联/停用管理员的恢复通道，也不在每次 API 启动时执行。

“追加式”是服务用例的约束；现有 FK/唯一键及 downgrade guard 不等于数据库已阻止任何 UPDATE/DELETE。上线审查仍需检查所有写入者与保留策略；不能向普通管理 API 提供修改/删除审计入口。

## 生效与界面

账户入口属于平台，不依赖 Project 是否存在或当前是否可访问。所有用户看到本人安全操作，ADMIN 才看到组织用户列表、创建、版本化编辑和审计；前端隐藏按钮不替代服务器授权。

| 收到的事实 | 界面应如何处理 |
| --- | --- |
| 成功且当前会话已撤销 | 清除当前本地登录态与密码，要求重新登录；不在 cleanup 再调用 logout，不自动发送新密码 |
| 401 会话失效 | 退出当前认证态；不能把原管理操作自动重放到下次登录的 actor |
| 400 当前密码错误 | `current_password_rejected`：显示操作拒绝，不清除仍有效的登录态，不泛化成 401 |
| 403 / 404 | `csrf_rejected` / `administrator_required` 与 `user_not_found` 分开；不自动提权、切换目标或重试 |
| 409 账户版本变化 | `user_version_conflict`：重新读取账户，保留非敏感草稿供人工比较；不自动更新 `expected_row_version` 后重发 |
| 409 末位 ADMIN | `last_active_admin`：必须保留最后一个活动 ADMIN，单纯刷新版本不能解决 |
| 409 重复 email | `user_email_conflict`：不覆盖原账户，也不证明原创建成功；不能换版本盲目重发 |
| 422 请求不合法 | `validation_error` 与 `invalid_user_request` 都是请求拒绝；修正参数，不展示含密码的原请求或异常输入 |
| 429 / 503 | 遵守有限等待或不可用提示，不反复验证密码，不将防护故障表示为密码错误 |
| 断连、timeout、abort | 结果未知，不表示已回滚；确认当前身份后读取账户/授权范围内的审计，保留不含密码的目标与原版本供人工核对 |

响应丢失后，一条“当前账号存在”或较新的版本不足以证明是原操作造成。确认不了就保留未知结论，由用户在最新事实基础上明确发起后续动作；无专用重放契约时不承诺 exactly-once。重新登录本身不等于重新提交刚才的操作。

表单以在途 ref 防止同一 tick 双提交；离页、unmount 或 actor 变化时中止消费晚到响应。密码与 token 不进入 URL/storage/日志；用户列表的 email 查询也按个人信息处理，不复制到遥测。成功、拒绝与未知均同步三语、键盘焦点和窄屏布局。

撤销提交后，后续认证拒绝旧会话；已通过入口但尚未取得管理锁的操作须再次判断操作者资格。不把本链路的重验推广为所有业务提交，更不把删 cookie 等同于关闭既有 SSE、取消 Run 或撤回外部 effect。

## 迁移与历史兼容

[0032](../../PJM/backend/migrations/versions/0032_user_lifecycle.py)为既有用户初始化 row_version=1，不补造 CREATED 或历史变更事件。因此安全历史为空不等于用户从未被管理，也不能要求所有旧用户都存在第一条事件。

该 migration 在存在任意 user_security_events 行时拒绝 downgrade 丢表；已经失效的会话及其操作记录仍需保留。不能先删除事件凑降级条件，不能把 schema 升级当作 API/UI 已交付。会话协议迁移 [0031](authentication.md#会话凭据-v2-与切换要求)与账户审计迁移负责不同事实，按[发布审查](../operations/deployment.md#迁移与回退审查)分别验收。

备份恢复可能带回旧角色、密码和会话有效性，须按[恢复后验证](../operations/backup-recovery.md#恢复后验证)重新核对安全事实；本设计不提供未经审查的 SQL 撤权或清表步骤。

## 开发接续与验收

先从[Backend 接线入口](../../PJM/backend/README.md#ユーザー管理の接続を引き継ぐ)核对内部逻辑与 migration，再同步[公开契约入口](../../PJM/contracts/README.md#ユーザー管理の公開面を準備する)和[Web 账户入口](../../PJM/web/README.md#アカウント管理を接続する)。接续分四步，不重做已存在的服务或 Schema：

1. 保持可信的合跑基线：工作副本已使用独立 user_harness，原裸 conftest 冲突见[合跑复核与剩余工作](../planning/roadmap.md#r05-领域与身份安全)。继续合跑 users / API / contracts，不将单独通过或收集成功当作整条交付完成。
2. 核对路由、Schema/example 与错误声明，再由 exporter 同步 OpenAPI；保持已有专属 Web client/validator/barrel 的回归，不重新搭建同一客户端，不手改快照或放宽校验取得通过。
3. 接账户入口、服务端分页、版本化表单与三语反馈；用真实 component 验成功、拒绝、未知以及换 actor 的晚到响应。
4. 在获准专用环境验证真实锁竞争、审计回滚、bootstrap、迁移和双浏览器会话，再决定发布。离线通过不跨过此门禁。

以下是完整验收条件，不是已经通过的测试清单：

| 给定场景 | 可观察结果与证据层次 |
| --- | --- |
| 两个 ADMIN 并发降级/停用、并发 bootstrap | 至少保留一个活动 ADMIN，首 ADMIN 不重复；真实 PostgreSQL 锁竞争与事务回滚，不能仅比较 SQL 文本 |
| 新登录与撤销竞争、角色改回、停用后启用 | 按 User 锁顺序确定先后；提交前的旧会话不能复活，新会话不漏过应适用的撤销 |
| 操作者锁等待过期/被撤权、跨组织目标 | 锁后重新判断并拒绝，不泄漏目标内容；不使用等待前时间或旧 actor 角色 |
| 旧版本、匹配版本 no-op、重复 email、审计失败 | 冲突无部分变更；no-op 不造事件；唯一键不冒充幂等；父子写入/事件失败完整回滚 |
| 改密错误、超限或 Redis 不可用 | 防护按入口阶段计数；密码错误不注销有效会话；配额拒绝不进入密码计算/管理变更，成功不清零 |
| 任意客户端 request ID、公开 DTO、缓存 | 审计由服务器生成关联；header/Problem 一致，所有成功/失败响应不缓存；密码/token/hash 不经日志或 DTO 泄漏 |
| 自撤销/改密成功、响应丢失、双提交与换 actor | 实 component 验证状态与焦点，真实双浏览器/Server 验会话；abort 不当回滚，不消费旧结果或自动重放 |
| 0031/0032 升降级、旧用户、历史恢复 | 旧会话切换、旧用户无伪造事件、含审计拒绝 downgrade；用获准专用数据库验证，不试跑在共享环境 |

本地单测、mock HTTP、真实 DB、真实会话和文档浏览分别举证。需要创建/删除专用测试数据库时先确认授权；缺环境不改变验收条件，也不阻止先补安全的内部/契约回归。
