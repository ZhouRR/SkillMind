# 生成 FrontendModule

本页负责生成界面的构建、隔离、版本和回退，业务/执行仍归 [SkillVersion](skill-contract.md)/[Runtime](agent-runtime.md)。当前仅有版本模型与纯检查函数，完整链路未接；状态见[计划 R04](../planning/roadmap.md#r04-生成模块)。

## 先分清三种模块与预览

| 名称 | 边界 |
| --- | --- |
| 业务模块 | SkillComposition 组合精确 SkillVersion；现有 projects/modules API 只管理此对象 |
| 生成界面 | FrontendModuleVersion 对应隔离展示产物，不是新的业务能力 |
| 文档预览 | DocumentManagerPanel 的禁脚本 HTML，不可加 allow-scripts 当作生成 Preview |

standard/ViewSpec 不执行生成代码；生成 Preview 已是代码执行，必须先过首次执行门禁。

## 一个例子：图表坏了，任务没有失败

目标行为：G2 超时则关闭本页 iframe/通道，以 standard 展示原 Result/Evidence；不重跑、不换业务 SkillVersion、不全局停版。管理员可另选兼容且未停用的 G1，当前尚未实现。

## 目标与流水线

```text
冻结源码/依赖/构建配置 → 静态检查
  → 隔离构建与断网测试 → 冻结产物/CSP/报告
  → 隔离预览 → ADMIN 发布 → 独立展示选择
  → Host 受控交互；本页失败回退 standard
```

模块只呈现，不创建服务、DB、Secret、Tool 或权限；builder 不接 edge、不挂凭据/Docker socket，只用批准依赖来源。

## 现有代码与公开契约

| 当前载体 | 不能推导的保证 |
| --- | --- |
| FrontendModuleVersion / 0026 | 有状态/hash/CSP/报告列，但无 service/repository 接线或完整 DB 不可变保护 |
| plan_module_transition / require_publishable | 检查状态、bundle_hash、静态拒绝；不验证 blob/hash、完整报告或浏览器安全 |
| analyze_module_sources / validate_dependencies / plan_module_build | 局部前置函数，不执行安装、构建、隔离或投放 |
| RuntimeManifest | ui.frontend_module 当前只允许 null；Module API 常量不是 Host Schema/SDK |

入口：[modules](../../SKM/backend/src/skillmind/modules/)、[RuntimeManifest](../../SKM/contracts/runtime-manifest/v1alpha1.schema.json)。不向业务 module API 塞 bundle URL 或借 ViewSpec 绕协议。

## 决策与安全边界

### Origin 与 CSP

目标为同主机 context path 的专用 bundle-hash 路径。iframe 仅 `sandbox="allow-scripts"`，无 same-origin/弹窗/顶层导航/下载权限；HTTP 还须带 `Content-Security-Policy: sandbox allow-scripts` 保护直接导航，meta/Report-Only 无效，见 [CSP](https://www.w3.org/TR/CSP3/#directive-sandbox)。

opaque origin 不保证请求无本站 cookie；静态路径不处理 mutation，API 独立验身份/Origin/CSRF/归属。同主机须专项威胁建模，不能视为独立站点隔离。

产物与脚本/样式 CSP 同时冻结，优先 hash；nonce 须先定义响应/缓存/完整性，不能长期固定。不得用 unsafe-inline/unsafe-eval、宽泛公网或未验证的 'self' 修错。CSP 覆盖加载、base-uri、form-action、frame/object/worker 与嵌入方；connect-src 不控制全部出网/导航，须实测允许渲染和禁止出网。

### 当前 CSP 常量不是投放配置

[domain.py](../../SKM/backend/src/skillmind/modules/domain.py) 的 'self'/unsafe-inline 等常量尚不满足目标，也未用于投放。字符串测试不证明安全；旧记录缺验证保持不可运行，不覆盖 CSP 伪造验收。

### 静态拒绝与依赖

[静态检查](../../SKM/backend/src/skillmind/modules/static_analysis.py)字面拒绝动态代码/网络/宿主 context/外部 URL/动态 import/持久存储，仍可能误报或漏过间接调用；通过不是执行许可，报告也须脱敏。

声明仅允许 react/react-dom/@skillmind/module-sdk/@skillmind/ui，不证明内部包可用。冻结版本与完整 lockfile 闭包，禁止 lifecycle script、native/Git/动态依赖；验证 manifest 后检查全部实际包，不能只查顶层。

### 构建网络

[build_plan.py](../../SKM/backend/src/skillmind/modules/build_plan.py)的来源拒绝不是 egress allowlist。builder 只访问批准 registry/mirror，核验 resolved URL、重定向和出口，构建后断网测试；host 黑名单不能替代。

### 构建输入与结果的提交

1. 冻结源码/lockfile/依赖闭包、工具链/构建器、策略/限额与请求身份，不接受自选 Shell/输出路径。
2. 独立临时目录运行平台命令；分别限制 CPU、内存、进程、磁盘、时间、产物大小，禁安装脚本。
3. 受信提交者核对原请求、完整产物/hash、静态/隔离报告后提交 BUILT；网络 I/O 不占长期 DB 锁。
4. 未知确认原身份，不重复发布；无完整记录不投放。BUILT、Preview、发布与选择分开决定，孤立产物延后清理。

报告/源码/产物须授权读取并有保留/恢复规则，普通成员不见原始错误/路径。

## Host 协议

公开协议尚未冻结。null origin 不能授信，Host 须验 source window、随机 nonce、精确模块版本、当前 Project/Task/Run 与 payload Schema；nonce 只标识挂载。

仅开放最小 context/result、草稿更新、平台 Preflight/调度草稿、当前 Run 订阅和授权附件展示；不自动提交草稿或代理任意 URL，禁直接 API、批准、账户/Skill/Secret 管理及跨 Run 读取。

### 挂载、切换与晚到消息

版本/账号/Project/Task/Run 切换、iframe 导航/重载/回退均关闭旧通道/订阅并拒绝晚到；同窗口不跨导航授信，草稿验当前版本。

若初始化必须以 * 向 opaque 接收方发送，引导不得含正文/凭据；专用通道、nonce 与导航失效评审通过后才传数据，见[消息安全](https://html.spec.whatwg.org/multipage/web-messaging.html#security-postmsg)。限制 payload/速率/在途/重复动作，未知协议拒绝，不做通用 API proxy。

## 版本与回退

版本绑定精确 SkillVersion，冻结 source/lockfile/bundle hash、Module API/CSP/报告。DRAFT → BUILT → PUBLISHED，各阶段可 DISABLED 且不可恢复；事务/审计待接。

### 内容身份与历史兼容

现唯一键 (skill_version_id, source_hash) 不区分依赖/工具链/CSP 变化。目标分源码、完整构建输入与产物身份：同输入确认原请求，不同输入建新版本，不伪造 hash/覆盖 bundle。同步 DB 唯一约束、repository 和发布契约，保留旧 ID/hash/引用；缺验证不投放，frozen DTO 不替代 DB 保护。

### 展示选择与全局停用

| 情况 | 目标处理 |
| --- | --- |
| 本页超时、崩溃、协议错误 | 关闭实例，保留草稿/焦点，standard 展示原结果；不重试业务或全局停版 |
| 回选旧展示版 | 经授权和并发检查选择兼容、仍可投放版本；不改 Run 或复活 DISABLED |
| 管理员/安全停用 | 记录 actor/原因/原选择，阻止新投放并使 Host 失效；不由不可信客户端错误单独触发 |
| 无模块、不兼容、历史验证不足 | standard，不猜 latest，不重新运行旧任务 |

展示故障不切业务 SkillVersion；bundle hash/缓存不越过 Project/停用检查。已下载字节不可收回，撤回传播、Host 断开和恢复分别验收。

## 实施顺序与验收

按身份/契约 → builder → 安全投放/Host → 发布/选择/回退推进；威胁模型与隔离门禁通过前不执行代码。

- 同源码换依赖/策略有新身份；超时重试无重复发布，未知可确认。
- 拒绝外部 host/重定向、传递脚本、伪造报告和超额产物，无半份可运行产物。
- 直接导航/iframe/缓存/错误响应检验实际 CSP；允许渲染正常，禁止的网络/DOM/导航失败。
- null-origin 伪造、错 nonce、旧 iframe、换 actor/context、过大/重复消息不泄漏、不覆盖草稿、不写业务。
- G2 失败、旧版不兼容与全局停用都能保留同一结果的 standard 入口；三语、键盘、窄屏可用。
- [局部回归](../../SKM/backend/tests/modules/)不代替真实 builder、代理和浏览器安全验收。
