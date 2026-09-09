# 登录入口防护与失败恢复

> 定位：登录与本人改密共用的请求配额、短期状态和失败语义正本。实现、契约同步和部署验收的状态见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)。本页区分服务端拒绝、客户端反馈和服务端结果未知，不把它们合成一个“登录失败”。

本页回答“什么时候允许进入密码验证”，不定义账号是否有权访问 Project。[认证与会话](authentication.md)负责密码、会话和授权，[Secret 设计](secret-storage.md)负责外部凭据，现场故障使用[登录防护分诊](../operations/runbook.md#登录防护的排查与恢复)。配额不是账号状态，也不是永久撤销记录。

## 一个例子：一次登录，两次入口请求

Web 每次提交先取 challenge，再提交密码。默认配置下，同一个来源的一次正常登录占用两次来源配额，但只占用一次账号及组合配额：

| 请求 | 经过哪些检查 |
| --- | --- |
| GET login-context | 来源计数 → 创建短时 challenge |
| POST login | 来源计数 → 请求与 Origin → 账号及组合计数 → challenge → 密码与新会话 |

同一账号、同一来源在一个观察窗口内已有 5 次通过账号/组合配额检查的尝试，第 6 次返回 429；前五次不必都到达密码验证，challenge 拒绝也可能已经计数。正确密码不绕过限制。等到允许再次尝试时，仍须取得新 challenge，不自动重发密码。

两个常见误读：换来源不能清掉账号维度的累计；换账号也不能清掉来源维度的累计。来源上限按 HTTP 请求而非“点击登录次数”计算，共享出口的多个用户会共用它。具体默认值是项目策略，不是每个部署都适用的安全常数。

## 保护范围与执行顺序

当前 middleware 的来源 gate 覆盖 `/api/v1/auth/login-context`、`/api/v1/auth/login` 和 `/api/v1/users/me/password`，外部入口由既有 context path 规则映射。它按路径而非仅按方法识别，因此这些路径上的错误方法、尾斜线和不合法正文也先经过来源检查。

下面是登录链；已登录改密不消费匿名 challenge，另按表后的接线说明判断：

```text
进入受保护路径
  ↓
来源检查 ── 拒绝 / 无法确认 → 429 / 503
  ↓
路由、正文/header 校验
  ├─ login-context → 保存 challenge
  └─ login → Origin → email 标准化
                        ↓
                   账号 + 组合检查
                        ↓
                  消费 challenge
                        ↓
                   密码 / 新会话
```

流程中的每一步都可能结束请求，并非所有失败都应返回 401：

- 正文/header 校验和 Origin 拒绝发生在来源检查之后、账号计数之前；错误方法也可能在来源检查后返回 405。
- 能标准化的 email 在 challenge 检查前计入账号和组合；账号不存在、已停用或密码正确都不改变计数规则。正在限流时不会先查数据库判断账号是否存在。
- 无效 email 不创建账号 key，但已经计入来源；当前服务仍消费 challenge，再做 dummy password 验证并返回通用凭据错误。错误 challenge 会更早拒绝。
- 尾斜线的重定向请求与随后真正抵达的请求分别计入来源，不把重定向当成免费入口。

来源通过后产生的 `LoginAdmission` 只是在同一 AuthService 内使用一次的内存回执，30 秒后失效；不交给浏览器，不是额外登录 token，也不证明密码已经验证。长时间读取正文仍需要 ingress 的大小、连接和超时限制，这个回执不承担网络层防护。

本人改密已[复用同一配额](user-lifecycle.md#改密入口的配额)：来源通过后，route 验证请求及 Origin/会话 CSRF，调用 admit_password_change 从当前 actor 取得账号，再进入管理用例。它不取匿名 challenge，也不能用请求中的另一个 email 替换账号。一次改密通常占一次来源及一次账号/组合额度，和登录共享累计；来源或前置校验拒绝时可能尚未计入账号。

这是 Backend 工作副本的接线，不是账户 Web、真实 Redis/数据库或部署已验收。改密错误使用[账户错误语义](user-lifecycle.md#生效与界面)，不能直接套用 LoginPage 的 401 凭据错误逻辑。

## 计数与退避如何恢复

三个维度使用相同算法，窗口按该状态的首次计数开始，不是自然分钟，也不是滑动窗口。

| 维度 | 当前默认策略与计数对象 |
| --- | --- |
| 来源 | 每 60 秒 100 个入口 HTTP 请求，包括 challenge、登录与本人改密 |
| 账号 | 每 60 秒 15 次标准化账号的密码验证尝试，登录/改密与不同来源共用 |
| 账号 + 来源 | 每 60 秒 5 次相同组合的尝试，登录/改密共用 |

超过上限的那次请求被拒绝，并设置退避；后续超过上限时依次为 60、120、240、300 秒，之后封顶 300 秒。单纯等完一次退避不会立即忘掉此前的升级次数。某维度距离最后一次状态更新至少 900 秒后，下次检查才将其按安静期重置；状态自身最长保留至最后写入后 1260 秒。

“不延长封禁”要按检查阶段理解：

- 某次 Redis 检查发现仍在退避时，只返回剩余秒数，不更新该次检查涉及的计数、退避或 TTL。
- 账号/组合被拒绝前，独立的来源检查可能已计数；不能承诺“返回 429 的请求完全没有写入”。
- 成功登录或改密也不删除任何维度的状态，避免清掉其他在途尝试。成功与失败的尝试都可能推动尚未阻塞的计数，不能将该策略称为“只累计错误密码”。

选择账号、来源和组合共同限制，是为了覆盖换 IP 与换账号两种绕过；有限退避保留正常用户恢复机会。但持续攻击仍可再次触发限制，账号维度和共享出口也有误伤风险。上线必须结合实际并发、NAT 与安全运营评估，不能宣称“不会造成拒绝服务”。依据是 OWASP 的[登录限流与账号锁定取舍](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#login-throttling)，上述数值和成功计数规则为项目自己的决定。

## 短期状态与失败关闭

当前实现位于 [LoginProtection](../../PJM/backend/src/projectmind/auth/login_protection.py)。以 Redis TIME 作为配额计时基准；来源单独执行一次 EVAL，账号和组合在另一次 EVAL 中共同判断与更新。两次 EVAL 与 PostgreSQL 会话创建或账户变更不组成一个跨系统事务。

每个状态写入都使用带 PX 的 SET，避免计数成功但另一次 EXPIRE 未执行而留下无期限 key。EVAL 内部不被其他命令交错，不代表执行错误会回滚先前写入；若结果不明，本次登录拒绝而不尝试补偿扣减。原子执行与过期参数分别见 [Redis Lua](https://redis.io/docs/latest/develop/programmability/eval-intro/) 和 [SET](https://redis.io/docs/latest/commands/set/)。这不是 Redis 故障切换、客户端传输重发或跨存储 exactly-once 的证明。

| 状态或故障 | 当前处理与不能推出的保证 |
| --- | --- |
| key 缺失或正常过期 | 以新窗口开始；不能区分正常过期、驱逐、重启丢失或人工删除，因此 Redis 数据丢失会削弱累计防护 |
| 格式/版本错误、无 TTL、错误类型 | 拒绝登录，不删除后重新初始化；已损坏的无 TTL key 也不会被自动修复 |
| EVAL 出错、超时或返回值不合法 | 返回 503，不继续验证密码，不在业务层自动重试 EVAL |
| challenge 保存未确认 | SET 出错、超时或未明确返回成功时为 503，不向浏览器发出新 challenge |
| challenge 消费未确认 | GETDEL 出错、超时或读到非预期标记时为 503，不继续验证密码；缺失、过期、已消费仍按 403 处理 |
| Redis 登录防护不可用 | 不因这一原因阻止已有会话的读取/登出；它们仍须独立通过数据库会话与 CSRF 检查 |

key 使用 `projectmind:auth:limit:v2:` 命名空间和 email/来源的 SHA-256，不直接包含原值。hash 是可关联标识，不是匿名化或加密，低熵 email/IP 仍可能被枚举推测；不将 key 或完整状态暴露到公开 Problem、Web 或普通日志。账号与组合使用同一 hash tag，并不说明现有 client 已支持 Redis Cluster。

## 公开响应与客户端责任

以下是工作副本的服务端行为。公开同步与证据统一见[计划 R05](../planning/roadmap.md#r05-领域与身份安全)，不要从已有 Web 文案推导 OpenAPI 或部署已同步。JSON 字段仍使用既有 [Problem Schema](../../PJM/contracts/errors/problem/v1.schema.json)，不新增限流维度、email、IP 或内部回执字段。

当前[OpenAPI](../../PJM/contracts/openapi/projectmind-api.v1.json)已声明登录 429/503 的 Problem body、正确媒体类型与响应 header；认证相关的 401/403/422 同样按实际 handler 声明。共享定义与既有 Schema 做完全一致性检查，成功响应仍是 application/json。此同步不证明所有其他业务接口已完成逐字段审计。

| 返回 | 客户端应该如何理解 |
| --- | --- |
| 429 等待 | `login_rate_limited`：本次未获密码验证许可，名称在改密入口也保持不变；Retry-After 是 1–300 的等待秒数，不暴露哪个维度触发，也不承诺等待后一定通过 |
| 503 不可用 | `login_protection_unavailable`：无法确认短期防护，本次没有进入密码验证；不是账号停用或凭据错误。当前不返回恢复时间承诺 |
| 401 凭据错误 | `invalid_credentials`：已走到凭据检查但认证失败；不区分不存在、停用、密码错误或无效 email |
| 403 / 422 / 405 | Origin/challenge、请求结构或方法不合法；来源配额可能已经消耗，不保证这些错误优先于 429/503 |

表中的 invalid_credentials 与匿名 challenge 拒绝属于登录；改密的当前密码错误是 400 current_password_rejected，有效会话不因此被注销。两条路径复用 429/503 及 HTTP 元数据，不复用全部身份错误和界面反馈。

入口中间件对受保护路径的处理结果设置 `Cache-Control: no-store`，即使还未匹配到 auth route；已处理的 429/503 同样包含服务器生成的 request_id。不把这一点推广到未处理异常、代理自产响应或全站缓存策略。

HTTP 的 Retry-After 可以使用日期或秒数，但本接口选择秒数形式；语义依据 [RFC 9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)。[Web HTTP client](../../PJM/web/src/api/http.ts)已保留秒数到 ApiProblemError.retryAfterSeconds；只接受数字形式且能无损表示为非负安全整数的值，日期、负数、小数和溢出值不猜测转换。错误正文不是 JSON 时，仍保留 HTTP status 和合法 header。

共享解析器只判断语法；[loginFeedback](../../PJM/web/src/lib/loginFeedback.ts)再判断登录策略范围。这一分工避免把所有 API 的 Retry-After 都限定为登录上限。

| 页面收到什么 | 当前提交错误的反馈 |
| --- | --- |
| 429 + 合法等待 | 只有 1–300 秒显示“至少等待 N 秒后手动重试”；当前是静态提示，没有倒计时或到时自动提交 |
| 429 + 其他 header | 缺失、不合法或超出登录范围，显示通用限流提示；不伪造默认秒数 |
| 503 | 显示通用“登录服务暂不可用”。无论是否有 Problem code，都不据此断言一定是 Redis 故障，也不套用其等待 header |
| 已知认证拒绝 | 401 invalid_credentials 与 403 csrf_rejected 使用各自文案；其他错误使用通用失败提示 |

上述反馈通过 zh/ja/en catalog 提供，不直接展示本次提交异常的原 message。LoginPage 保存错误原因，在渲染时按当前 catalog 生成文字；App 首次会话检查传入的 initialError 仍是另一条字符串路径，不属于这套反馈保证。页面语言选择、多页会话和真实交互的证据按各自范围验收。

## 提交、离页与结果未知

当前 [LoginPage](../../PJM/web/src/pages/LoginPage.tsx)以一个 activeRequest ref 守住同一表单的在途请求，[auth client](../../PJM/web/src/api/auth.ts)在两次 HTTP 调用之间检查 abort。它们的职责如下：

```text
提交表单 → 登记在途
  ↓
获取 challenge
  ├─ 失败或已取消 → 停止
  └─ 有效且未取消 → 提交密码
  ↓
响应或异常返回
  ├─ 请求仍有效 → 成功交 App / 失败提示
  └─ 已离页     → 忽略，不回调

离页同时：abort + 清除在途引用
```

同一轮渲染内再次 submit 也先检查 ref，不只依赖下一轮渲染才生效的 disabled；busy/aria-busy 负责提示正在提交。收到错误后会释放在途状态，429 提示不是本地强制锁定按钮，配额仍由服务端决定。手动重试从新 challenge 开始，不自动发送密码、不轮询 challenge；这只约束当前表单，不是多 tab 的全局互斥。

离页保护解决的是“旧请求不再驱动页面”，不是“撤销服务器工作”。从 [AuthService](../../PJM/backend/src/projectmind/auth/service.py)先提交 Session、[route](../../PJM/backend/src/projectmind/api/routes/auth.py)再构造 cookie 响应的顺序可知：密码 POST 发出后发生断连、abort 或响应解析失败，不能断言服务端没有创建会话，也不能保证浏览器未接收 cookie。

这类结果未知不等同于已收到的 429/503 拒绝；后续需要核对当前会话时使用普通会话读取流程，不靠重发密码确认原结果。现行登录没有 Run 创建式的 Idempotency-Key 重放协议，新一次密码提交可能创建新会话。不得在 cleanup 中自动 logout 作“补偿”，以免撤销另一个页面刚建立的会话。跨页协调和结果未知恢复的完整体验仍需专门设计与验证。

## 来源识别与上线边界

来源取自 ASGI request.client，应用不自行相信任意 Forwarded / X-Forwarded-For。IP 规范化会合并 IPv4 与其 IPv4-mapped IPv6 表达；无法识别的地址统一进入 unknown 桶。不同 IPv6 地址、多个真实出口和大量不同账号仍可扩大总请求量，不能将此方案称为分布式 DoS 防御。

真实来源取决于前级 proxy 的信任配置：未正确恢复地址可能把所有人合并到代理 IP，信任过宽又可能允许伪造。以实际 Traefik/ASGI 拓扑验证，不能通过信任所有代理、关闭 Origin/CSRF 或增加旁路登录解决误限流。

当前 Settings → `.env.example` → API lifespan 已有以下配置接线；所有 API 实例应保持同一策略，不能靠逐实例配置不同数值试运行：

- `PROJECTMIND_AUTH_LOGIN_ATTEMPTS_PER_MINUTE`：组合上限，默认 5，允许 1–30。
- `PROJECTMIND_AUTH_LOGIN_ACCOUNT_ATTEMPTS_PER_MINUTE`：账号上限，默认 15，允许 1–100。
- `PROJECTMIND_AUTH_LOGIN_SOURCE_REQUESTS_PER_MINUTE`：来源请求上限，默认 100，允许 2–10000。
- `PROJECTMIND_AUTH_LOGIN_PROTECTION_TIMEOUT_SECONDS`：每次受保护 Redis 调用的等待上限，默认 2 秒，必须大于 0 且不超过 10 秒；不是整个 HTTP 请求的 deadline。

窗口、退避、安静期和状态结构目前属于代码内策略，不能仅调环境变量就改变它们。Redis limiter 的命名空间 v2、状态 JSON version 1 与会话凭据 v2 是三个不同的版本概念；本次限流不新增数据库 migration，也不替代 [0031 会话切换](authentication.md#会话凭据-v2-与切换要求)。

旧组合限流 key 不被新实现读取。全部 API 切换前须评审旧累计失去作用和混合版本的绕过风险；不在文档操作中批量删除 Redis，更不清队列、challenge 或会话审计来“恢复登录”。Redis 容量、驱逐策略、重启/切换、监控与受控恢复仍是部署验收项。

## 开发接续与验收

先沿 [Backend 登录防护入口](../../PJM/backend/README.md#ログイン入口の防護を追う)核对 middleware → service → Redis → Problem，再沿 [Web 入口](../../PJM/web/README.md#ログインと書込失敗を切り分ける)复用已有 HTTP header、错误反馈与在途控制。公开同步、真实交互与环境验收分层接续，不重新引入 INCR/EXPIRE 分离或成功清零。

| 场景 | 必须观察到的结果与证据范围 |
| --- | --- |
| 无效 JSON、缺 header、错误 Origin/方法、尾斜线 | 来源先判断；随后仍遵守各层错误语义。真实网络 body/超时/代理另验 |
| 同组合并发、换来源、换账号 | 各自触发对应共享上限；同一次来源和账号判断不是一个原子事务 |
| 连续超限、等待、安静期、成功登录 | 退避有限且可衰减；阻塞中的检查不延长其状态，成功不清掉其他在途计数 |
| 无 TTL/损坏状态、断连、超时 | fail closed，未进行密码验证；响应丢失和 Redis 重启/切换另行注入 |
| 已有会话遇到登录防护故障 | 会话读取/登出仍按自身条件处理，不返回假成功，不等同于全系统不依赖 Redis |
| 429/503 到 Web | API/header/Problem 同步；不自动发送密码，重复点击/卸载与三语反馈有真实交互证据 |
| 离页或密码响应丢失 | challenge 阶段不再启动密码 POST；密码已发送时只抑制晚到回调，不声称服务端回滚，不自动再登录/登出 |

现有证据入口按替身边界选择，不把文件名中的 real 当作完整端到端：

| 验证层 | 已有测试的范围 |
| --- | --- |
| 短期状态 | [实际 Lua/TTL](../../PJM/backend/tests/auth/test_real_login_protection.py)：独立 Redis、多个 service 对象并发；长退避通过移动专用状态时间戳验证 |
| HTTP 到拒绝 | [ASGI + Redis](../../PJM/backend/tests/auth/test_real_login_http.py)：错误正文限流、跨来源账号累计、损坏状态拒绝；未启动网络 server，数据库和密码调用被禁止 |
| 成功不清零 | [Session service](../../PJM/backend/tests/auth/test_session_service.py)：真实 Redis 与密码校验，Session 持久化使用 fake，不证明 PostgreSQL 提交 |
| 客户端消费 | [HTTP 元数据](../../PJM/web/tests/api/http.test.ts)、[auth 请求](../../PJM/web/tests/api/authProjects.test.ts)、[三语反馈](../../PJM/web/tests/lib/loginFeedback.test.ts)：mock fetch 和纯函数，不挂载真实 LoginPage |
| 真实页面 | [LoginPage 浏览器回归](../../PJM/web/tests/browser/check_login.py)：真实 component/StrictMode、键盘、三语与窄屏；API 和 cookie 是 fixture，不连接真实账号/数据库 |

[共享 Redis fixture](../../PJM/backend/tests/auth/conftest.py)只启动自己的 Unix socket 进程，不连接项目 Redis；运行前读[隔离验证指南](../development/local-development.md#隔离-redis-登录防护验证)。[页面回归步骤](../development/local-development.md#ログイン画面のブラウザ回帰)与单元测试分开执行；它覆盖表单重复 submit/卸载和等待反馈，不证明 App 初次会话检查、多 tab、真实 PostgreSQL、HTTPS 多实例或 Redis 切换。已有局部证据不替代这些环境验收。
