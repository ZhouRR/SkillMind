# ProjectMind 认证、会话与 Secret Storage 决策

状态：已确认  
日期：2026-07-03

本文冻结 MVP 的认证安全边界，作为认证实现依据。用户、项目和权限的业务定义仍以 `04_ProjectMind_Domain_Model.md` 为准。

## 1. 威胁边界与目标

- ProjectMind 使用独立账号体系，不依赖外部统一认证。
- 生产入口仅允许共享 Traefik 提供的 HTTPS；API 与 Web 保持同源，均位于 `PROJECTMIND_CONTEXT_PATH` 下。
- 浏览器不得持有长期 Bearer token，也不得把 session identifier 保存到 Local Storage 或 Session Storage。
- PostgreSQL 是用户、会话、项目成员关系和审计信息的正本。Redis 只用于登录限流等短期状态，不作为会话正本。
- MVP 不实现 OAuth/OIDC、API token、密码找回、邮件验证和 MFA。未启用 MFA 的部署只适合受控内部环境；对公网开放前必须另行完成 MFA、账号恢复和安全运营设计。

## 2. 密码认证

- email 在组织内以标准化值唯一；登录失败统一返回相同 Problem，不暴露账号是否存在、停用或密码错误。
- 密码允许 Unicode、空格和密码管理器生成值；最少 15 个字符，最多按 UTF-8 编码后 1024 bytes；不要求大小写、数字或特殊字符组合。
- 密码使用 Argon2id 保存，参数基线为 memory 19 MiB、iterations 2、parallelism 1。数据库仅保存 PHC 格式 hash，不保存密码、可逆密文或独立 salt 字段。
- 成功登录时若 hash 参数已过期，在同一受控流程内 rehash。登录失败按账号与来源组合做渐进限流，不能通过永久锁定制造拒绝服务。
- 日志、Problem、审计 metadata 和测试 fixture 均不得包含密码或完整 session/CSRF token。

参数基线与密码策略参考 OWASP [Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) 和 [Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)。

## 3. 浏览器会话

- 登录成功生成至少 256 bit 的密码学随机 opaque token；数据库只保存 `sha256:<hex>` token hash。
- 生产 cookie 名为 `__Host-projectmind_session`，属性为 `Secure; HttpOnly; SameSite=Strict; Path=/`，不设置 `Domain`。开发环境允许使用非 `__Host-` 名称和非 Secure cookie，但不能带入 production。
- 会话默认 idle timeout 为 30 分钟、absolute timeout 为 12 小时；ADMIN 会话 absolute timeout 为 8 小时。每次登录以及权限提升后必须轮换 token。
- logout、用户停用、密码修改和 ADMIN 主动撤销会话时立即写入 `revoked_at`。过期记录按保留策略清理，不依赖 Redis key 自动过期表达审计事实。
- 请求只使用 token hash 查询活动会话，并同时检查 user status、idle/absolute expiry。`last_seen_at` 使用节流更新，避免每个请求产生数据库写放大。

