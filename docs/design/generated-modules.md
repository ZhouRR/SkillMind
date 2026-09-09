# generated FrontendModule 设计

> 后续设计，部分前置实现。2026-09-09 核对：只有版本模型、纯函数检查与 migration 0026；没有构建服务、投放路由、iframe Preview 或运行时回退。状态见[计划 R04](../planning/roadmap.md#r04-生成模块)，具体核对与验证见[交付记录 §74](../history/delivery-history.md#74-生成模块边界与开发阅读路径续整2026-09-09)。本文的目标不是已经发布的 API。

本页负责生成界面的构建、隔离、展示版本和失败回退；任务业务规则仍由 [SkillVersion](skill-contract.md)负责，执行事实仍由 [Runtime](agent-runtime.md)负责。先看例子，再按职责进入[现有代码](#现有代码与公开契约)或[首次执行门禁](#实施顺序与验收)。

## 先分清三种模块与预览

| 名称 | 含义与限制 |
| --- | --- |
| 业务模块 | SkillComposition 把精确 SkillVersion 组合展示；现有 `/projects/{project_id}/modules` API 管理它，不管理生成代码 |
| 生成界面 | FrontendModuleVersion 描述一份隔离运行的展示产物；有表和检查函数不等于可以投放 |
| 文档预览 | 现有 DocumentManagerPanel 的 HTML 预览使用不允许脚本的 sandbox；不是生成 React 界面的 Preview，也不能直接加 allow-scripts 复用 |

standard / ViewSpec 是平台受控展示，不执行生成的 React/TS。任务分组、声明式视图、文档预览、生成界面不是同一发布或安全边界；实现入口见[Backend 案内](../../PJM/backend/README.md#生成-module-の前置実装を読む)。

## 一个例子：图表坏了，任务没有失败

假设某个已完成 Run 使用 SkillVersion A。目标中的生成界面 G1 能画图，G2 预览通过后被选中，但用户打开时初始化超时。

```text
Run 继续指向 SkillVersion A
        ↓
Host 尝试展示 G2
        ↓ 初始化失败
关闭本页 iframe 与消息通道
        ↓
standard 展示同一 Result / Evidence
```

这次失败不重跑任务、不修改 Result、不自动把业务 SkillVersion 切到 B。管理员以后可选择仍可用且兼容 A 的 G1；这是[展示选择与回退](#展示选择与全局停用)，不是恢复一个已 DISABLED 的版本。以上是待实现的用户体验，不是当前可操作步骤。

## 目标与流水线

为 Skill 生成 React/TypeScript 源码，经受控构建形成不可变 bundle，在隔离 iframe 中呈现；失败时回退平台标准视图。

```text
冻结源码、依赖与构建配置
        ↓
静态检查 → 不通过则停止
        ↓
隔离构建与断网测试
        ↓
冻结产物、CSP 与报告
        ↓
隔离预览 → ADMIN 发布 → 选择展示版本
        ↓
Host 受控交互 / 本页失败回退
```

生成模块只负责呈现。它不创建后端服务、数据库表、Secret、Tool 或新的系统权限。预览已经执行生成代码，因此也必须经过首次执行门禁，不能成为绕过发布安全检查的开发捷径。

## 现有代码与公开契约

| 当前载体 | 已有行为与不能推导的保证 |
| --- | --- |
| 版本模型 | `FrontendModuleVersion` / migration 0026 有状态、hash、CSP、报告与外键；没有 repository/service 接线，DB 也没有全面禁止内容更新的机制 |
| 状态函数 | `plan_module_transition` 检查枚举跳转；`require_publishable` 只检查 BUILT、有 bundle_hash、静态未拒绝。不会确认 blob 存在、hash 正确、构建报告或浏览器安全 |
| 静态与构建检查 | `analyze_module_sources` / `validate_dependencies` / `plan_module_build` 都是局部前置函数；没有实际安装、构建、网络隔离或产物投放 |
| 运行契约 | RuntimeManifest 的 `ui.frontend_module` 如出现只允许 null；`runtime_defaults` 也将它设为 None。代码中的 Module API v1 常量不证明 Host Schema/SDK 已存在 |

不要给现有业务模块 API 增加“临时 bundle URL”，或把生成引用塞进 ViewSpec 来绕开上述契约。后续先定义版本化引用、读取兼容与发布门禁，再同步 Schema、解释/发布、Run/Host 消费者和测试。[契约入口](../../PJM/contracts/README.md#生成-module-と既存-module-api-を分ける)负责找到准确文件，本页不预造公开字段。

## 决策与安全边界

| 决策 | 约束与理由 |
| --- | --- |
| D1 源码生成 | React/TS 可扩展呈现；业务控制仍由 Host/API 负责 |
| D2 同主机专用路径 | 目标路径为 context path 下的 modules/bundle-hash；响应头与 iframe 共同强制 opaque origin |
| D3 首次执行门禁 | 威胁模型、批准依赖、构建隔离与浏览器验证完成后才能实际运行 |
| D4 独立 builder | 不接 edge，不挂载凭据，不挂 Docker socket；只访问批准的内部依赖来源 |
| D5 依赖白名单 | 冻结版本/lockfile，禁止 lifecycle script、native/Git/动态依赖 |
| D6 回退 | 本页错误关闭 iframe；全局停用另需受信决定，Task/Run/Result/Evidence 不变 |

### Origin 与 CSP

iframe 固定 `sandbox="allow-scripts"`，不加入 `allow-same-origin`、弹窗、顶层导航或下载权限。bundle 文档的 HTTP 响应也必须包含 `Content-Security-Policy: sandbox allow-scripts`，以覆盖直接打开 bundle URL 的情况；不能只用 iframe 属性代替响应头。CSP sandbox 在 meta 和 Report-Only 中均不生效，投放测试必须检查实际响应与浏览器行为。[CSP sandbox 规范](https://www.w3.org/TR/CSP3/#directive-sandbox)

同主机 sandbox 与独立站点不是等价的风险边界。opaque origin 约束执行后的 origin 权限，**不保证加载文档的 HTTP 请求不携带该主机 Cookie**。静态路径不能处理业务 mutation；API 的认证、Origin/CSRF、资源所有权与 Host 消息验证继续独立生效。具体投放访问控制在首次执行的威胁模型中验证。

仅配置 `default-src 'none'; connect-src 'none'` 会同时阻止脚本和样式，不能被当作可工作的最终配置。构建产物必须明确脚本/样式加载方式；本设计以与不可变产物匹配的内容 hash 为优先方案。若选择响应级 nonce，须先定义 HTML/CSP 同步、缓存与产物完整性的关系，不能把固定 nonce 写进 bundle 长期复用。不得用 `unsafe-inline`、`unsafe-eval` 或宽泛公网来源来消除报错；opaque origin 下也不能未经浏览器验证就假设 `'self'` 足够。

目标 CSP 还须约束 `base-uri`、`form-action`、`frame-src`、`object-src`、worker 与嵌入方。connect-src 不负责所有导航/资源渠道；不能把它等同于完整网络隔离。[CSP fetch 指令](https://www.w3.org/TR/CSP3/#directives-fetch)分别约束不同加载目的。项目的同主机安全结论仍需实际威胁模型和浏览器验收，不能从规范条文直接推导。

### 当前 CSP 常量不是投放配置

[domain.py](../../PJM/backend/src/projectmind/modules/domain.py) 的常量仍有 `script-src 'self'`、`style-src 'self' 'unsafe-inline'`、`img-src 'self' data:`，与本页目标存在差距。现有测试只断言少数指令字符串存在，没有运行产物或验证导航/网络；尚无投放路由使用该常量。

后续应将产物格式、策略和 Host 兼容性作为一组进行验证，不直接复制常量上线。本轮只标明影响面，不在文档整理中更改常量、测试断言或已存版本。不能安全投放的历史记录保持不可运行，也不通过覆盖旧 CSP 来伪造旧版验收。

### 静态拒绝与依赖

[static_analysis.py](../../PJM/backend/src/projectmind/modules/static_analysis.py) 目前按字面模式产生拒绝报告，包含文件、行号与摘录。以下是现有检查分组，不是完整 JavaScript 安全分析：

| 拒绝码 | 匹配对象 |
| --- | --- |
| `dynamic_code_evaluation` | eval、new Function、innerHTML 赋值、document.write、dangerouslySetInnerHTML |
| `direct_network_access` | fetch、WebSocket、EventSource、XMLHttpRequest、sendBeacon |
| `host_context_access` | document.cookie、window.parent/top/opener、location 赋值、直接 parent/top.postMessage |
| `external_resource_url` | 字符串中的 HTTP(S) 或协议相对外部 URL |
| `dynamic_import` | import()、require() |
| `persistent_storage_write` | localStorage、sessionStorage、indexedDB、caches.open |

声明依赖白名单为 react、react-dom、@projectmind/module-sdk 与 @projectmind/ui；白名单本身不意味着内部包已经发布可用。当前只检查提交的 manifest 中部分依赖声明和 install/prepare 等脚本名，不校验完整 lockfile 图，也不会检查或安装传递依赖。非法类型可能被跳过，故“无 finding”不等于 manifest 有效；需要先做输入形状校验，再检查每份实际取得的包和依赖闭包。

字面扫描会误报，也可能漏过混淆/间接调用，不能因检查通过就允许运行。模块与父窗口的通信应经受信任 SDK；生成业务源码不能借 Host 通信名义直接访问父窗口。测试入口为[静态分析回归](../../PJM/backend/tests/modules/test_module_static_analysis.py)。

### 构建网络

[build_plan.py](../../PJM/backend/src/projectmind/modules/build_plan.py) 当前拒绝缺少 registry、已知公共 registry 与部分 lockfile 外部来源；这是前置检查，**不是完整 egress allowlist**。未列出的外部 host 仍不能被当作“已证明为内部”。

实际构建服务必须将 registry/mirror 作为显式批准的来源，校验所有 resolved URL、重定向与网络出口，并阻断其它网络。不能靠补长 PUBLIC_REGISTRY_HOSTS 拒绝名单实现允许名单。构建后测试断网。没有这些控制时不执行生成源码。

### 构建输入与结果的提交

以下是 builder 待实现的职责边界，不是已经存在的 job/表：

1. 平台冻结精确源码、lockfile、允许的依赖闭包、构建器/工具链版本与资源上限，创建可审计的构建请求。builder 不接受任意 Shell 或由源码选择输出路径。
2. builder 在独立临时目录内执行平台固定命令；源码与依赖均是不可信输入。CPU、内存、进程、磁盘、时间与产物大小分别限额；失败停止并清理，不返回半份可运行产物。
3. 先取得完整产物与受限报告，再由受信提交方核对请求身份、hash、静态/构建/隔离测试结论并提交 BUILT。大文件和网络 I/O 不放在 DB 行锁内。
4. 重试定位原构建请求；未确认的提交不产生第二份发布。孤立 blob 可延后清理，但没有完整记录的 blob 不可投放。构建成功不自动 PUBLISHED，预览成功也不自动选择展示版本。

安装不运行包生命周期脚本；build 命令只使用平台批准的工具链。报告中的源码摘录、路径和错误也可能含敏感数据，不直接交给所有 Project 成员或写入公开日志。迁移和部署需同时考虑源码/产物/报告的保存、授权读取与恢复，只有 hash 列并不足够。

## Host 协议

子文档使用 opaque origin，message 的 origin 可能为 `null`。Host 必须同时校验消息来源窗口、随机 channel nonce、模块精确版本、当前 Project/Task/Run 与 payload Schema；不能仅按 `event.origin` 信任。nonce 绑定本次挂载，不是账户凭据，也不授予业务权限。

允许的目标动作是读取最小化 context/result、更新页面输入草稿、打开平台 Preflight/调度草稿、订阅当前 Run、打开 Evidence/Artifact。脱敏后的业务内容仍可能机密，只给当前授权视图必要字段。草稿操作不提交任务，打开 Evidence 是请 Host 检查所有权并展示，不向子页发放任意 URL 读取能力。模块不能直接调用后端、批准 Proposal、管理用户/Skill/Secret 或读取其它 Run。

Host 协议尚未冻结；旧 Workspace §12 的动作名仅是候选设计。新增协议时需同时落地 Schema、Host dispatcher、鉴权与负向测试。

### 挂载、切换与晚到消息

Host 建立的信任仅限当前实例；版本、账号、Project、Task/Run 切换，iframe 重载/导航或回退时，关闭旧通道、取消订阅并丢弃旧异步结果。只有窗口引用相同也不足以跨导航延续信任。更新草稿必须校验当前草稿版本，不能用晚到消息覆盖用户新输入。

向 opaque 接收方不能照搬普通同源 `targetOrigin`。如果初始化确需 `*`，引导消息不得携带业务内容、Cookie/CSRF 或凭据；后续须评审专用通道、实例绑定与导航失效方案。MessageChannel/nonce 本身不证明生成内容可信，未完成威胁模型前不传入真实业务数据。此处是项目约束，不是已验证的通信协议。[HTML 消息安全规范](https://html.spec.whatwg.org/multipage/web-messaging.html#security-postmsg)要求核对消息格式，避免以 `*` 发送机密内容，并考虑限流。

Host 对每种动作限制 payload 大小、频率和在途数量；未知动作/版本拒绝，重复消息不重复打开弹窗或创建订阅。schema、实例检查与当前授权都通过后才调用现有平台入口；不可转成一个通用 API 代理。错误返回稳定原因，不含完整业务 payload 或堆栈。

## 版本与回退

FrontendModuleVersion 绑定精确 SkillVersion，冻结 source/lockfile/bundle hash、Module API version、CSP 和构建报告。冻结的是内容与验证依据，不是禁止状态变化：DRAFT → BUILT → PUBLISHED，各阶段可到 DISABLED；DISABLED 不恢复。上述枚举已有纯函数，构建/发布/停用事务与操作审计尚未实现。

### 内容身份与历史兼容

现有唯一约束是 `(skill_version_id, source_hash)`，不能表达“源码不变、只升级 lockfile/工具链/CSP 的新展示版”。源码没有变化时伪造 source_hash、修改注释凑新 hash，或覆盖旧 bundle，都不是解决方式。

目标将源码身份、完整构建输入身份、产物身份分开：相同完整输入的重试确认原请求，不同 lockfile/工具链/策略走新构建与新版本。具体字段与唯一约束在实现前同时设计 model/migration/repository 与发布契约，不能只改 Python dataclass。保留旧 ID、hash 和引用；缺少完整验证的旧记录不是自动可投放版本。DB 内容不可变与更新权限也需落实，`frozen=True` 的读 DTO 不保护数据库。

### 展示选择与全局停用

展示选择与 Skill 业务启用必须独立。现有代码注释把回退描述为切换 ProjectComposition 的 SkillVersion；这样会改变后续任务的规则来源，不能实现[图表失败的例子](#一个例子图表坏了任务没有失败)。后续增加独立、可审计的展示选择边界，并确认目标版本兼容当前精确 SkillVersion；不能通过改 composition 来处理界面异常。

| 情况 | 目标处理与不变项 |
| --- | --- |
| 本页超时、崩溃或协议错误 | 关闭本实例，保留当前输入草稿，显示 standard 和可理解原因；不自动重试业务写入，也不全局停版 |
| 选择以前的展示版本 | 经授权与并发检查选择仍可投放的兼容版本；不改 Run 快照，不复活 DISABLED |
| 安全撤回或管理员停用 | 受信操作记录原因、操作者和原选择；阻止新投放，并让现有 Host 失效。客户端错误上报不能单独停用全体用户的版本 |
| 无模块、不兼容或历史验证缺失 | 使用 standard，不挑选“最新可用”来猜测兼容，也不重新执行旧 Run |

内容寻址不等于公开访问授权；bundle hash 不是访问凭据。投放须验证当前版本状态和请求者权限，缓存不能越过 Project 或停用检查。已下载的字节不能被远程收回；Host 撤销数据通道与重新检查授权是独立措施。缓存策略、撤回传播时限和备份恢复必须在投放设计中冻结并验收，不先承诺“停用即清除所有副本”。

无 generated 模块不妨碍任务通过 standard 执行；但原本的 Skill 启用、资源就绪、权限与执行门禁仍适用。

## 实施顺序与验收

| 阶段 | 交付 | 门禁 |
| --- | --- | --- |
| 前置（已有） | 版本模型、纯检查函数 | 不执行生成代码；先补完整身份、CSP 与公开契约差距 |
| 构建服务 | 独立 service、固定依赖、网络/资源限制 | 内部镜像、完整出口控制、隔离与提交恢复 |
| 安全投放与预览 | 专用响应头、授权投放、iframe 与 Host | 威胁模型；预览也需过门禁 |
| 发布与回退 | 独立展示选择、停用、standard 回退 | 授权、并发、历史兼容、缓存与撤回传播 |
| 可用性 | 键盘、窄屏、三语与内容溢出 | 浏览器验收 |

### 从场景验收

- 同源码换 lockfile/策略：产生独立构建身份；旧产物和引用不变。同请求的超时重试不重复发布，提交结果不明可核对。
- 未批准 host、跨 host 重定向、传递依赖脚本、伪造报告、超额产物：在对应边界拒绝；无可投放的半成品，无公网兜底。
- 直接导航、iframe、缓存命中、错误响应：核对实际 CSP 与安全响应；在禁止的 DOM/网络/导航测试之外，正向确认允许的脚本和样式能够运行。字符串断言不替代浏览器。
- 伪造 null-origin 消息、错误 source/nonce、旧 iframe、账号/Project/Run 切换、突发与过大消息：不泄漏数据、不覆盖新草稿、不触发业务写入；卸载后无活动订阅。
- G2 故障、G1 不兼容、全局停用、旧页面晚到响应：standard 仍能查看同一结果；不改变 Task/Run/Result/Evidence，不自动切换业务 SkillVersion。
- 键盘、三语、320px 窄屏、长内容与焦点恢复：用户能看懂“展示失败”和“任务失败”的区别，能回到平台原有操作。

本地 helper 回归、真实隔离 builder、代理响应与业务浏览器是不同证据。[R04](../planning/roadmap.md#r04-生成模块)汇总差距；[Backend 接续](../../PJM/backend/README.md#生成-module-の前置実装を読む)与 [Web 接续](../../PJM/web/README.md#生成表示と業務-module-を分ける)提供代码入口。保留[旧 §24](../planning/roadmap.md#24-generated-frontendmodule)供历史引用，不把旧里程碑当作新增执行授权。
