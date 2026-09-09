# Secret 保存、解析与轮换

本页负责外部 Provider 凭据；[认证](authentication.md)负责浏览器 token，[Runtime](agent-runtime.md)负责模型连接配置，[受控写入](repository-effects.md)负责批准。凭据可用不授予额外 scope/Tool 权限，当前缺口见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。

## 一个例子：保存成功不等于资源可用

ADMIN 保存 MANAGED key 时，引用与密文同事务提交，公开响应不返回原值。执行时 Worker 先验证冻结 binding，再解析给 Provider；外部 key 仍可能已撤销。保存、配置就绪、远端访问是三种事实，不是一笔跨系统事务。

## 三种来源的边界

| 来源 | 保存与约束 |
| --- | --- |
| ENVIRONMENT | DB 只存 locator，原值由部署环境提供；宿主/配置仍可能有副本 |
| FILE | 受控 /run/secrets 文件，不是用户上传或 Run input |
| MANAGED | ADMIN 提交明文，DB 独立表保存 AES-256-GCM 密文；部署密钥另保管 |

ENVIRONMENT/FILE 的“零 at-rest”仅指应用 DB 不存原值。正式部署优先受控文件注入；Settings 不支持任意 _FILE 自动替代，不能与 FILE resolver 混淆。

当前 FILE 仅词法限制绝对路径与 ..，普通 stat/read_text 不防 symlink/替换竞争。部署须独占管理、最小只读挂载；目标补最终文件、链接竞争与权限验证。

resolver 上限为 65,536 UTF-8 bytes，FILE 去末尾 CR/LF，ENVIRONMENT 不去；MANAGED 创建另限 8,192 bytes。空值/超限拒绝，不截断继续。

## MANAGED 的实际加密结构

[SecretCipher](../../PJM/backend/src/projectmind/core/secret_crypto.py)直接用 32-byte 主密钥执行 AES-256-GCM，每次随机 nonce，Project/引用身份作为 AAD；没有逐项 DEK 包装层，不称为信封加密。

既有设置 PROJECTMIND_MANAGED_SECRET_KEK 与 kek_version 名称保持兼容；更换 DEK/KMS 方案需另设计版本/迁移，不能据改名重写旧密文。

| 载体 | 边界 |
| --- | --- |
| SecretReference | provider/resolver/locator/key_version/status；公开不含 locator/原值 |
| ManagedSecretMaterial | 引用/Project、nonce、ciphertext、kek_version；引用 key_version 与密文版本不同 |
| keyring | 首项新加密 active，旧项解密；与 DB dump 分开保管，不入 Run/日志/Agent |
| AAD | 固定格式绑定 Project + 引用；不能代替访问授权 |

### 明文与威胁边界

创建时 ADMIN 页面/API、使用时 Worker/Provider 必须短暂接触明文；要求不回显、不入响应/日志/Evidence/workspace/prompt/业务子进程环境，而非声称 Web/API 从不接触。

该结构降低 DB 单独泄漏风险，不防同时控制应用进程与 DB 的攻击者。字段 redaction 也不证明任意自由文本已脱敏，不因此引入 Vault/KMS 或扩大服务暴露。

## 读取与失效

[run_binding](../../PJM/backend/src/projectmind/agent/run_binding.py)先重验 Run binding、Integration、capability 与同 Project 引用，再由 [DeploymentSecretResolver](../../PJM/backend/src/projectmind/integrations/secrets.py)解析。

停用、缺材料、未知密钥版、AAD/密文错误、不可读/空/超限均 fail closed，不换引用或空凭据降级。停用只阻止后续解析，不能收回已解出的值或已发送请求；立即切断外部访问另走 incident 流程。

Run 不存原值；替换引用/连接 revision 不改写旧 snapshot。相同明文重加密不应改变 binding 身份或 scope。

## 轮换不是更换外部凭据

存储密钥 k1 → k2 的重加密不改变外部 API key，更不撤销其权限。[rotate_managed_material](../../PJM/backend/src/projectmind/integrations/repository.py)锁定材料并事务重加密；active 标签相同的行直接 skipped，不验证解密。标签必须稳定对应同一 key bytes，不能将全 skipped 当作可读证明。

### 切换与恢复的顺序

1. 确认获批维护窗口，独立保留 DB 恢复点及对应旧 key，不在普通清单登记原值。
2. 所有读写进程加载“新 active + 旧解密 key”；cipher 在启动时构造，只改 CLI 环境不更新 API/Worker。按[环境边界](../operations/deployment.md#环境文件与配置边界)核对 migrate 等实际持钥者。
3. 运行既有 CLI 并检查退出/计数；旧实例仍可写旧版时不能宣称完成，行锁不防随后旧配置写入。
4. 受控验证必要解密与完整读取链后再移出现用旧 key；当前无全量只读验证 CLI，不输出原值作替代。
5. 只要备份仍需旧 key，就独立安全保管恢复副本；仅 DB dump 无法恢复丢失密钥。

命令集中在 [Runbook](../operations/runbook.md#managed-secret-の-kek-運用)，不承诺热更新或已演练。

## 开发接续与验收

[契约入口](../../PJM/README.md#contracts)与 [Backend](../../PJM/README.md#backend)连接现有实现；R05/R11 保留加固与部署缺口。

- 跨 Project/非 ADMIN 拒绝，响应不含 locator/原值/密文；引用与材料完整回滚。
- key/AAD/篡改、空值、文件不可读均安全拒绝；FILE symlink/替换需独立真实文件测试。
- API/Worker/CLI 混合配置不误接新材料、不降级；skipped 不替代解密验证。
- 旧备份与对应 key 能恢复，Run/binding 不变；crypto/helper 或 fake repository 测试不证明真实 DB、进程切换、Web 明文处理及恢复。
