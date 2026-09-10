# 登录入口防护与失败恢复

本页定义登录/本人改密的配额、短期状态与失败语义；身份见[认证](authentication.md)，改密事务见[用户管理](user-lifecycle.md)，缺口与操作分别见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)、[Runbook](../operations/runbook.md#登录防护的排查与恢复)。

## 一个例子：一次登录，两次入口请求

每次提交先 GET login-context、再 POST login：正常登录消耗两次来源配额、一次账号/组合配额。同组合窗口内第六次尝试拒绝；前五次不必都到验密，challenge 拒绝也可能已计数，正确密码不豁免。换来源不清账号累计，换账号不清来源累计，NAT 共享来源额度；手动重试取新 challenge，不自动重发密码。

## 保护范围与执行顺序

middleware 按路径保护 login-context、login、users/me/password；错误方法、尾斜线、非法正文也先计来源，重定向前后分别计数。

```text
来源 gate → 路由/正文/header
  ├─ login-context → 保存 challenge
  └─ login → Origin → 标准化 email → 账号/组合 gate
               → 消费 challenge → 密码/新会话
```

正文/header/Origin 拒绝先于账号计数；可标准化 email 在 challenge 前计数，不查询账号存在性绕过 gate。无效 email 不建账号 key，但来源已计数，仍消费 challenge 并 dummy 验密。

LoginAdmission 是同一 AuthService 的一次性内存回执，30 秒失效，不公开、不证明验密通过；body/连接/超时仍由 ingress 限制。

本人改密沿“来源 → 请求/Origin/会话 CSRF → actor 账号/组合计数 → 管理事务验密”，与登录共享额度；不用匿名 challenge、不接受另传 email，错误按[管理协议](user-lifecycle.md#生效与界面)处理。

## 计数与退避如何恢复

| 维度 | 默认窗口 / 上限 |
| --- | --- |
| 来源 | 60 秒 / 100 个入口 HTTP 请求 |
| 标准化账号 | 60 秒 / 15 次尝试，跨来源、登录/改密共享 |
| 账号 + 来源 | 60 秒 / 5 次尝试，登录/改密共享 |

窗口从首次计数开始，不是自然分钟或滑动窗口。超限退避依次 60、120、240、300 秒；安静至少 900 秒才于下次检查重置升级次数，状态最长保留到最后写入后 1260 秒。

阻塞中的单次检查不更新计数/退避/TTL，但账号拒绝前的来源 gate 可能已写入。成功也不清零，避免抹掉其他在途计数。这是尝试限流而非错误密码累计；NAT、持续攻击和账号维度仍可能误伤，不替代分布式 DoS 防护。策略取舍见 [OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#login-throttling)。

## 短期状态与失败关闭

[LoginProtection](../../SKM/backend/src/skillmind/auth/login_protection.py)使用 Redis TIME；来源与账号/组合各一次 EVAL，以 SET PX 写有限 TTL。它们不与 PostgreSQL 原子提交，Lua 错误也不保证回滚已写状态。

| 情况 | 处理 |
| --- | --- |
| key 缺失/过期 | 新窗口；无法区分驱逐/重启丢失，累计防护可能削弱 |
| 格式/版本/类型错、无 TTL | 拒绝，不删除重置 |
| EVAL 超时/错误/异常结果 | 503，不验密、不自动重试或补偿计数 |
| challenge 保存/消费未知 | 503，不发新 challenge、不验密；明确缺失/过期/已消费为 403 |
| 防护故障但已有会话 | 会话读取/登出仍独立检查 DB/CSRF |

key 使用 skillmind:auth:limit:v2: 及 email/来源 SHA-256，不公开内部状态。hash 可被低熵枚举，不是匿名化；hash tag 不证明 Redis Cluster 支持，Lua 原子性不证明故障切换 exactly-once。

## 公开响应与客户端责任

沿用 [Problem](../../SKM/contracts/errors/problem/v1.schema.json)，不暴露维度、email、IP 或回执。已处理的受保护响应有 no-store 和服务器 request_id，不涵盖代理/未处理异常。

| 返回 | 客户端含义 |
| --- | --- |
| 429 login_rate_limited | 本次未获验密许可；Retry-After 1–300 秒，等完不保证通过 |
| 503 login_protection_unavailable | 防护未确认、未验密；不代表账号停用或承诺恢复时间 |
| 401 invalid_credentials | 不区分不存在、停用、密码错误 |
| 403/422/405 | Origin/challenge、结构或方法错误；来源可能已消耗，不优先于 gate |
| 改密 400 current_password_rejected | 保留仍有效会话 |

[HTTP client](../../SKM/web/src/api/http.ts)只接非负安全整数秒为 retryAfterSeconds，非 JSON 错误也保留 HTTP 元数据；日期、小数、溢出不猜测。[loginFeedback](../../SKM/web/src/lib/loginFeedback.ts)再限制为 1–300。

LoginPage 对合法 429 显示静态“至少等 N 秒后手动重试”，无效 header 用通用提示；不是倒计时/本地锁定，503 不套等待 header。提交错误走三语 catalog、不显示原 message；App 初始会话检查仍是独立字符串路径。

## 提交、离页与结果未知

[LoginPage](../../SKM/web/src/pages/LoginPage.tsx)以 activeRequest ref 防同 tick 双提交，离页 abort/清引用；[auth client](../../SKM/web/src/api/auth.ts)在 challenge 与密码 POST 之间复查取消。只有有效请求可回调，busy/aria-busy 不构成跨 tab 互斥。

密码 POST 发出后的断连、abort、解析失败不证明 Session 未提交或 cookie 未接收。应读当前会话确认，不重发密码、不在 cleanup 自动 logout。登录没有原请求幂等重放，完整跨页未知恢复尚未实现。

## 来源识别与上线边界

来源取 ASGI request.client，合并 IPv4/mapped IPv6，无法识别入 unknown 桶；不自行信任 Forwarded。须验证 Traefik/ASGI 拓扑：信任过窄共享代理 IP，过宽可伪造，不能关闭 Origin/CSRF 或扩大信任来修复。

[Settings](../../SKM/backend/src/skillmind/core/settings.py)→ .env.example → lifespan 定义上限与 Redis timeout（默认 5/15/100、2 秒）。timeout 不是 HTTP 总 deadline；窗口/退避不是环境变量策略，各 API 实例保持一致。

limiter namespace v2、状态 JSON v1、session v2 各自独立。新 limiter 不读旧 key；切换评审累计丢失与混跑绕过，不批删 Redis、queue 或 challenge。

## 开发接续与验收

重点覆盖拒绝顺序、共享计数/有限退避、损坏/未知 fail closed、已有会话独立可用与密码错误不误注销。[表单回归](../../SKM/web/tests/browser/check_login.py)检查重复/卸载及三语/键盘/窄屏；[真实 Lua](../../SKM/backend/tests/auth/test_real_login_protection.py)和 [ASGI + Redis](../../SKM/backend/tests/auth/test_real_login_http.py)分别检查状态与顺序，后者不证明网络服务或真实会话提交。

真实 PostgreSQL、HTTPS 多实例、NAT、多页面及 Redis 容量/驱逐/重启/切换须独立验收；按[本地指南](../development/local-development.md)使用获准专用目标，不对用户 Redis 做故障实验。
