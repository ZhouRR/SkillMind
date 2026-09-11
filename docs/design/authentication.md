# 认证、会话与项目授权

本页负责浏览器身份和授权入口；[登录防护](login-protection.md)、[用户管理](user-lifecycle.md)、[外部 Secret](secret-storage.md)分别维护独立协议。当前交付缺口见[计划 R05](../planning/roadmap.md#开发任务)。

## 先分清四类凭据

| 凭据 | 用途与边界 |
| --- | --- |
| login CSRF challenge | 登录前正文与短时 HttpOnly cookie 配对，Redis 一次性消费；不是会话 |
| session token | HttpOnly cookie 携带；服务端仅保存 hash，Web 不读取原值 |
| session CSRF token | 当前会话的写请求 header；Web 仅在内存保存，不能单独证明身份 |
| Provider Secret | 只交受控 Provider 使用，不授予额外 Tool 权限 |

## 威胁边界与目标

- 独立账号体系，当前只支持一个 Organization。email 唯一键含组织，但登录只按 email 查询；引入多组织前须先定义组织选择与查询隔离。
- 生产由共享 Traefik 提供 HTTPS；API/Web 同源，位于 SKILLMIND_CONTEXT_PATH 下。浏览器不持有长期 Bearer token，不将会话存入 Local/Session Storage。
- PostgreSQL 保存用户、会话、成员与审计；Redis 仅承载登录防护等短期状态。
- 当前不提供 OAuth/OIDC、API token、找回密码、邮件验证或 MFA；受控内部部署的边界不能直接推广到公网。

## 密码认证

email 标准化后在组织内唯一。不存在、停用和密码错误统一为 invalid_credentials；配额、防护不可用、CSRF 和结构错误保持各自语义，见[登录错误](login-protection.md#公开响应与客户端责任)。

密码允许 Unicode、空格和密码管理器值：至少 8 字符，UTF-8 最多 1,024 bytes，不强制字符组合。使用 Argon2id（19 MiB、iterations 2、parallelism 1），只保存 PHC hash；成功登录按需 rehash。密码、完整 session/CSRF 不进入日志、Problem、审计或 fixture。

策略依据：[密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)、[认证建议](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)。项目参数仍需结合部署容量验证。

## 浏览器会话

登录使用至少 256 bit 随机 opaque token，数据库保存 `sha256:<hex>`。生产 cookie 为 `__Host-skillmind_session`，固定 `Secure; HttpOnly; SameSite=Strict; Path=/`，无 Domain；非 Secure 开发设置不能带入生产。

默认 idle 30 分钟，absolute 12 小时，ADMIN absolute 8 小时；idle 写入按 5 分钟节流且不超过 absolute 截止。每次登录创建新 token，不自动撤销其他登录。认证仍锁行，节流不等于无锁。

登录与会话读取响应的可选 `deferred_features_enabled` 表示当前部署是否开放后置执行功能；缺省按关闭处理。Web 用它控制入口及辅助读取，不能作为授权凭证；API 与 Worker 仍分别检查同一部署配置，既有会话不绕过配置变更。

### 会话失效与权限变化

| 情况 | 规则 |
| --- | --- |
| 登出 | Origin/CSRF 通过后持久写 revoked_at，再删除 cookie |
| 停用、角色变化、改密、主动撤销 | 用户管理事务撤销旧会话；重新启用或改回角色不能复活它们 |
| 当前角色与登录快照不同 | v2 读写均拒绝；仅比较当前角色不能代替持久撤销 |
| 锁等待期间过期 | User → AuthSession 取锁后再取时间，检查状态、撤销、idle/absolute 期限 |

密码 hash 改变不自动使会话失效，必须走管理用例。撤销也不改写 Run permission snapshot，更不证明已有 SSE/进程已停止，见[执行监督](run-supervision.md)。

## CSRF 与同源约束

非安全方法同时验证 Origin 与 synchronizer CSRF；SameSite 只是纵深防御。登录前 challenge 经 header/cookie 配对后以 Redis GETDEL 消费，每次重试重新获取；登录后写请求仅通过 X-CSRF-Token 提交会话 CSRF，不能混用两种凭据。

GET/SSE 不触发业务命令，但认证可能更新 idle，login-context 会保存 challenge；不能作为无持久副作用的健康检查。只信任明确配置的代理，不直接相信任意 X-Forwarded-*。

### 会话读取与多页面

同一有效 session 的各页面读取相同 CSRF；重新登录换 cookie 后，旧 token 不再适用。稳定凭据不等于跨标签同步或重试协议：CSRF 失败先核对 actor/Project 与原提交事实，不自动重放创建、批准或外部写入，Web 不自行派生 token。

### 会话凭据 v2 与切换要求

[auth/domain.py](../../SKM/backend/src/skillmind/auth/domain.py)定义内部凭据协议，不改变公开 SessionResponse 版本：

| 项目 | 固定值 |
| --- | --- |
| session | sm2. + 32 随机 bytes 的无填充 base64url，47 ASCII 字符 |
| HKDF 输入 | 完整 session 原值的 ASCII bytes，不是数据库 hash |
| 派生 | SHA-256，32 bytes，salt=None，info=skillmind.auth.session-csrf/v2 |
| CSRF | csrf2. + 派生值的无填充 base64url，49 ASCII 字符 |
| 保存 | session/CSRF 各自 hash；不存明文或可逆 token 密文 |

使用 cryptography HKDF；固定用途标记和默认 salt 遵循 [RFC 5869](https://www.rfc-editor.org/rfc/rfc5869)。方案依赖 session 随机性与保密性，不能降低 cookie 被窃风险。

新登录保存 credential_version=2 与 system_role_at_login；数据库默认仍为 1，以识别旧写入方。认证校验格式后锁 User/AuthSession，锁后检查期限、撤销、用户状态、协议、角色和派生 hash；损坏/未知版本 fail closed，无旧格式 fallback。

[0031 migration](../../SKM/backend/migrations/versions/0031_auth_session_credentials.py)标记并撤销旧活动会话，要求重新登录，不迁移旧随机 CSRF。任何 v2 行存在时均拒绝 downgrade 丢列。新旧 API 不混跑，不删除审计绕过回退限制；上线见[会话切换](../operations/deployment.md#会话协议切换检查)。

### 缓存与代理

auth-tag route 的已处理响应与登录防护路径设置 Cache-Control: no-store，Web auth client 同样禁止缓存；不外推到未处理异常、代理自产错误或全站。实际 HTTPS 入口仍须验收。

[require_same_origin](../../SKM/backend/src/skillmind/api/auth_dependencies.py)比较 Origin 与代理恢复后的 scheme/host/port；context path 不属于 Origin。缺失 Origin 拒绝，不以关掉 Secure/CSRF 或信任全部代理解决配置错误。

## 首个 ADMIN 初始化

仅受控交互 CLI，密码经 getpass，不进参数、环境或 shell history。锁 migration 创建的 Organization，存在任何 ADMIN 即拒绝；后续走[用户管理](user-lifecycle.md)，不以反复 bootstrap、匿名 HTTP、启动自动创建或 SQL 提权恢复账户。命令见[首次起动](../operations/quickstart.md#最初の-admin-を作成する)；当前 make bootstrap-admin 不清空数据，也不是重置入口。

## 权限判定

```text
cookie + 写请求 Origin/CSRF
  → 当前会话/用户/期限
  → actor dependency：角色与 Project
  → use case：目标所有权、版本、业务条件
  → 执行边界：冻结权限、scope、Tool 与批准
```

ADMIN 管理本组织，USER 需 ACTIVE ProjectMember；不存在与越权资源统一 404。ProjectReadActor 可读获授权历史，ProjectWriteActor 另拒绝 ARCHIVED；ReadActor/WriteActor 本身不建立 Project 边界。

Run 创建冻结 actor、成员资格及 Skill/Tool 权限上限，不随后来权限修改。当前请求仍须重新授权，冻结上限不替代当前资格。外部批准另限制 Run 发起人或 system ADMIN；低风险预授权仅 ADMIN 配置，repository.write 不可预授权，见[受控写入](repository-effects.md)。

### 认证与业务提交不是同一个事务

[AuthService](../../SKM/backend/src/skillmind/auth/service.py)返回 actor 前已结束认证事务。业务用例须在自身锁等待后及最终提交门禁重验原请求；入口成功不证明提交时仍有资格。各用例的锁序、重复/未知结果和审计规则只在所属设计维护：

| 授权对象 | 事务正本 |
| --- | --- |
| 账户、成员、项目 | [用户](user-lifecycle.md#事务与并发)、[成员](project-lifecycle.md#成员管理的现状与目标)、[项目 CRUD](project-lifecycle.md#并发修改不能只看有无行锁) |
| Run 与人工操作 | [创建/确认](run-creation.md#创建与确认的授权事务)、[普通答复](user-interactions.md#首次答复与原答复重放)、[评价](results-evaluation.md#评价的授权事务) |
| Skill 组织资产与项目配置 | [导入](skill-interpretation.md#导入保存与上传授权)、[版本管理](skill-interpretation.md#版本管理的授权事务)、[组合](skill-contract.md#组合保存的授权事务) |
| 调度与文档 | [调度管理](task-scheduling.md#管理写入的授权事务)、[上传](document-lifecycle.md#上传的授权事务)、[删除](document-lifecycle.md#删除事务与引用判定) |

新会话可按各协议人工查询原事实，不能恢复旧会话或接管旧写入；历史 Run/发布/上传回执不因当前授权而重写。[异步解释/Worker/SSE](skill-interpretation.md#异步解释的交接要求)尚未闭合原请求授权，其他业务与长连接也不能从上述局部实现推导立即停权。

## 开发接续与验收

复用统一 actor 与凭据校验，接口变更同步[契约](../../SKM/README.md#contracts)、Web client 和回归。重点验证多页面/换账号不自动重放、拒绝不泄漏身份、撤销/登录与锁等待竞争、旧会话不复活，以及 0031、HTTPS/context path、cookie/no-store 和多实例代理信任。

[service 回归](../../SKM/backend/tests/auth/test_session_service.py)与[真实会话测试](../../SKM/backend/tests/db/test_real_auth_sessions.py)分别举证；真实数据库须使用获准的专用目标。
