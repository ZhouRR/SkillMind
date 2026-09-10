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

浏览版只收录 docs Markdown、工作区/代码根 README 和 AGENTS；非固定分类的普通文档归“其他资料”。Backend 短 README 用于打包，不重复导航；默认不建模块 README。
禁止扩大扫描到 .env、配置、凭据、Gold、源码或 Skill 执行资产。docs 内含 SKILL.md 的目录也作为 Skill 包整体排除（含 references/HTML），相邻普通文档照常收录；原文件不随文档整理修改。

## 写一份设计

较大设计按“用途/非目标 → 责任与数据 → 流程及失败 → 实施入口与验收”组织，只保留有关部分；小改动更新原段，不为套模板扩成整页。复杂关系才用短表或图。

- 新内容优先替换原段，不追加“本轮完成”或迁移流水。设计保留限制及待接协议，完整进度集中计划。
- 一个规则只写一次；字段全集链接契约，命令链接操作指南。跨页引用优于复制长段。
- 区分当前行为与目标设计；待接协议写清触发条件，不混入日常操作或每次开发的必做项。权限、冻结、批准、原请求、事务/结果未知和兼容保留唯一正本，不为缩短而删除。
- README/AGENTS 用日文，设计正文用中文，标识符不翻译。每页一个 H1，标题层级不跳号。
- 标题保持稳定；合并/删除时同步现行链接，不留空章节。旧代码编号仅由[对应表](../README.md#旧番号の対応)解释。

## 一次整理的更新顺序

核对正本及相关代码/契约 → 原位精简 → 检查链接/示例并生成浏览版 → 浏览修改涉及的页面；构建器或模板变化再做工具回归。
保留无关改动，不重导出 OpenAPI 来掩盖差距；交付说明本次验证和未覆盖范围，不复制旧测试次数。

## 维护进度报告

[roadmap](../planning/roadmap.md)固定采用“总体进度与估算口径 → 按重要度排序的 R01–R13 完成度表 → 首版目标与推进顺序 → 验收与停止条件”。每项只保留已有基础、关键缺口、本期取舍和设计链接，详细规则不在计划复制。

只维护最新快照；实现、验证证据或获准范围变化时原位复估并更新基准日期，不因文档整理或测试数量增加进度。R ID 保持稳定，百分比按完整目标估算；后置和首版验收不算全量完成。不要追加历次报告、逐轮日志或重复的逐项详情。

## 生成和验证浏览版

在 `SKM/` 执行；首次缺依赖时才运行 `python3 -m pip install --user -r scripts/docs-requirements.txt`。正文修改只需：

```bash
python3 scripts/build_docs.py
python3 scripts/build_docs.py --check
```

build 校验标题、代码块及 ViewSpec JSON，并由 Markdown + docs-viewer.html 覆盖生成 index.html。普通生成遇到本地文件、锚点或浏览版页面链接失效时只向终端报告警告，不阻断预览、不删正文或改原文件；失效目标仍无法访问。`--check` 严格检查链接与生成物是否最新，失败也不回写。其他解析、读取和契约错误仍阻止生成；外部链接不联网验证。
只改正文时不动模板。构建器/模板改变时加跑 `python3 -m unittest scripts.tests.test_build_docs -v`；不为正文修改执行部署工具全套测试。`source_paths()` / `FIRST_PAGES` 分别控制收录与阅读顺序。

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
