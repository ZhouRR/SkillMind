# ProjectMind 认证、会话与 Secret Storage 决策

> 定位：浏览器身份、会话和项目授权的设计正本。基线确认于 2026-07-03，以下现状于 2026-09-09 对照工作副本核对；不是整套安全能力已交付的声明。

所有权见[领域模型](domain-model.md)，登录前的配额与失败关闭见[登录入口防护](login-protection.md)，Provider 凭据的保存与轮换见[Secret 设计](secret-storage.md)，实施状态统一见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。本页保留旧标题与章节编号兼容引用；登录防护和 Secret 的详细规则不在这里重复维护。

先看[两页面的例子](#一个例子同一账号打开两个页面)理解行为，再看[会话协议与兼容](#会话凭据-v2-与切换要求)。排障使用[认证故障分诊](../operations/runbook.md#认证故障的只读分诊)，后续管理功能从[用户生命周期](user-lifecycle.md)接续；本页的[授权与业务提交](#认证与业务提交不是同一个事务)解释它为何需要独立事务。这里描述工作副本，不假定部署环境已升级。

## 先分清四类凭据

| 名称 | 用途与边界 |
| --- | --- |
| login CSRF challenge | 登录前的一次性凭据。响应正文与短时 HttpOnly cookie 配对，Redis 原子消费；不是已登录会话 |
| session token | 浏览器由 HttpOnly cookie 携带，服务端用 hash 找到 AuthSession；Web JavaScript 不读取原值 |
| session CSRF token | 登录后的写请求放入 X-CSRF-Token；Web 保存在内存，服务端与当前会话比较。它不能单独证明身份 |
| Provider Secret | 外部资源的密码或 API key，只交给受控 Provider；不是 ProjectMind 登录 token，也不授予新的 Tool 权限 |

登录接口与业务写接口虽共用 header 名，使用的 token 不同；不能把登录前 challenge 留作后续 Run 创建的 CSRF。

## 一个例子：同一账号打开两个页面

当前 v2 会话在同一有效 session 下返回稳定的 CSRF。打开另一页面不再更换写凭据：

```text
页面 A 读取会话 → 保存 CSRF S
        ↓
页面 B 读取同一会话 → 仍返回 S
        ↓
页面 A 提交 → 携带 cookie + S
        ↓
服务端重新核对会话、Origin 和 CSRF
```

这只消除了“正常读取相互撤销 CSRF”的原因，不保证提交成功：会话过期、登出、角色不匹配、错误 Origin 或目标资源越权仍会拒绝。重新登录换了 cookie 后，旧页面内的 CSRF 也不与新会话匹配；稳定只针对同一会话，不是同一账号的所有登录。

调用方是 [get_session / authenticate_unsafe_session](../../PJM/backend/src/projectmind/auth/service.py) 与 [Web 启动](../../PJM/web/src/App.tsx)；[现有 service 回归](../../PJM/backend/tests/auth/test_session_service.py)用共享 fake 数据库检查多次读取和不同 service 实例，不是实机多标签页验收。旧轮换问题保留在[历史 §73](../history/delivery-history.md#73-认证凭据与安全开发入口文档续整2026-09-09)。当前规则见[会话读取与多页面](#会话读取与多页面)，不靠关闭 CSRF 或自动换幂等键解决失败。

## 1. 威胁边界与目标

- ProjectMind 使用独立账号体系，不依赖外部统一认证。
- 生产入口仅允许共享 Traefik 提供的 HTTPS；API 与 Web 保持同源，均位于 `PROJECTMIND_CONTEXT_PATH` 下。
- 浏览器不得持有长期 Bearer token，也不得把 session identifier 保存到 Local Storage 或 Session Storage。
- PostgreSQL 是用户、会话、项目成员关系和审计信息的正本。Redis 只用于登录限流等短期状态，不作为会话正本。
- MVP 不实现 OAuth/OIDC、API token、密码找回、邮件验证和 MFA。未启用 MFA 的部署只适合受控内部环境；对公网开放前必须另行完成 MFA、账号恢复和安全运营设计。

当前是单 Organization 部署：数据库 email 唯一键包含 organization_id，但登录请求只传 email/password，查询也未选择组织。这个前提不能直接推广为多租户登录；引入第二组织前必须先定义组织选择、查询隔离和错误语义，不通过放宽唯一键解决。

## 2. 密码认证

- email 在组织内以标准化值唯一；凭据错误统一返回相同 Problem，不暴露账号是否存在、停用或密码错误。限流、防护不可用与请求校验使用各自错误，不把所有登录失败概括为 401。
- 密码允许 Unicode、空格和密码管理器生成值；最少 15 个字符，最多按 UTF-8 编码后 1024 bytes；不要求大小写、数字或特殊字符组合。
- 密码使用 Argon2id 保存，参数基线为 memory 19 MiB、iterations 2、parallelism 1。数据库仅保存 PHC 格式 hash，不保存密码、可逆密文或独立 salt 字段。
- 成功登录时若 hash 参数已过期，在同一受控流程内 rehash。登录防护应兼顾账号、来源与组合维度，并为计数和惩罚设置有限期限，不能通过永久锁定制造拒绝服务。
- 日志、Problem、审计 metadata 和测试 fixture 均不得包含密码或完整 session/CSRF token。

参数基线与密码策略参考 OWASP [Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) 和 [Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)。

工作副本已用来源、账号、组合三维检查和有限退避替代旧组合固定窗口；来源在正文校验前计数，成功登录不清零。执行顺序、TTL、429/503 和误伤取舍统一见[登录防护设计](login-protection.md#保护范围与执行顺序)。Web 的等待 header、三语与在途控制，认证错误的公开声明，以及真实表单 + mock API 的交互分别有验证入口；当前证据与环境残项见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。密码请求已发出后的离页/断连也不撤销服务端会话，按[结果未知的边界](login-protection.md#提交离页与结果未知)接续。

## 3. 浏览器会话

- 登录成功生成至少 256 bit 的密码学随机 opaque token；数据库只保存 `sha256:<hex>` token hash。
- 生产 cookie 名为 `__Host-projectmind_session`，属性为 `Secure; HttpOnly; SameSite=Strict; Path=/`，不设置 `Domain`。开发环境允许使用非 `__Host-` 名称和非 Secure cookie，但不能带入 production。
- 会话默认 idle timeout 为 30 分钟、absolute timeout 为 12 小时；ADMIN 会话 absolute timeout 为 8 小时。每次登录创建新 token；权限变化的旧会话处理见下表，不把 CSRF 更新当作 session token 轮换。
- logout、用户停用、密码修改和 ADMIN 主动撤销会话应持久化失效事实；[账户链路的接线边界](user-lifecycle.md#工作副本与公开入口)分别列明工作副本 API 与未完成的 Web/契约交付。过期记录按保留策略清理，不依赖 Redis key 自动过期表达审计事实。
- 请求只使用 token hash 查询活动会话，并同时检查 user status、idle/absolute expiry。`last_seen_at` 使用节流更新，避免每个请求产生数据库写放大。

Cookie 和会话生命周期依据 OWASP [Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。

### 会话失效与权限变化

| 情况 | 现状与后续要求 |
| --- | --- |
| 登录 / 登出 | 登录新增 AuthSession；登出校验 Origin/CSRF 后写 revoked_at、删除当前 cookie。新登录不代表自动撤销该账号的其他历史会话 |
| 用户已停用 | 下一次认证检查 User.status 并拒绝；管理用例另将旧会话持久撤销。直接改 status 再恢复不具备同样保证，不能绕过用例实现停用 |
| 角色与登录时不同 | v2 比较当前角色与登录快照，不匹配即拒绝，包括读请求。管理用例的角色变更另撤销旧会话；只比较快照记不住改回前的撤权 |
| 密码修改 / ADMIN 撤销 | 工作副本 API 已接管理用例，要求账号变更、旧会话失效和审计原子提交；Web 与真实事务仍待验。密码 hash 改动本身不使会话自动失效 |
| 锁等待跨过期限 | 当前先锁 User、再锁 AuthSession，最后取新时间检查期限与失效，不再使用等待前的 now。已有模拟时钟/查询回归，真实行锁竞争仍需验证 |

当前 idle 更新间隔为 5 分钟，并受 absolute expiry 截止；认证读仍获取数据库行锁。“节流写入”不表示不锁行，也不证明并发撤销已验收。会话撤销不改写历史 Run 的 permission snapshot；已开始的业务执行如何停止，由[执行监督](run-supervision.md)定义，不能用删 cookie 代替取消。

## 4. CSRF 与同源约束

- 所有非安全方法（POST、PUT、PATCH、DELETE）必须同时通过 synchronizer CSRF token 和 Origin 校验；SameSite 仅是纵深防御，不能替代 CSRF token。
- session CSRF token 由服务器生成并绑定 session，通过登录/会话响应取得，只在 `X-CSRF-Token` header 回传，不写入 cookie、URL 或日志。登录前 challenge 的短时 cookie 是另一套流程，不与此规则混淆。
- 登录 endpoint 也校验 Origin，且要求 header 与 login-context cookie 一致，再用 Redis GETDEL 消费 challenge。重试登录先取新 challenge；不得把匿名 challenge 升格为业务会话。
- 业务 GET/SSE 不触发业务写命令。认证维护有副作用：login-context 创建短时 challenge，普通认证可能更新 idle 记录；v2 会话读取不轮换 CSRF，但不因此成为完全无持久化变化的健康检查。
- 反向代理来源判断仅信任明确配置的 Traefik proxy；不能直接信任任意 `X-Forwarded-*` header。

实现依据 OWASP [Cross-Site Request Forgery Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)。

### 会话读取与多页面

设计选择是同一会话的正常页面读取不使其他页面仍有效的写凭据立即失效。原因是页面加载、返回和多个标签页均不是提权或账号切换，不应成为互相撤销写入资格的操作；OWASP 的 synchronizer token 说明也区分 session 与逐请求 token 的可用性取舍。

当前实现采用下面的 v2 派生方案，不从 hash 还原原 token，也不保存一串永不过期的历史 CSRF。读取顺序或响应乱序不改变同一会话的凭据；登出、过期、协议/角色/hash 不匹配则拒绝会话。Web 仍只接收 opaque 字符串，不实现派生逻辑或跨页面认证正本。

会话读取不承担业务重试。CSRF 错误后先保留原请求身份、确认当前 actor/Project 与已提交事实，再由用户决定后续操作；不能无条件重放创建、批准或外部写入。当前没有通用的跨标签页身份同步或自动恢复流程，不能将服务端稳定 token 说成这些 UI 能力已经完成。

### 会话凭据 v2 与切换要求

此方案已存在于 [auth/domain.py](../../PJM/backend/src/projectmind/auth/domain.py)，本次文档整理没有新增或修改算法。参数属于内部会话协议 v2，不等于公开 SessionResponse 升为 v2：

| 项目 | 固定含义 |
| --- | --- |
| session 原值 | pm2. 前缀 + 32 随机 bytes 的无填充 base64url，完整长度 47 个 ASCII 字符 |
| 派生输入 | 完整 session 原值的 ASCII bytes；不是数据库可见的 token_hash，也不是密码 |
| HKDF | SHA-256，输出 32 bytes；salt=None 表示未提供 salt，使用规范的默认零值，不是每次重新生成随机 salt |
| 用途标记 | info 固定为 `projectmind.auth.session-csrf/v2`；同版中不能修改 |
| CSRF 原值 | csrf2. 前缀 + 派生输出的无填充 base64url，完整长度 49 个 ASCII 字符 |
| 数据库存储 | session 与 CSRF 各自的 sha256:<hex>；不存明文原值或可逆 token 密文 |

复用 cryptography 的 HKDF，不自行实现密码学原语。默认 salt 与用途隔离依据 [RFC 5869 §2.2、§3.2](https://www.rfc-editor.org/rfc/rfc5869)；这是项目的方案选择，不表示 RFC 或 OWASP 认证了整个应用协议。选择的取舍是无需新增 token 密文或部署主密钥，但依赖原 session 的随机性及保密性，并要求固定参数与旧会话切换。cookie 泄漏仍是会话失窃，派生不能降低该风险。

AuthSession 的 credential_version 和 system_role_at_login 是内部保存字段。新登录显式写入 v2 和当时角色；数据库 version 的默认值仍是 1，用于识别旧写入方，不能“清理”为默认 2 而把旧会话误标成新版。认证先校验原值格式，再按 User → AuthSession 锁定，最后取新时间检查期限、撤销、用户状态、协议、角色和派生 hash；读请求也经过这些检查。损坏或不支持的会话统一拒绝，不尝试旧格式 fallback。

[migration 0031](../../PJM/backend/migrations/versions/0031_auth_session_credentials.py)将旧会话标为 v1 并持久撤销活动行；用户须重新登录，不迁移旧随机 CSRF。存在任何 v2 行（包括已撤销）时，downgrade 拒绝删除协议/角色审计列。混合新旧 API 不受支持：旧读取会改坏新 hash，旧写入也不能被当作 v2。上线/回退按[会话协议切换检查](../operations/deployment.md#会话协议切换检查)执行，不删审计行绕过限制。公开 token 字符串字段不变，仍需分别验收旧 Web、真实迁移和 HTTPS 多实例。

### 缓存与代理

含 token 的响应必须禁止缓存。当前 [API 中间件](../../PJM/backend/src/projectmind/api/main.py)对匹配 auth tag 的 route 响应设置 Cache-Control: no-store，涵盖正常响应、已处理的认证错误和请求校验错误；还按[登录入口路径](login-protection.md#公开响应与客户端责任)覆盖路由匹配前的防护拒绝。[Web auth client](../../PJM/web/src/api/auth.ts)的会话、login-context、login 与 logout 请求也指定 no-store。不能由此宣称其他未匹配路径、未处理异常、代理自产错误或整个站点均已有相同响应头，实际入口仍需检查。

当前 Origin 比较点是 [require_same_origin](../../PJM/backend/src/projectmind/api/auth_dependencies.py)，对比请求 Origin 与代理恢复后的 scheme/host/port；context path 不属于 Origin。缺失 Origin 不走宽松 fallback，生产不能用关掉 Secure cookie 或扩大可信代理范围来修复配置错误。

## 5. 首个 ADMIN 初始化

- 不提供公开的匿名 bootstrap HTTP endpoint，也不在 API 启动时从环境变量自动创建管理员。
- 运维人员在 API container 内执行一次性 CLI。CLI 从交互式终端读取 email、display name 和密码，密码使用 `getpass`，不得出现在命令参数、环境变量或 shell history。
- 初始化 transaction 对组织级 bootstrap 锁串行化，并在已存在任何 ADMIN 时 fail closed。migration 先创建单一 Organization，CLI 在其锁内创建首个 ADMIN，不负责创建 Organization。
- 后续用户与 ADMIN 由已认证 ADMIN 通过受控管理流程创建；工作副本的管理 API 已接入，Web 与完整契约交付尚未完成，见[账户公开边界](user-lifecycle.md#工作副本与公开入口)。不能用 ProjectMember 管理、反复 bootstrap 或直接 SQL 补出提权路径。

普通初始化使用 `ops.bootstrap_admin`；Makefile 的同名近似 target 会清空全数据，见[起动说明](../operations/quickstart.md#最初の-admin-を作成する)。本页不提供第二套初始化命令。

## 6. 权限判定

- 系统角色只有 `ADMIN` 和 `USER`。ADMIN 可跨本组织管理项目与用户；USER 必须具有活动 `ProjectMember` 才能访问项目。
- 路由负责 actor/资源入口与 use case 调用，领域服务与 Tool Gateway 继续检查策略；不是一次登录就完成后面所有授权。平台硬拒绝、当前用户与项目资格、冻结权限上限、参数级 scope 和批准均须满足，后一层不能覆盖前一层拒绝。
- Project-scoped resource 必须核对 `project_id + resource_id` 的所有权；查询可直接带条件，或在内部取得记录后立即核对并隐藏差异。公开越权与不存在统一返回 404，不能在核对前返回资源正文。
- Run 创建时把 actor、项目成员资格以及有效 Skill/Tool 权限固化到不可变 permission snapshot。之后用户权限变化不改写历史 Run。

资源访问权不等于操作批准权。外部变更 decision 先经过 ProjectWriteActor 的认证/CSRF/项目边界，再检查 Run 发起人或 system ADMIN 身份与精确 Proposal；当前没有独立的“Project ADMIN”角色。预授权仅由 system ADMIN 创建，且不覆盖 repository.write。具体批准、重放与执行权要求统一见[受控写入](repository-effects.md#调用与批准链路)，不以 Skill guidance 或历史 permission snapshot 代替当前入口授权。

### 从请求到项目内操作

```text
浏览器 cookie / 写请求的 Origin + CSRF
        ↓
AuthService：当前用户、会话与期限
        ↓
actor dependency：角色、Project 访问
        ↓
use case：目标所有权、版本、业务条件
        ↓
执行边界：冻结权限、scope、Tool / 批准
```

ReadActor / WriteActor 只建立会话或写凭据边界；ProjectReadActor / ProjectWriteActor 另外检查 Project。当前项目写 alias 还拒绝 ARCHIVED，项目读取不因此自动拒绝；ADMIN 的组织内访问也不覆盖业务上的硬拒绝。具体实现与正反例由 [Backend 接续入口](../../PJM/backend/README.md#認証と-secret-の境界を追う)连接，不在各 route 新建同义认证逻辑。

### 认证与业务提交不是同一个事务

当前 AuthService 在返回 actor 前结束认证事务；Project 资格和具体 use case 由后续调用检查。因此“锁后检查会话”只保护这次认证判断，不把整个 HTTP 请求锁成一个不可撤权的区间，也不证明权限在所有业务提交时再次核验。

用户管理用例另在[管理事务](user-lifecycle.md#事务与并发)内重验原会话，并将改密/停用/角色变更、旧会话撤销和审计一起提交；重新启用账号或改回角色不能复活已撤销会话。这不自动修补其他业务的提交边界，也不保证既有 SSE 或在途 Run 立即停止。真实锁竞争与回滚仍须验证；Run 的停止按[执行监督](run-supervision.md)处理。

## 用户生命周期与管理事务

完整规则已移至[用户生命周期与安全管理](user-lifecycle.md)，本节保留旧锚点。[源码与公开入口](user-lifecycle.md#工作副本与公开入口)区分已接的管理 API/Schema、未同步的 OpenAPI 快照与未接入的 Web，不再用一个“已实现/未实现”概括整条链路。

### 用户操作与公开面

见[操作与公开面](user-lifecycle.md#用户操作与目标公开面)和[改密配额](user-lifecycle.md#改密入口的配额)。账户管理与 ProjectMember 管理分开，路由存在不表示 Web 或部署已可用。

### 事务与并发

见[管理事务与锁顺序](user-lifecycle.md#事务与并发)、[审计关联](user-lifecycle.md#审计与请求关联)和[迁移兼容](user-lifecycle.md#迁移与历史兼容)。当前 middleware 每次生成服务器 UUID；它用于关联，不替代授权、幂等或真实事务证据。

### 生效与界面

见[界面与结果未知](user-lifecycle.md#生效与界面)及[用户管理验收](user-lifecycle.md#开发接续与验收)。不能把密码错误、会话失效和断连合并为自动重新登录/重发，更不能从账户停用推导在途 Run 已停止。

## 7. Secret Storage

凭据生命周期已独立为 [Secret 保存、解析与轮换](secret-storage.md)。以下仅保留旧章节锚点；新增规则与实现说明只在新正本更新。

### 7.1 部署方管理（ENVIRONMENT / FILE，零 at-rest）

见[三种来源的边界](secret-storage.md#三种来源的边界)。“零 at-rest”只表示应用数据库不保存原值，不保证部署文件、环境或宿主没有凭据副本。

### 7.2 平台托管（MANAGED，应用层信封加密）

旧标题为兼容引用保留；当前是主密钥直接执行 AES-256-GCM，并无逐项 DEK 的信封结构。准确格式、威胁边界与历史名称见[MANAGED 的实际加密结构](secret-storage.md#managed-的实际加密结构)，不因术语更正改写已有密文或设置名。

### 7.3 共同约束

最小暴露、失败拒绝与备份要求见[读取与失效](secret-storage.md#读取与失效)及[轮换不是更换外部凭据](secret-storage.md#轮换不是更换外部凭据)。本项目不因此新增 Vault、KMS、对外服务或专有 Traefik。

## 8. 数据模型与实现工作包

已有 models/migration、密码 helper、bootstrap、登录/会话/登出、统一 actor dependency、Project/成员管理与 Web 登录；工作副本另有会话 v2/0031、稳定 CSRF、登录角色快照、no-store 和三维登录防护。它们是接续起点，不是用户生命周期、真实多页面并发和生产代理安全已完成的证明；缺口登记在[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。

后续按[契约同步流程](../development/contract-workflow.md)修改公开接口。认证与管理凭据的 field 入口见[认证契约索引](../../PJM/contracts/README.md#認証と-secret-の契約を読む)，旧会话处理必须与 Web/代理一起上线；不得新增仅被某个页面使用的旁路登录。

### 验收从场景出发

| 给定场景 | 必须观察到的结果 |
| --- | --- |
| 同会话两页面、返回/刷新、响应乱序 | 正常读取不互相撤销写凭据；actor 变化与单纯 CSRF 更新分开，原业务动作不被自动重放 |
| 错误/缺失/过期/已消费 challenge | 登录不创建会话；新 challenge 与旧业务 token 不能混用；失败不暴露账号存在性 |
| 行锁等待期间过期、登出、停用或提权 | 提交边界重新判断有效性；失效凭据不恢复，旧会话不自动取得新角色；事务回滚可验证 |
| 改密、停用后恢复、角色修改后改回 | 管理事务持久撤销旧会话；不能因当前状态重新匹配而复活旧 cookie。此项管理流程待实现 |
| 0031 升级、旧 API 或降级尝试 | 旧会话要求重登；新旧 API 不混跑；存在任何 v2 审计行时拒绝丢列，失败后不删除记录凑条件 |
| 其他 Project、归档 Project、非 ADMIN | 分别验证隐匿 404、项目写拒绝和管理权限；读权限不替代批准身份或 Tool scope |
| 实际 HTTPS / context path / 多 API 实例 | Secure/HttpOnly/SameSite、可信代理、Origin、禁止缓存和登录限流生效；绕过 proxy 的来源不能被信任 |
| 已登录的 SSE 或已开始的 Run 遇到撤权 | 明确后续请求、既有流、执行与审计各自的处理，不把下一次认证拒绝当成立即停止全系统 |

会话核对见[交付记录 §75](../history/delivery-history.md#75-会话现状对齐与文档维护指南续整2026-09-09)，三维防护分离见[§76](../history/delivery-history.md#76-登录防护设计与开发入口文档续整2026-09-09)，后续客户端与分层验证见[§77](../history/delivery-history.md#77-登录客户端现状与验证入口文档续整2026-09-09)。旧行为见 §73。登录限流的场景独立放在[防护验收](login-protection.md#开发接续与验收)。fake transaction/route 不能证明实 DB、真实浏览器会话或部署；[真实会话测试](../../PJM/backend/tests/db/test_real_auth_sessions.py)的存在也不是已执行的证据，须先确认专用测试数据库的创建/删除授权。文档浏览验证只检查是否容易阅读。
