# 文档维护约定

文档只服务当前开发与运行：规则有唯一正本，进度只写计划，不新增历史整理页或特定业务项目的说明。

## 正本与派生内容

| 内容 | 正本 |
| --- | --- |
| 产品、术语、结构 | overview/ |
| 领域规则、失败恢复、兼容、验收 | design/ |
| 当前基础、缺口、优先顺序 | planning/roadmap.md |
| 开发与验证 / 部署与排障 | development/ / operations/ |
| 代码导航 / 必须遵守的规则 | SKM/README.md / SKM/AGENTS.md |
| 按改动查阅的实现约束 | development/coding-rules.md |
| 离线浏览版 | docs/index.html，由 Markdown 生成 |

浏览版只收录 docs Markdown、工作区/代码根 README 和 AGENTS。Backend 短 README 用于打包，不重复导航；默认不建模块 README。
禁止扩大扫描到 .env、配置、凭据、Gold、源码或 Skill 的 SKILL.md/references。执行资产不随文档整理修改。

## 写一份设计

按“用途/非目标 → 责任与数据 → 流程及失败 → 实施入口与验收”组织，复杂关系才用短表或图。

- 新内容优先替换原段，不追加“本轮完成”或迁移流水。设计保留限制及待接协议，完整进度集中计划。
- 一个规则只写一次；字段全集链接契约，命令链接操作指南。跨页引用优于复制长段。
- 明确设计要求、局部实现与已验证行为。不能删掉权限、冻结、批准、原请求、事务/结果未知和兼容条件来缩短文档。
- README/AGENTS 用日文，设计正文用中文，标识符不翻译。每页一个 H1，标题层级不跳号。
- 标题保持稳定；合并/删除时同步现行链接，不留空章节。旧代码编号仅由[对应表](../README.md#旧番号の対応)解释。

## 一次整理的更新顺序

核对正本及相关代码/契约 → 精简正文和重复入口 → 检查链接/示例并生成浏览版 → 工具回归与实际浏览。
保留无关改动，不重导出 OpenAPI 来掩盖差距；交付说明本次验证和未覆盖范围，不复制旧测试次数。

## 生成和验证浏览版

在 `SKM/` 执行；已有依赖不重复安装：

```bash
python3 -m pip install --user -r scripts/docs-requirements.txt
python3 scripts/build_docs.py
python3 scripts/build_docs.py --check
python3 -m unittest discover -s scripts/tests -v
```

build 校验标题、代码块、本地链接/锚点及 ViewSpec JSON，并由 Markdown + docs-viewer.html 覆盖生成 index.html；`--check` 不回写。
只改正文时不动模板。`source_paths()` / `FIRST_PAGES` 分别控制收录与阅读顺序。

外置依赖用 PYTHONPATH，检查用 PYTHONDONTWRITEBYTECODE=1，不建 venv、不清理用户已有文件。
若检查 Python 工具，Ruff 显式使用 `--config backend/pyproject.toml --no-cache`。

### 可重复执行的浏览器检查

[检查脚本](../../SKM/scripts/check_docs_browser.py)离线打开 file 页面，阻断并报告 HTTP(S)，不启动业务应用或执行正文命令。

```bash
python3 -m pip install --user -r web/tests/browser/requirements.txt
python3 -m playwright install chromium
PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_docs_browser.py
```

首次安装需下载；已有依赖/浏览器分别通过 PYTHONPATH / PLAYWRIGHT_BROWSERS_PATH 复用。
`--output <新的外部目录>` 才保存截图，固定名称会覆盖。

覆盖页面、章节焦点/原生刷新、真实链接、搜索、前进后退、键盘、打印及旧模块书签。
字体 16/24px，宽度 320/390/768/1440px；原生刷新先检查，不先修补位置。正文修改检查涉及的浏览路径，viewer/导航改动执行全量回归。

### 人工浏览检查

抽看修改入口、长表和流程：标题完整露出固定栏下方，按钮不碎成竖排，整页不横向溢出。
宽表/图可局部滚动并提示，不能隐藏限制列或缩小全页字号。搜索文字不得变成可执行 HTML。

文档检查不证明业务、真实 DB、模型或部署完成；相关验证见[本地开发](local-development.md#変更に応じた検証)。
