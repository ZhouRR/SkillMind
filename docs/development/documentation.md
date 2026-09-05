# 文档维护约定

## 正本与派生内容

| 内容 | 放置位置 | 更新方式 |
| --- | --- | --- |
| 产品、术语、整体结构 | overview/ | 解释目的与组件关系，链接详细规则 |
| 不变量、协议语义、失败恢复 | design/ | 每个主题一个正本；标明现状与目标 |
| 当前状态、顺序和验收缺口 | planning/roadmap.md §13 | 实现/部署/专项验收分别举证 |
| 开发命令和变更指南 | development/ | 写明工作目录、依赖、输入和副作用 |
| 配备、backup、restore、故障处理 | operations/ | 根据操作风险给出前置和成功条件 |
| 业务质量标准 | acceptance/ | 说明样本、指标、数据隔离和人工判定 |
| 当时的实施记录 | history/ | 保留历史事实；新结论追加，不倒改旧报告 |
| 代码入口与 agent 约束 | PJM/*/README.md、AGENTS.md | README 做导航，AGENTS 约束变更 |
| Skill 输入与解释器指令 | PJM/skills/** | 可执行资产，遵守 version/hash/fixture 同步 |
| 浏览版 | docs/index.html | 从 Markdown 生成，不手改正文副本 |

Markdown 是可维护来源。浏览版只嵌入 docs Markdown、workspace/代码 README 和 AGENTS，不扫描 .env、内部配置、业务 Skill 原文、凭据、测试 Gold 或任意源码。

## 写一份设计

开头写用途、状态和前置文档；正文依次说明场景、数据/职责、成功与失败流程、兼容性、实施入口和验收。表格用于比较，短流程图用于表达分支，代码示例应能通过现有契约。

- 精确字段、enum 和 required 以 Schema 为准；示意结构明确标为目标或语义摘要。
- 已实现的功能提供实现入口。未实现的功能明确缺少的 API/Schema/页面，不写成用户操作步骤。
- 发现设计与实现不符时分别写现状、影响、目标及同步范围，不能悄悄弱化设计或声称代码已修好。
- 不在 README、设计和计划同时维护同一进度表，不继续把实施日志追加到设计主文。
- 使用可点击的相对链接；旧 docs/01–13 略记由 [文档索引](../README.md#旧番号の対応) 解释。
- 保留有用历史，过时材料进入 history 并标注时点。归档不能成为当前功能或测试状态的证据。
- README/AGENTS 使用日文；设计正文沿用中文，协议与代码标识保持原样。

## 生成和验证浏览版

以下在 `PJM/` 执行。依赖只用于文档工具，不改变 Backend/Web 运行依赖，也不需要虚拟环境。

```bash
python3 -m pip install --user -r scripts/docs-requirements.txt
python3 scripts/build_docs.py
python3 scripts/build_docs.py --check
```

若 user site 不可写，可把依赖安装到工作区外明确的临时目录，并用 PYTHONPATH 指向它；不因此修改应用依赖或创建 venv。

build 检查 Markdown 标题、代码块、本地链接与章节锚点，并验证本文档中的 ViewSpec JSON 示例。生成的 index.html 包含所有可浏览正文，可直接离线双击打开；不使用 CDN、运行时 fetch 或服务端。目录搜索支持标题和正文，历史默认单独折叠。

`--check` 执行相同检查并比较生成物是否与来源一致，内容未同步时退出非零，不修改文件。变更 Markdown 或浏览模板后重新 build，再提交/交付来源与 index.html。

## 图与附件

[业务结构图](../overview/business-structure.html)和[技术结构图](../overview/technical-architecture.html)是离线展示摘要。改变组件关系时同步检查；它们不维护实现状态表。

旧 Roadmap HTML 留在 history，仅作当时展示记录。当前可视状态来自浏览版里的 roadmap Markdown，避免继续手工维护两套进度。

## 验证结果的表达

文档检查只证明结构、链接与示例一致，不能证明运行行为正确。代码、真实 DB、Compose、模型与浏览器验收分别记录日期和范围；本次未执行的检查不复用历史数字冒充当前结果。
