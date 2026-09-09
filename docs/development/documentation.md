# 文档维护约定

文档只保留当前开发与运行需要的信息。规则写在唯一负责的页面，进度写在计划；不再累积整理流水账或特定业务项目的说明。

## 正本与派生内容

| 内容 | 位置 |
| --- | --- |
| 产品、术语、整体结构 | overview/ |
| 规则、失败恢复、兼容与验收 | design/ |
| 当前缺口与优先顺序 | planning/roadmap.md |
| 开发、契约与验证步骤 | development/ |
| 起动、发布、恢复、排障 | operations/ |
| 代码导航 / 必需约束 | PJM/README.md / PJM/AGENTS.md |
| 按改动类型查阅的细则 | development/coding-rules.md |
| 离线浏览版 | docs/index.html，由 Markdown 生成 |

浏览版只读取 docs Markdown、工作区/代码根 README 与 AGENTS。Backend 短 README 只供打包，不重复进入导航。
不扫描 .env、配置、凭据、Gold、任意源码或 Skill 的 SKILL.md/references；执行资产另走版本/hash/fixture 同步。

## 写一份设计

用最少章节说明：用途与非目标 → 责任/数据 → 关键流程与失败 → 现有缺口 → 实施入口与验收。
每页只保留必要场景、短表或流程图；字段全集链接契约，命令链接操作指南，不重复长篇接线说明。

- 明确“设计要求、已有局部实现、已验证行为”，未接入 API/Schema/页面不能写成操作步骤。
- 并发与恢复须说明判定/提交边界、原请求身份和结果未知；不因存在锁、版本号或 204 就推导完整保证。
- 不弱化权限、冻结、批准、不可变结果和恢复规则；安全限制与所限制的能力放在一起。
- README/AGENTS 使用日文，设计正文使用中文，代码标识不翻译。每页一个 H1，层级不跳号。
- 标题写稳定主题，不为旧目录保留空章节。移动/合并时同步现行链接；旧代码文档编号由[对应表](../README.md#旧番号の対応)解释。
- 默认不建模块 README；必要包说明保持极短，导航集中到代码根 README。
- 删掉重复提示和过时测试数字；mock、真实 DB、模型、业务浏览器与部署的验证范围在交付时分别说明。

## 一次整理的更新顺序

1. 核对目标页面和相关源码/契约，明确本次是否修改实现。
2. 修改规则正本，再同步相邻入口与当前缺口，不在每页复制进度。
3. 检查全部文档的本地链接、章节、示例和浏览版目录，保留本次之外的工作。
4. build/check、文档工具回归与相关契约检查后，做桌面/窄屏实际浏览。
5. 报告变更、实际验证和未覆盖风险；只有功能状态变化才更新计划。

## 生成和验证浏览版

在 `PJM/` 执行，依赖已有时不重复安装：

```bash
python3 -m pip install --user -r scripts/docs-requirements.txt
python3 scripts/build_docs.py
python3 scripts/build_docs.py --check
python3 -m unittest discover -s scripts/tests -v
```

build 检查标题、代码块、本地链接/锚点及 ViewSpec JSON，并覆盖生成 index.html；`--check` 只检查一致性。
正文只改 Markdown，交互/样式改 docs-viewer.html，不手改生成正文。
`source_paths()` 控制收录范围，`FIRST_PAGES` 控制阅读顺序；新增文档不扩展到源码或 Skill。

用 Backend Ruff 时显式指定 `--config backend/pyproject.toml` 和 `--no-cache`。
复用外置依赖时用 PYTHONPATH，Python 检查设置 PYTHONDONTWRITEBYTECODE=1；不创建 venv，也不清理用户已有文件。

### 可重复执行的浏览器检查

[检查脚本](../../PJM/scripts/check_docs_browser.py)直接打开生成的 file 页面，阻断并报告 HTTP(S)，不启动应用或执行正文命令。

```bash
python3 -m pip install --user -r web/tests/browser/requirements.txt
python3 -m playwright install chromium
PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_docs_browser.py
```

依赖和浏览器可分别通过 PYTHONPATH / PLAYWRIGHT_BROWSERS_PATH 复用外部目录。首次安装需要下载，检查本身离线。
`--output <明确的外部目录>` 才保存截图，会覆盖固定名称，宜使用新的临时目录。

检查覆盖全部页面、章节布局/焦点/原生刷新、代码到设计的实际点击、搜索、前进/后退、键盘和打印。
字体用 16/24px，屏宽用 320/390/768/1440px；原生刷新先检查，不先调用 showPage 修补位置。
旧模块 README 书签只映射到代码根对应章；删除的资料不再进入目录或搜索。

### 人工浏览检查

抽看修改入口、长表和关键流程：完整标题须位于固定顶部栏下方，按钮标签不碎成竖排，整页不横向溢出。
复杂表和图可局部滚动并有提示；不能隐藏限制列或缩小全页字号来“通过”。搜索文字不得变成可执行 HTML。
新增页/标题/路径须检查真实点击与刷新，viewer 导航/样式变更须执行全量浏览器回归。

[业务结构图](../overview/business-structure.html)和[技术结构图](../overview/technical-architecture.html)仅作整体关系摘要。
文档检查不证明业务实现、真实数据库、模型或部署完成；本轮未跑的检查不能借用旧结果。