Cookie 和会话生命周期依据 OWASP [Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。

## 4. CSRF 与同源约束

- 所有非安全方法（POST、PUT、PATCH、DELETE）必须同时通过 synchronizer CSRF token 和 Origin 校验；SameSite 仅是纵深防御，不能替代 CSRF token。
- CSRF token 由服务器生成并绑定 session。浏览器通过认证状态 endpoint 取得 token，只在 `X-CSRF-Token` header 回传，不写入 cookie、URL 或日志。
- 登录 endpoint 也校验允许的 Origin，并使用短时 login CSRF 流程，防止 login CSRF。SSE 和其他 GET endpoint 不改变服务器状态。
- 反向代理来源判断仅信任明确配置的 Traefik proxy；不能直接信任任意 `X-Forwarded-*` header。

实现依据 OWASP [Cross-Site Request Forgery Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)。

## 5. 首个 ADMIN 初始化

- 不提供公开的匿名 bootstrap HTTP endpoint，也不在 API 启动时从环境变量自动创建管理员。
- 运维人员在 API container 内执行一次性 CLI。CLI 从交互式终端读取 email、display name 和密码，密码使用 `getpass`，不得出现在命令参数、环境变量或 shell history。
- 初始化 transaction 对组织级 bootstrap 锁串行化，并在已存在任何 ADMIN 时 fail closed。migration 先创建单一 Organization，CLI 在其锁内创建首个 ADMIN，不负责创建 Organization。
- 后续用户与 ADMIN 由已认证 ADMIN 通过管理 API 创建；不能再次运行 bootstrap 提权。普通初始化使用 `ops.bootstrap_admin`；Makefile 的同名近似 target 会清空全数据，见[起动说明](../operations/quickstart.md#最初の-admin-を作成する)。

## 6. 权限判定

- 系统角色只有 `ADMIN` 和 `USER`。ADMIN 可跨本组织管理项目与用户；USER 必须具有活动 `ProjectMember` 才能访问项目。
- 路由只负责解析 actor 与调用 use case。统一 authorization service 按“平台硬拒绝 → user status/system role → project membership → project/Skill/Tool policy → Run snapshot”判定。
- Project-scoped resource 使用 `project_id + resource_id` 复合查询；越权与不存在统一返回 404，避免泄露资源存在性。
- Run 创建时把 actor、项目成员资格以及有效 Skill/Tool 权限固化到不可变 permission snapshot。之后用户权限变化不改写历史 Run。

## 7. Secret Storage

ProjectMind 支持三种 SecretReference resolver。`ENVIRONMENT` 与 `FILE` 是部署方管理的既有方式（零 at-rest），`MANAGED` 是 `docs/01` §18 引入的应用层信封加密方式，供 UI 自助录入凭据。

### 7.1 部署方管理（ENVIRONMENT / FILE，零 at-rest）

- MVP 首选部署方管理的 Docker/文件 Secret。数据库 `SecretReference` 只保存 provider、resolver、locator、key version 和状态，明文 Secret 值不落库。
- Secret 仅挂载到确需消费它的 API/Worker/Provider 边界；Web、Run workspace、Agent subprocess、Evidence 和日志不可见。
- `env_file` 中的 provider credential 只作为现有迁移期兼容方式，不视为生产目标。通用 Settings 的 `_FILE` 配置扩展是后续要求，当前不能假设所有环境变量都支持同名 `_FILE`。已有 FILE resolver 的定位与权限检查由 integrations/secrets 实现。

### 7.2 平台托管（MANAGED，应用层信封加密）

MANAGED resolver 让 ADMIN 在资源向导直接输入凭据，无需运维放文件或重启 Worker。这是一次**明确的边界移动**：以「明文经 API 一次、密文存库」换取「UI 自助」，用信封加密把风险约束到可接受范围。

- **明文永不落库**：ADMIN 提交的明文经创建端点一次，服务端立即用部署级主密钥（KEK）做 AES-256-GCM 加密，只把密文写入独立表 `managed_secret_material`。明文不落库、不回传、不进日志、不进 Evidence、不进 Agent；`secret_value` 字段名被 `core/redaction.py` 的 `find_sensitive_key` 命中遮断。
- **密文与公开读模型物理分表**：密文、nonce 与 KEK 版本存于独立表，任何 SecretReference/Integration response model 都不投影该表，从类型上杜绝密文经 API 泄出。
- **KEK 只在部署环境**：KEK 以 base64 keyring 形式注入 `PROJECTMIND_MANAGED_SECRET_KEK` 环境变量，仅 API 与 Worker 进程可读，绝不写入数据库、备份、日志或 Agent 环境。
- **AAD 绑定 project_id + secret_reference_id**：GCM 的 Additional Authenticated Data 绑定所属 project 与 reference id，密文被搬到别的 project/reference 后解密失败，防止越权复用。
- **fail closed**：KEK 缺失、版本不匹配、密文被篡改或 AAD 不一致时，Worker 解析一律失败并复用现有「credential unavailable」路径，绝不降级为明文或空值。
- **轮换**：`ops/rotate_secrets.py` 用 KEK keyring（新键在前、旧键仅解密）就地重加密全部托管密文并更新 KEK 版本，当前密文重封装完成后可从现用 keyring 移除旧键，但旧备份仍需对应旧键恢复；备份有效期内必须另行安全保留，不能销毁唯一副本。轮换命令只输出计数，不显示任何凭据。

**威胁模型（必须理解的边界）**：MANAGED 防的是「数据库或其备份被单独导出」——拿到密文而无 KEK 无法解密。它**不防**「主机/进程被完全攻陷」——攻陷进程可同时读到环境变量里的 KEK 与库中密文。外部 KMS/HSM 可降低密钥被直接导出的风险，但不能单独阻止已获授权且被控制的应用进程请求解密；更强隔离需独立设计（当前范围外）。

**残余风险**：KEK 丢失 = 全部托管凭据不可恢复。运维手册（`docs/10`）必须要求 KEK 与数据库备份分离保存。

### 7.3 共同约束

- 本次实现不部署 Vault，不新增对外服务，也不让 ProjectMind Compose 配置专有 Traefik。MANAGED 全程在现有 API/Worker 进程内完成，不违反该约束。

## 8. 数据模型与实现工作包

以下为已完成实现的责任清单，后续修改继续按对应边界回归；它不是等待重新实施的计划：

1. `Organization`、`User`、`AuthSession`、`Project`、`ProjectMember` migration，以及密码/session domain helper。
2. 一次性 ADMIN bootstrap CLI；登录、登出、当前会话和 CSRF middleware。
3. 统一 actor/authorization dependency，先保护新 Project API，再逐步收口 Skills、Run、Result、Evidence 和 Evaluation。
4. Project CRUD、成员管理和用户 Project preference；Web 登录与真实 Project 选择。
5. 删除固定 actor 与 fixture Project 的公开 API fallback，并完成跨 Project/角色测试。

当前代码入口：[auth](../../PJM/backend/src/projectmind/auth/)、[actor dependency](../../PJM/backend/src/projectmind/api/auth_dependencies.py)、[Secret resolver](../../PJM/backend/src/projectmind/integrations/secrets.py)。部署仍需按[计划](../planning/roadmap.md#13-当前执行状态)验证认证、CSRF 与越权场景。
