# 文档维护

正式文档只保留指导当前使用、开发与运维的内容。入口是[文档指南](../README.md)，代码模块导航集中在 [SKM README](../../SKM/README.md)。

## 保留什么

- 用户需要执行的操作、维护者必须保持的边界、可运行的验证和恢复命令。
- 关键取舍及其代码/契约入口；详细字段、状态图和实现步骤直接引用源码，不再维护平行规格。
- [Roadmap](../planning/roadmap.md)只记录当前能力、待复验事项与依据实际效果确定的下一步。没有可靠分母时不用百分比估算。

每条规则只在一个指南维护，其他位置链接引用。废弃详细设计、重复结构图和逐轮进度日志不归档到正式目录，历史用 Git 查询。使用场景未变化时不机械更新进度。

## 整理边界

`SKILL.md`、Skill 的 references/Schema/template 是执行输入，不属于产品文档整理；不要改写业务规则、冻结内容或模型配置。
保留有实际消费者的稳定锚点，删除页面后同步入口和工具检查；不要创建空重定向文档维持旧目录规模。

文档不收录凭据、内部地址、真实业务正文和临时验收数据。示例使用合成值。仅生成器读取正式 Markdown 和三个工程入口，不扫描 `.env` 或依赖目录。

## 构建与检查

在 `SKM/` 执行，HTML 是生成物，不直接修改：

```bash
python3 -m pip install --user -r scripts/docs-requirements.txt
python3 scripts/build_docs.py
python3 scripts/build_docs.py --check
python3 -m unittest discover -s scripts/tests -p 'test_build_docs.py' -v
```

构建检查标题、围栏、链接、锚点及存在的契约示例，`--check` 同时检查生成结果是否最新。文档入口/分组变化时更新 build_docs.py 与对应导航回归；不把旧篇数或详细设计的章节数当作必要功能。

布局或导航改变后，安装 [Web 浏览器测试依赖](local-development.md#ブラウザ回帰)并运行：

```bash
python3 scripts/check_docs_browser.py --output /tmp/skillmind-docs-check
```

检查全页和章节在不同窗口、字号下的布局、搜索、实际链接、键盘、历史和打印；仅访问本地生成文档，不调用业务环境。外置依赖使用 PYTHONPATH，浏览器使用 PLAYWRIGHT_BROWSERS_PATH。

文档整理无需运行业务任务、真实数据库或模型；生成器发生逻辑改动时运行其测试。完成时说明删改范围、实际检查和未验证限制。
