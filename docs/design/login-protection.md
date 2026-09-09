# 登录入口防护与失败恢复

本页负责登录/本人改密的配额、短期状态和失败语义；[认证](authentication.md)负责身份，[用户管理](user-lifecycle.md)负责改密事务。当前缺口见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)，现场操作见[Runbook](../operations/runbook.md#登录防护的排查与恢复)。

## 一个例子：一次登录，两次入口请求

Web 每次提交先 GET login-context，再 POST login：正常登录占两次来源配额，一次账号/组合配额。同组合窗口内第六次尝试被拒绝，前五次不必都到密码验证，challenge 拒绝也可能已计数；正确密码不绕过限制。

换来源不清账号累计，换账号不清来源累计；NAT 用户共享来源额度。手动重试取新 challenge，不自动重发密码。

## 保护范围与执行顺序

middleware 按路径覆盖 login-context、login、users/me/password；错误方法、尾斜线、非法正文也先过来源 gate。

```text
来源检查 → 路由/正文/header
  ├─ login-context → 保存 challenge
  └─ login → Origin → 标准化 email → 账号/组合检查
               → 消费 challenge → 密码/新会话
```

正文/header/Origin 拒绝在账号计数前。可标准化 email 在 challenge 前计数，不查询账号存在性绕过 gate；无效 email 不建账号 key，但来源已计数，仍消费 challenge 并 dummy 验密。尾斜线重定向与后续请求分别计来源。

LoginAdmission 是同一 AuthService 一次性内存回执，30 秒失效，不公开、不证明密码通过。网络 body/连接/超时仍需 ingress 限制。

本人改密：来源 → 请求/Origin/会话 CSRF → 从 actor 取账号计数 → 管理事务验密。它不消费匿名 challenge、不接受另传 email，与登录共享额度；[管理错误](user-lifecycle.md#生效与界面)不能套用匿名登录的全部 401 逻辑。

## 计数与退避如何恢复

| 维度 | 默认窗口与上限 |
| --- | --- |
| 来源 | 60 秒 / 100 个入口 HTTP 请求 |
| 标准化账号 | 60 秒 / 15 次尝试，跨来源与登录/改密共享 |
| 账号 + 来源 | 60 秒 / 5 次尝试，登录/改密共享 |

窗口从首次计数开始，不是自然分钟/滑动窗口。超过上限的请求拒绝并退避 60、120、240、300 秒，封顶 300；等待结束不立即忘掉升级次数。安静至少 900 秒才于下次检查重置，状态最长到最后写入后 1260 秒。

阻塞中的单次 Redis 检查不更新其计数、退避或 TTL；但账号拒绝前独立来源检查可能已写入。成功也不清零，避免抹掉其他在途计数；这是尝试限流，不只是错误密码累计。

账号/来源/组合同时限制覆盖常见绕过，但持续攻击、NAT 与账号维度仍可能误伤；不是分布式 DoS 防御。取舍参考 [OWASP 登录限流](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#login-throttling)，数值是项目策略。

## 短期状态与失败关闭

[LoginProtection](../../PJM/backend/src/projectmind/auth/login_protection.py)用 Redis TIME；来源一次 EVAL，账号/组合另一次 EVAL，各写入以 SET PX 设置有限 TTL。它们不与 PostgreSQL 会话/账户事务原子提交，Lua 执行错误也不保证回滚先前写入。

| 情况 | 处理 |
| --- | --- |
| key 缺失/过期 | 新窗口；驱逐/重启丢失无法与正常过期区分，会削弱累计防护 |
| 格式/版本/类型错或无 TTL | 拒绝，不自动删除重置 |
| EVAL 超时/错误/异常结果 | 503，未进入验密；业务层不自动重试或补偿计数 |
| challenge 保存/消费未知 | 503，不发新 challenge/不继续验密；明确缺失、过期、已消费为 403 |
| 防护故障但会话已存在 | 读会话/登出仍独立检查 DB 与 CSRF，不因此全部阻断 |

key 使用 projectmind:auth:limit:v2: 与 email/来源 SHA-256，不公开 key/状态；hash 不是匿名化，低熵值可被枚举。账号/组合 hash tag 不证明 Redis Cluster 支持，原子脚本不证明故障切换 exactly-once。

## 公开响应与客户端责任

沿用 [Problem Schema](../../PJM/contracts/errors/problem/v1.schema.json)，不公开触发维度、email、IP 或内部回执。受保护路径的已处理响应含 no-store 和服务器 request_id；不外推到代理/未处理异常。

| 返回 | 含义与客户端处理 |
| --- | --- |
| 429 login_rate_limited | 未获本次验密许可；Retry-After 为 1–300 秒，等完不保证通过 |
| 503 login_protection_unavailable | 防护无法确认，未验密；不承诺恢复时间或等同账号停用 |
| 401 invalid_credentials | 登录凭据失败，不区分账号不存在/停用/密码错误 |
| 403/422/405 | Origin/challenge、结构或方法问题；来源可能已消耗，错误不优先于 gate |
| 改密 400 current_password_rejected | 当前密码错误，不注销仍有效会话 |

[HTTP client](../../PJM/web/src/api/http.ts)仅将非负安全整数的秒数 header 保存为 retryAfterSeconds；日期、小数、溢出不猜测转换，非 JSON 错误也保留 HTTP 元数据。[loginFeedback](../../PJM/web/src/lib/loginFeedback.ts)另限制登录范围为 1–300。

LoginPage 当前对合法 429 显示静态“至少等 N 秒后手动重试”，不是倒计时或按钮本地锁定；无效 header 用通用提示，503 不套等待 header。提交错误经 zh/ja/en catalog，不直接展示原 message；App 初始会话检查仍是另一条字符串路径。

## 提交、离页与结果未知

[LoginPage](../../PJM/web/src/pages/LoginPage.tsx)用 activeRequest ref 防同 tick 双提交，离页 abort 并清引用；[auth client](../../PJM/web/src/api/auth.ts)在 challenge 与密码 POST 之间检查取消。只在请求仍有效时回调，busy/aria-busy 提示在途，不构成多 tab 全局互斥。

密码 POST 已发出后，断连/abort/解析失败不能证明 Session 未提交或 cookie 未接收。此类未知不同于明确 429/503；确认应读当前会话，不重发密码，不在 cleanup 自动 logout。登录没有原请求幂等重放，跨页协调/完整未知恢复仍待实现。

## 来源识别与上线边界

来源取 ASGI request.client；IPv4 与 mapped IPv6 合并，无法识别入 unknown 桶，不自行信任 Forwarded。实际 Traefik/ASGI 拓扑必须验证，信任过窄会共享代理 IP，过宽可伪造；不能通过扩大代理信任或关闭 Origin/CSRF 解决。

Settings → .env.example → lifespan 已接组合/账号/来源上限与 Redis 调用 timeout：默认分别 5/15/100、2 秒；允许范围以 [Settings](../../PJM/backend/src/projectmind/core/settings.py)为准。timeout 不等于整个 HTTP deadline，窗口/退避不是环境变量策略，所有 API 实例保持一致。

limiter namespace v2、状态 JSON v1、session 凭据 v2 是不同版本。新 limiter 不读旧 key；切换需评审旧累计丢失与混跑绕过，不批删 Redis、queue 或 challenge。容量/驱逐/重启/切换与恢复独立验收。

## 开发接续与验收

- 非法正文/header/Origin/方法/尾斜线仍先计来源；并发、换来源/账号触发共享维度。
- 连续超限、安静期和成功登录不破坏有限退避；损坏/无 TTL/未知结果 fail closed。
- 已有会话在防护故障时按自身协议读/登出；密码错误不误注销。
- mock Web 与[真实表单回归](../../PJM/web/tests/browser/check_login.py)验证重复/卸载、三语/键盘/窄屏、429/503，不证明真实 cookie/DB。
- [实际 Lua](../../PJM/backend/tests/auth/test_real_login_protection.py)与 [ASGI + Redis](../../PJM/backend/tests/auth/test_real_login_http.py)分别证明状态和拒绝顺序；后者不启动网络 server，也不验证真实会话提交。
- 专用隔离 Redis、真实 PostgreSQL、HTTPS 多实例、NAT、多页面与 Redis 切换分别验收；按[本地指南](../development/local-development.md)准备，不连接用户项目 Redis 做故障实验。
