# Secret 保存、解析与轮换

> 定位：外部资源凭据的设计正本，承接原认证文档 §7。2026-09-09 核对工作副本；现有加密、resolver 与公开字段不因文档拆分而变化，完整验收状态见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。

本页负责 SecretReference 使用的业务 Provider 凭据。浏览器登录 token 由[认证设计](authentication.md)负责，模型连接配置属于 [Agent Runtime](agent-runtime.md)，外部写入批准属于[受控写入](repository-effects.md)。持有凭据不是放宽资源 scope、Tool 权限或批准条件的理由。

## 一个例子：保存成功不等于资源可用

ADMIN 为某个 Project 保存一个 MANAGED API key，只能说明平台保存了该引用和加密材料。Integration 还须指向同 Project、同 Provider 的有效引用，Run 使用冻结 binding；实际请求也可能因外部系统撤销 key 而失败。

```text
ADMIN 在资源表单输入凭据
        ↓
API 验证当前身份与 Project
        ↓
同一事务：引用 metadata + 加密材料
        ↓
公开响应：引用 ID / 状态，不返回原值

执行时：冻结 binding 的再验证
        ↓
Worker 解析凭据 → 受控 Provider
```

两段不是跨 API、Worker 和外部系统的一笔事务。创建时的成功、当前引用可用、实际外部访问成功分别观察；不通过读取 Secret 明文来判断资源就绪。

## 三种来源的边界

| resolver | 原值在哪里、平台保存什么 |
| --- | --- |
| ENVIRONMENT | 原值由部署环境提供；SecretReference 保存变量名等 metadata，应用 DB 不保存原值。环境、配置文件和宿主仍可能有副本 |
| FILE | 原值来自部署管理的文件；引用保存 /run/secrets 下的 locator。文件不是 Agent 的 input 或用户上传文档 |
| MANAGED | ADMIN 经受控表单/API 提交原值；应用 DB 分表保存 AES-256-GCM 密文，部署主密钥独立保管 |

ENVIRONMENT / FILE 的“零 at-rest”仅指应用 DB 不存凭据原值，不表示整个部署没有落盘。正式部署优先采用受控文件/Secret 注入；env_file 中的业务凭据属于兼容方式。通用 Settings 不支持任意同名 `_FILE` 自动替代；FILE resolver 和配置文件扩展不是一回事。

当前 FILE locator 的 domain 校验限制绝对路径在 /run/secrets 下并拒绝 `..`；resolver 使用普通 stat/read_text。这是词法检查，不是文件描述符级防 symlink/替换保证。部署方应独占管理该目录并最小化只读挂载；后续文件安全加固需覆盖最终目标、链接/替换竞争和权限，不能把 locator 合法视为内容可信。

解析上限是 65,536 UTF-8 bytes；FILE 会去掉末尾 CR/LF，ENVIRONMENT 不做相同处理。MANAGED 的创建 domain 另限制 8,192 bytes，不能拿底层 cipher/resolver 的较大上限当作公开上传额度。空值和超限均拒绝，不截断凭据继续调用。

## MANAGED 的实际加密结构

