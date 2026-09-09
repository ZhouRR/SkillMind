# 生成 FrontendModule

本页负责生成界面的构建、隔离、版本和回退；业务仍由 [SkillVersion](skill-contract.md)定义，执行事实由 [Runtime](agent-runtime.md)保存。当前仅有版本模型、migration 0026 与纯检查函数，尚无 builder、投放、Preview 或 Host；状态见[计划 R04](../planning/roadmap.md#r04-生成模块)。

## 先分清三种模块与预览

| 名称 | 边界 |
| --- | --- |
| 业务模块 | SkillComposition 组合精确 SkillVersion；现有 projects/modules API 只管理此对象 |
| 生成界面 | FrontendModuleVersion 对应隔离展示产物，不是新的业务能力 |
| 文档预览 | DocumentManagerPanel 的禁脚本 HTML，不可加 allow-scripts 当作生成 Preview |

standard/ViewSpec 是平台受控展示，不执行生成 React/TS。任何预览已经执行生成代码，必须先满足首次执行门禁。

## 一个例子：图表坏了，任务没有失败

Run 使用 SkillVersion A，生成展示 G2 初始化超时：Host 关闭本页 iframe/通道，使用 standard 展示同一 Result/Evidence。它不重跑任务、不改变 A、不自动全局停版。管理员可另选兼容且未停用的 G1；这是目标行为，当前未实现。

## 目标与流水线

```text
冻结源码/依赖/构建配置 → 静态检查
  → 隔离构建与断网测试 → 冻结产物/CSP/报告
  → 隔离预览 → ADMIN 发布 → 独立展示选择
  → Host 受控交互；本页失败回退 standard
```

生成模块只呈现，不创建后端服务、DB、Secret、Tool 或权限。独立 builder 不接 edge、不挂凭据或 Docker socket，只使用批准依赖来源。

## 现有代码与公开契约

| 当前载体 | 不能推导的保证 |
| --- | --- |
| FrontendModuleVersion / 0026 | 有状态/hash/CSP/报告列，但无 service/repository 接线或完整 DB 不可变保护 |
| plan_module_transition / require_publishable | 检查状态、bundle_hash、静态拒绝；不验证 blob/hash、完整报告或浏览器安全 |
| analyze_module_sources / validate_dependencies / plan_module_build | 局部前置函数，不执行安装、构建、隔离或投放 |
| RuntimeManifest | ui.frontend_module 当前只允许 null；Module API 常量不是 Host Schema/SDK |

源码入口：[modules](../../PJM/backend/src/projectmind/modules/)，契约入口：[RuntimeManifest](../../PJM/contracts/runtime-manifest/v1alpha1.schema.json)。不可给业务 module API 临时加 bundle URL，或借 ViewSpec 绕开协议。

## 决策与安全边界

### Origin 与 CSP

目标使用同主机 context path 下的专用 bundle-hash 路径。iframe 固定 sandbox="allow-scripts"，无 allow-same-origin、弹窗、顶层导航和下载权限；HTTP 响应还必须带 Content-Security-Policy: sandbox allow-scripts，覆盖直接导航。meta/Report-Only 的 sandbox 无效，见 [CSP 规范](https://www.w3.org/TR/CSP3/#directive-sandbox)。

opaque origin 不保证加载请求不带本主机 cookie；静态路径不处理业务 mutation，API 的认证、Origin/CSRF 和所有权仍独立检查。同主机方案须专项威胁建模，不能视为独立站点等价隔离。

产物与脚本/样式 CSP 同时冻结，优先内容 hash；采用 nonce 时先定义响应、缓存和完整性，不固定 nonce 长期复用。不以 unsafe-inline/unsafe-eval、宽泛公网或未经验证的 'self' 修复报错。

CSP 同时约束加载、base-uri、form-action、frame/object/worker 与嵌入方；connect-src 不是全部网络/导航控制，必须实际验证正向渲染及负向出网。

### 当前 CSP 常量不是投放配置

[domain.py](../../PJM/backend/src/projectmind/modules/domain.py)仍有 script-src 'self'、style 的 unsafe-inline 等目标差距，未被投放路由使用。字符串测试不证明浏览器安全；旧记录缺验证时保持不可运行，不覆盖旧 CSP 伪造验收。

### 静态拒绝与依赖

[static_analysis.py](../../PJM/backend/src/projectmind/modules/static_analysis.py)按字面拒绝动态代码、网络调用、宿主 context、外部 URL、动态 import 和持久存储访问；可能误报、漏过间接调用。通过不是执行许可，报告摘录也可能敏感。

依赖声明允许 react/react-dom/@projectmind/module-sdk/@projectmind/ui，不证明内部包已经可用。目标冻结版本与完整 lockfile 闭包，禁止 lifecycle script、native/Git/动态依赖；先验证 manifest 形状，再检查实际取得的所有包，不能只检查顶层声明。

### 构建网络

[build_plan.py](../../PJM/backend/src/projectmind/modules/build_plan.py)只拒绝已知公共来源与部分 lockfile URL，不是 egress allowlist。实际 builder 必须批准 registry/mirror，校验 resolved URL、重定向及出口并阻断其他网络，构建后断网测试；不能靠扩大 host 黑名单代替。

### 构建输入与结果的提交

1. 冻结源码、lockfile、依赖闭包、工具链/构建器、策略和限额，创建可审计请求；不接受来源自选 Shell/输出路径。
2. 独立临时目录执行平台固定命令；CPU、内存、进程、磁盘、时间、产物大小各自限额，不运行安装脚本。
3. 受信提交者核对请求身份、完整产物/hash 与静态/隔离报告后提交 BUILT；网络 I/O 不占长期 DB 锁。
4. 未知提交核对原构建身份，不重复发布；无完整记录的 blob 不投放，孤立产物可延后清理。BUILT、Preview、PUBLISHED、展示选择分别决定。

报告、源码与产物须有授权读取、保留与恢复规则，不向普通成员暴露原始错误/路径。

## Host 协议

尚未冻结公开协议。消息 origin 可能为 null，Host 必须同时验证 source window、随机 channel nonce、精确模块版本、当前 Project/Task/Run 与 payload Schema；nonce 是挂载身份，不是账号权限。

只允许最小 context/result、草稿更新、打开平台 Preflight/调度草稿、订阅当前 Run、请求 Host 展示授权 Evidence/Artifact。草稿不自动提交，不发放任意 URL 读取能力；禁止直接 API、批准、管理用户/Skill/Secret 或读取其他 Run。

### 挂载、切换与晚到消息

版本、账号、Project/Task/Run 切换，iframe 重载/导航或回退时关闭旧通道/订阅，丢弃晚到响应；同窗口引用也不能跨导航延续信任。草稿更新需校验当前版本。

初始化若必须向 opaque 接收方用 *，引导不得含业务内容或凭据；专用通道、nonce、导航失效须评审后才传真实数据，见 [消息安全规范](https://html.spec.whatwg.org/multipage/web-messaging.html#security-postmsg)。限制 payload、速率、在途和重复动作，未知协议拒绝，不做通用 API proxy。

## 版本与回退

版本绑定精确 SkillVersion，冻结 source/lockfile/bundle hash、Module API、CSP 与报告。DRAFT → BUILT → PUBLISHED，各阶段可 DISABLED，停用不可恢复；事务与审计尚待实现。

### 内容身份与历史兼容

当前唯一键 (skill_version_id, source_hash) 不能表达只换 lockfile/工具链/CSP 的新版本。目标区分源码身份、完整构建输入身份、产物身份：同输入重试确认原请求，不同输入创建新版本，不伪造 source_hash 或覆盖旧 bundle。

同时设计 model/migration/repository、唯一约束与发布契约；保留旧 ID/hash/引用，缺验证的旧行不自动可投放，frozen DTO 不替代 DB 内容保护。

### 展示选择与全局停用

| 情况 | 目标处理 |
| --- | --- |
| 本页超时、崩溃、协议错误 | 关闭实例，保留草稿/焦点，standard 展示原结果；不重试业务或全局停版 |
| 回选旧展示版 | 经授权和并发检查选择兼容、仍可投放版本；不改 Run 或复活 DISABLED |
| 管理员/安全停用 | 记录 actor/原因/原选择，阻止新投放并使 Host 失效；不由不可信客户端错误单独触发 |
| 无模块、不兼容、历史验证不足 | standard，不猜 latest，不重新运行旧任务 |

不能切换 ProjectComposition 的业务 SkillVersion 来修复界面。bundle hash 不是授权，缓存不能越过 Project/停用检查；已下载字节不可收回，撤回传播、Host 断开与备份恢复须独立验收。

## 实施顺序与验收

按完整身份/契约 → builder → 安全投放/Host → 发布/展示选择/回退推进；未满足威胁模型与隔离门禁前不执行生成代码。

- 同源码换依赖/策略有新身份；超时重试无重复发布，未知可确认。
- 拒绝外部 host/重定向、传递脚本、伪造报告和超额产物，无半份可运行产物。
- 直接导航/iframe/缓存/错误响应检验实际 CSP；允许渲染正常，禁止的网络/DOM/导航失败。
- null-origin 伪造、错 nonce、旧 iframe、换 actor/context、过大/重复消息不泄漏、不覆盖草稿、不写业务。
- G2 失败、旧版不兼容与全局停用都能保留同一结果的 standard 入口；三语、键盘、窄屏可用。
- [局部回归](../../PJM/backend/tests/modules/)不代替真实 builder、代理和浏览器安全验收。
