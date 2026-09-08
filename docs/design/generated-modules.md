# generated FrontendModule 设计

> 后续设计，部分前置实现。当前只有版本模型、静态拒绝与构建前置检查；没有可用的构建服务、投放路由、iframe Preview 或运行时回退。不得把本页的目标协议作为已经发布的 API。

## 目标与流水线

为 Skill 生成 React/TypeScript 源码，经受控构建形成不可变 bundle，在隔离 iframe 中呈现；失败时回退平台标准视图。

```text
源码 Draft → 静态检查 → 锁定依赖 → 隔离构建与测试
                                       ↓
                            冻结 bundle/CSP/报告
                                       ↓
                       iframe 预览 → ADMIN 版本发布
                                       ↓
                       Host 协议交互 / 异常回退 standard
```

生成模块只负责呈现。它不创建后端服务、数据库表、Secret、Tool 或新的系统权限。

## 决策与安全边界

| 决策 | 约束与理由 |
| --- | --- |
| D1 源码生成 | React/TS 可扩展呈现；业务控制仍由 Host/API 负责 |
| D2 同主机专用路径 | 目标路径为 context path 下的 modules/bundle-hash；响应头与 iframe 共同强制 opaque origin |
| D3 首次执行门禁 | 威胁模型、批准依赖、构建隔离与浏览器验证完成后才能实际运行 |
| D4 独立 builder | 不接 edge，不挂载凭据，不挂 Docker socket；只访问批准的内部依赖来源 |
| D5 依赖白名单 | 冻结版本/lockfile，禁止 lifecycle script、native/Git/动态依赖 |
| D6 回退 | 加载/协议/运行错误时关闭 iframe；Task/Run/Result/Evidence 不变 |

### Origin 与 CSP

iframe 固定 `sandbox="allow-scripts"`，不加入 `allow-same-origin`、弹窗、顶层导航或下载权限。bundle 文档的 HTTP 响应也必须包含 `Content-Security-Policy: sandbox allow-scripts`，以覆盖直接打开 bundle URL 的情况；不能只用 HTML meta 或 iframe 属性代替响应头。

同主机 sandbox 与独立站点不是等价的风险边界。opaque origin 约束执行后的 origin 权限，**不保证加载文档的 HTTP 请求不携带该主机 Cookie**。静态路径不能处理业务 mutation；API 的认证、Origin/CSRF、资源所有权与 Host 消息验证继续独立生效。具体投放访问控制在首次执行的威胁模型中验证。

仅配置 `default-src 'none'; connect-src 'none'` 会同时阻止脚本和样式，不能被当作可工作的最终配置。构建产物必须明确脚本/样式加载方式，并按内容 hash 或 nonce 生成允许项；不得用 `unsafe-inline`、`unsafe-eval` 或宽泛公网来源来消除报错。opaque origin 下也不能未经浏览器验证就假设 `'self'` 足够。

目标 CSP 还须约束 `base-uri`、`form-action`、`frame-src`、`object-src` 与嵌入方。connect-src 不负责所有导航/资源渠道；不能把它等同于完整网络隔离。原文依据见 [CSP sandbox](https://www.w3.org/TR/CSP3/#directive-sandbox) 与 [default-src](https://www.w3.org/TR/CSP3/#directive-default-src)。

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

声明依赖白名单为 react、react-dom、@projectmind/module-sdk 与 @projectmind/ui；白名单本身不意味着内部包已经发布可用。拒绝非 registry 来源和 install/prepare 等 lifecycle script。依赖的实际固定版本由 lockfile 与构建流程证明。

字面扫描会误报，也可能漏过混淆/间接调用，不能因检查通过就允许运行。模块与父窗口的通信应经受信任 SDK；生成业务源码不能借 Host 通信名义直接访问父窗口。测试入口为[静态分析回归](../../PJM/backend/tests/modules/test_module_static_analysis.py)。

### 构建网络

[build_plan.py](../../PJM/backend/src/projectmind/modules/build_plan.py) 当前拒绝缺少 registry、已知公共 registry 与部分 lockfile 外部来源；这是前置检查，**不是完整 egress allowlist**。未列出的外部 host 仍不能被当作“已证明为内部”。

实际构建服务必须将 registry/mirror 作为显式批准的来源，校验所有 resolved URL、重定向与网络出口，并阻断其它网络。构建后测试断网。没有这些控制时不执行生成源码。

## Host 协议

子文档使用 opaque origin，message 的 origin 可能为 `null`。Host 必须同时校验消息来源窗口、随机 channel nonce、模块精确版本、当前 Project/Task/Run 与 payload Schema；不能仅按 `event.origin` 信任。

允许的目标动作是读取脱敏 context/result、更新页面输入草稿、打开平台 Preflight/调度草稿、订阅当前 Run、打开 Evidence/Artifact。模块不能直接调用后端、批准 Proposal、管理用户/Skill/Secret 或读取其它 Run。

Host 协议尚未冻结；旧 Workspace §12 的动作名仅是候选设计。新增协议时需同时落地 Schema、Host dispatcher、鉴权与负向测试。

## 版本与回退

FrontendModuleVersion 冻结 source/lockfile/bundle hash、Module API version、CSP 和构建报告，绑定精确 SkillVersion。bundle 不可覆盖，新内容必须新版本。不能投放的版本不能标为可运行发布。

升级/回退只切换展示版本，不修改运行事实。运行失败禁用 iframe 并回到 standard；无 generated 模块的 Skill 始终可执行。

## 实施顺序与验收

| 阶段 | 交付 | 门禁 |
| --- | --- | --- |
| 前置（已有） | 版本模型、静态拒绝、构建配置检查 | 不执行生成代码 |
| 构建服务 | 独立 service、固定依赖、网络/资源限制 | 内部镜像和完整出口控制 |
| 安全投放与预览 | 专用响应头、bundle 加载策略、iframe 与 Host | 威胁模型；直接导航、伪造消息、Cookie/CSRF、脚本可用性测试 |
| 发布与回退 | 版本绑定、健康监测、standard 回退 | 加载失败/超时/协议不兼容/异常测试 |
| 可用性 | 键盘、窄屏、三语与内容溢出 | 浏览器验收 |

静态扫描不能证明生成代码安全；首次执行前必须完成上述跨层检查。当前计划见[§24](../planning/roadmap.md#24-generated-frontendmodule)。