当前 [SecretCipher](../../PJM/backend/src/projectmind/core/secret_crypto.py)直接用 keyring 中的 32-byte 主密钥执行 AES-256-GCM，每次加密生成随机 nonce，并把 Project/引用身份作为 AAD。没有逐条生成 DEK，再用 KEK 包裹 DEK 的层级；[信封加密的 DEK/KEK 定义](https://docs.cloud.google.com/kms/docs/envelope-encryption)与这里的实际结构不同。

因此本文称为“应用层直接加密”。历史设置名 PROJECTMIND_MANAGED_SECRET_KEK、字段 kek_version、旧注释中的 envelope 继续兼容，不因正名重命名配置、改密文或补一套解密 fallback。若将来采用独立 DEK/KMS，须另行评审版本化格式、迁移与恢复，不能把它当作当前已接入能力。

| 保存对象 | 事实与限制 |
| --- | --- |
| 凭据引用 | `SecretReference` 保存 provider、resolver、locator、key_version 与 status；公开读模型不返回 locator 或原值。key_version 是引用 metadata，不是密文使用的 kek_version |
| 加密材料 | `ManagedSecretMaterial` 独立表保存 secret_reference_id、project_id、nonce、ciphertext、kek_version；不进入公开响应。分表降低误投影风险，不替代响应 allowlist 和泄漏测试 |
| 部署 keyring | 首项为新加密 active key，旧项用于解密；不写入业务 DB、Run snapshot、日志或 Agent 环境，恢复副本与 DB dump 分离保管 |
| AAD | 由固定格式与 project_id + secret_reference_id 重建；调换身份后解密失败，但不能替代访问前的 Project 授权 |

准备轮换时先确认[进程切换与恢复](#切换与恢复的顺序)，不要由版本标签或加密成功推导所有运行实例和旧备份都能读取。

### 明文与威胁边界

MANAGED 的管理员输入页面和 API 在创建时会接触明文，Worker/Provider 在使用时也会接触解密后的值；不能概括为“Web/API 永远看不到 Secret”。要求是只在必要边界短暂处理，不回显已有原值，不进入响应、业务日志、Evidence、Run workspace、Agent prompt 或子进程的业务凭据环境。

模型的专用连接凭据不属于 SecretReference，本页不把它与业务资源 Secret 混为一谈。SDK 的环境隔离继续由 [Claude adapter](../../PJM/backend/src/projectmind/agent/claude.py)负责；`secret_value` 等敏感 key 的过滤复用 [redaction](../../PJM/backend/src/projectmind/core/redaction.py)，但字段命中过滤不等于任意自由文本已脱敏。

该结构主要降低数据库或 DB 备份单独泄漏的风险；同时控制应用进程和 DB 的攻击者仍可能获得密钥与原值。外部 KMS/HSM 不属于现有实现，也不能单独阻止受控进程滥用已获授的解密权限。本项目不因这份设计新增 Vault、对外服务或专有 Traefik。

## 读取与失效

执行路径复用 [load_bound_run_resource / resolve_binding_secret](../../PJM/backend/src/projectmind/agent/run_binding.py)：先验证原 Run 的 binding、Integration 与 capability，再在同 Project 内取得 SecretReference，由 [DeploymentSecretResolver](../../PJM/backend/src/projectmind/integrations/secrets.py)取值。不把读取 locator/密文的内部 DTO 暴露给浏览器或模型。

- 引用停用、材料缺失、未知密钥版本、AAD/密文校验失败、文件不可读、空值或超限时拒绝解析。不能以空凭据、另一个引用或未加密存储自动降级。
- 停用引用会阻止后续解析，但已经解出的值、已发出的远端请求不会被追溯撤回。需要立即切断外部访问时，必须按外部系统和平台 incident 流程处置，不能把 DISABLED 当作进程停止证明。
- Run 冻结的是 binding/Integration 的身份与范围，不保存凭据原值。替换引用或连接 revision 不能改写原 Run 快照；仅对相同明文重加密，不应改变这些身份或扩大 scope。

## 轮换不是更换外部凭据

例子：把相同 API key 从主密钥版本 k1 重加密到 k2，是存储密钥轮换；到外部系统撤销旧 API key 并签发新 key，是业务凭据替换。前者既不改变外部权限，也不能代替后者。

当前 [rotate_managed_material](../../PJM/backend/src/projectmind/integrations/repository.py)在事务内锁定现有材料，将旧版本解密后用 active key 重加密。同 active version 的行只计为 skipped，**不会重新验证解密**。因此版本标签必须稳定指向同一 key；不能保留标签却替换 key bytes，也不能把“全 skipped”当作全部凭据可解密的证明。

### 切换与恢复的顺序

1. 确认环境和停写/维护窗口，分别保留 DB 恢复点与它需要的旧密钥；版本引用可登记，原值不进入普通清单。
2. 给所有必要读写进程加载“新 active + 旧解密 key”。API/Worker 在启动时构建 cipher；只改 CLI 的环境不会更新正在运行的实例。当前共享 env_file 还被 migrate 使用，实际暴露范围须按[环境配置边界](../operations/deployment.md#环境文件与配置边界)审查，不能声称只有两个进程持钥。
3. 在获批窗口运行既有轮换 CLI，检查退出状态与计数；密文写入者未统一到新 active 时不得宣称轮换结束，表行锁也不阻止另一个旧配置实例稍后再写旧版本。
4. 以受控方式验证所需解密与完整读取链路，再决定从现用 keyring 移除旧 key。没有现成的全量只读验证 CLI，不能用输出原值或直接查询密文作替代。
5. 只要保留的备份需要旧 key，就继续独立安全保管它。移出现用 keyring 不等于销毁唯一恢复副本；密钥丢失时 DB dump 本身不能恢复凭据。

实际命令、副作用和维护窗口要求集中在 [Runbook KEK 运维](../operations/runbook.md#88-managed-secret-の-kek-運用)。以上是操作前的设计条件，不声称已运行轮换、恢复演练或热更新。

## 开发接续与验收

创建规则与公开 field 由 [Secret 契约入口](../../PJM/contracts/README.md#認証と-secret-の契約を読む)连接，实现与测试由 [Backend 接续](../../PJM/backend/README.md#認証と-secret-の境界を追う)连接。来源安全、进程配置收敛和真实恢复继续登记在 R05/R11，不因 crypto 单测通过而完成。

| 给定场景 | 必须观察到的结果 |
| --- | --- |
| 非 ADMIN / 其他 Project 提交或读取引用 | 当前身份/Project 边界先拒绝；响应不包含 locator、原值或加密材料 |
| MANAGED 正常保存、错误 key/AAD/篡改 | 引用与材料同事务；错误拒绝解密且不回显内容，Secret 缺失不能冒充资源就绪 |
| FILE 链接、替换、不可读、空值或超限 | 不读取允许范围外内容，不截断后调用；当前普通文件读取的加固需专项实现与验证 |
| API/Worker/CLI 配置版本混合 | 未统一前不结束轮换；旧读者不误接新材料，失败不降级，受控回滚保留所需 key |
| active 标签相同、旧备份恢复 | skipped 计数不替代解密验证；备份与对应旧 key 能共同恢复，原 Run/binding 身份不变 |

本轮离线测试覆盖 helper、resolver/domain 与 fake repository/route；FILE 的实际读取/链接竞争、真实 DB、部署进程切换、浏览器明文处理、外部凭据替换和备份恢复未验收。当前证据见[交付记录 §73](../history/delivery-history.md#73-认证凭据与安全开发入口文档续整2026-09-09)。
