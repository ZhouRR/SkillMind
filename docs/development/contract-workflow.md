# 公开契约变更与联调

本页负责跨层交付；字段规则在[领域设计](../design/README.md)，实现状态在[计划](../planning/roadmap.md)，命令环境在[本地开发](local-development.md)。

## 先看整条交付链

```text
领域规则/授权/兼容 → Schema/example → Backend 生产
  → Web/Worker 消费 → 契约与流程验证 → 版本匹配及发布
```

按影响修改，不机械改全部层。新增 required、删除字段、收窄 enum、改变空值或 HTTP 拒绝语义都可能破坏兼容，即使 URL 与 v1 文件名未变。

## 三种数据不要混成一份

| 层 | 责任与边界 |
| --- | --- |
| 持久快照 | 保存当时事实、格式版与校验值；不为新页面重写旧 Run/hash |
| 公开响应 | 授权后白名单投影；不直接返回内部 JSON 列，也不修改原值来完成脱敏 |
| 页面状态 | validator 后生成视图；不补造历史资源、不自行推导身份、不把缺字段视为成功 |

## 开工时列出消费者和同步先后

| 检查面 | 实际入口与检查内容 |
| --- | --- |
| 形状与 HTTP | [Contracts](../../PJM/README.md#contracts)、[routes](../../PJM/backend/src/projectmind/api/routes/)：required/nullable/enum、额外字段、status、header、缓存与 Problem |
| 授权与业务 | service/projection、[actor dependencies](../../PJM/backend/src/projectmind/api/auth_dependencies.py)：归属、hash、幂等与历史格式 |
| 持久与执行 | DB/repository/Worker：新旧格式、迁移必要性与读写顺序 |
| Web 与旁路 | api validator/barrel、组件、三语、Schedule、CLI、fixture 与 mock browser：正常/缺失/拒绝/历史分支 |
| 派生契约 | [OpenAPI](../../PJM/contracts/openapi/projectmind-api.v1.json)：由当前 app 生成，禁止手补快照 |

新 example 同步 [validator 注册](../../PJM/scripts/validate_contracts.py)与 [Backend 契约测试](../../PJM/backend/tests/contracts/test_contracts.py)。JSON Schema 不验证授权、内容 hash 或数据库竞争。具体强制同步见[实现细则](coding-rules.md#同步点)。

例如登录 429/503 复用 Problem JSON，但要求 HTTP client 保留 Retry-After、页面区分拒绝原因；仅验 Schema 会遗漏 header。OpenAPI 一致性也只证明快照等于声明，还需检查声明是否覆盖实际 body、media type 和 header。

## 遇到未接齐的交付链

| 现象 | 接续动作 |
| --- | --- |
| DTO/model 有，route 未调用 | 找装配和用例，不把字段当公开 API |
| route/Schema 有，OpenAPI 缺操作 | 核对语义后运行 exporter，不重造 API |
| API 齐，client/page 缺失 | 先接 validator/barrel，再验实际用户流程 |
| example 通过而一致性失败 | 分开报告，不能以静态合法代替交付一致 |
| 单模块通过但合跑收集失败 | 保留失败，修复 fixture/import 后重跑相同组合；未收集不算通过 |

只读检查需 Backend 开发依赖，不启动 lifespan、不连 DB、不回写 OpenAPI。在 `PJM/` 执行 `python3 scripts/validate_contracts.py`；在 `PJM/backend/` 执行：

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q \
  tests/contracts/test_contracts.py::test_exported_openapi_is_current
```

文档整理遇到失败只说明断点；获准改 API 时才在 `PJM/` 运行 `python3 scripts/export_openapi.py`，同时完成消费者和测试同步。不要关闭校验或修改 example 空值来掩盖断链。

## 历史数据兼容不等于前后端版本兼容

- 新 API 读取旧 Run：缺快照返回明确历史状态，保留结果，不从今天的资源补齐。
- 新 Web 读取旧 API：只接受已定义的兼容形状，否则报版本/契约错误；必需字段不能默认空数组。
- 旧 Web 读取新响应：核对额外字段与 enum 的 validator；不假定 JSON 天然兼容。
- API/Worker 与回退：确认 schema、队列、非终态快照与安全规则可读；不能删审计或降级校验换取回退。

即使同批发布，也要考虑旧浏览器、未退出 Worker 和在途请求。损坏的可选展示可以按领域规则降级，身份、授权和必需协议错误不能降级为成功。

## 示例：Run 的冻结文档读取投影

按[资源投影](../design/resource-snapshots.md#公开选择与读取投影的实施契约)核对：即时/调度显式选择 → 创建固定成员 → detail 白名单完整投影 / history 摘要 → Web 三态解析与三语。FROZEN 有清单，LEGACY_UNAVAILABLE/INVALID 无成员；不公开内部 binding，也不让当前目录改变旧 Run。

这些状态说明清单可信程度，不证明 blob 可读、物化完成或 Run 成功。验收覆盖跨 Project/slot/hash 拒绝、新上传不入旧清单、历史分支及 route/Schema/example/OpenAPI/Web fixture 一致。

## 怎样记录验证结论

分别报告文档、契约、Backend/Web、真实事务、用户流程和部署/Provider/模型的范围；mock、skip、失败和未执行明确标注。真实 DB、部署或外部写入需授权的隔离目标。当前断点只写计划，规则回到设计，不再追加历史日志或借用旧测试数字。
