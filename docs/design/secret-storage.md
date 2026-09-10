# Secret 保存、解析与轮换

本页负责 Provider 凭据；浏览器 token、模型连接、批准分别见[认证](authentication.md)、[Runtime](agent-runtime.md)、[受控写入](repository-effects.md)。Secret 可用不增加 scope/Tool 权限，缺口见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。

## 一个例子：保存成功不等于资源可用

ADMIN 保存 MANAGED 引用/密文同事务提交，响应不返原值；Worker 验冻结 binding 后解析给 Provider，外部 key 仍可能已撤销。保存、配置就绪、远端可达不是同一事实或跨系统事务。

## 三种来源的边界

| 来源 | 保存与约束 |
| --- | --- |
| ENVIRONMENT | DB 只存 locator，原值来自部署环境，宿主/配置仍可能有副本 |
| FILE | 受控 /run/secrets 文件，非上传或 Run input |
| MANAGED | ADMIN 提交明文，DB 独立保存 AES-256-GCM 密文，密钥另保管 |

ENVIRONMENT/FILE 的“零 at-rest”仅指 DB 不存原值；部署优先只读文件注入，Settings 并无任意 _FILE 自动替代。

FILE 当前只词法限制绝对路径/..，stat/read_text 不防 symlink/替换竞争；部署须独占管理、最小只读挂载，最终文件/链接/权限核验仍待补。

resolver 最多 65,536 UTF-8 bytes；FILE 去末尾 CR/LF，ENVIRONMENT 不去，MANAGED 创建另限 8,192 bytes。空值/超限拒绝、不截断。

## MANAGED 的实际加密结构

[SecretCipher](../../SKM/backend/src/skillmind/core/secret_crypto.py)以 32-byte 主密钥直接 AES-256-GCM，加随机 nonce、Project/引用 AAD；无逐项 DEK，不称信封加密。SKILLMIND_MANAGED_SECRET_KEK 与 kek_version 名称保持兼容；DEK/KMS 另定版本/迁移，不因改名重写密文。

| 载体 | 边界 |
| --- | --- |
| SecretReference | provider/resolver/locator/key_version/status，公开无 locator/原值 |
| ManagedSecretMaterial | 引用/Project、nonce/ciphertext/kek_version；与引用 key_version 不同层 |
| keyring | 首项 active 加密、旧项解密，与 DB dump 分开保管，不进 Run/日志/Agent |
| AAD | 固定格式绑定 Project/引用，不代替访问授权 |

### 明文与威胁边界

ADMIN 页面/API 创建、Worker/Provider 使用时必须短暂接触明文，但不回显或进入响应、日志、Evidence、workspace、prompt、业务子进程环境。加密降低 DB 单独泄漏风险，不防同时控制应用/DB；字段 redaction 不保证自由文本脱敏，不据此扩服务暴露。

## 读取与失效

[run_binding](../../SKM/backend/src/skillmind/agent/run_binding.py)先验 Run binding、Integration/capability、同 Project 引用，再由 [resolver](../../SKM/backend/src/skillmind/integrations/secrets.py)解析。

停用、材料缺失、未知 key 版、AAD/密文错误、不可读/空/超限均 fail closed，不换引用或空凭据降级。停用仅阻止后续解析，不能收回已解析值/已发请求；外部立即撤权另走 incident 流程。

Run 不存原值，换引用/连接 revision 不改旧 snapshot；同明文重加密不改变 binding 身份/scope。

## 轮换不是更换外部凭据

k1 → k2 重加密不更换或撤销外部 API key。[rotate_managed_material](../../SKM/backend/src/skillmind/integrations/repository.py)锁材料并事务重加密；active 标签相同直接 skipped、不验解密，标签必须稳定对应同一 key bytes，全 skipped 不是可读证明。

### 切换与恢复的顺序

1. 获准维护窗口内，分别保留 DB 恢复点与对应旧 key，不在普通清单登记原值。
2. 所有读写进程加载“新 active + 旧解密 key”；cipher 启动时构造，改 CLI 环境不更新 API/Worker。按[环境边界](../operations/deployment.md#环境文件与配置边界)核对 migrate 等实际持钥者。
3. 执行既有 CLI、核对退出/计数；旧实例仍写旧版时不算完成，行锁不阻止随后旧配置写入。
4. 受控验必要解密及完整读取链，再移出现用旧 key；尚无全量只读验证 CLI，不输出明文代验。
5. 备份仍依赖旧 key 时独立保管恢复副本；仅 DB dump 无法恢复丢失密钥。

命令见 [Runbook](../operations/runbook.md#managed-secret-の-kek-運用)，不承诺热更新。

## 开发接续与验收

入口见[Backend](../../SKM/README.md#backend)/[契约](../../SKM/README.md#contracts)。验证跨 Project/非 ADMIN 拒绝、DTO 无泄漏、引用/材料回滚、key/AAD/篡改/空值失败关闭，以及 FILE 链接竞争、API/Worker/CLI 混合配置和旧备份恢复。

crypto/helper 或 fake repository 不证明真实事务、进程切换、Web 明文处理和恢复；R05/R11 的实环境缺口保留。
