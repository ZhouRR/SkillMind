# 公开契约变更与联调

本页适用于公开协议或持久格式变更；内部重构、样式和文案不因此新增 Schema、版本或 migration。字段语义见[领域设计](../design/README.md)，状态见[计划](../planning/roadmap.md)，命令环境见[本地开发](local-development.md)。

## 先看整条交付链

```text
领域规则/授权/兼容 → Schema/example → Backend
  → Web/Worker → 契约与流程回归 → 版本匹配及发布
```

只改受影响层。新增 required、删字段、收窄 enum、改变空值或 HTTP 拒绝语义，即使 URL/v1 文件名不变也可能破坏兼容。

## 三种数据不要混成一份

| 层 | 边界 |
| --- | --- |
| 持久快照 | 保存当时事实、格式版与 checksum，不为新页面重写旧 Run |
| 公开响应 | 授权后白名单投影，不直接暴露内部 JSON 或修改原值来脱敏 |
| 页面状态 | validator 后生成视图，不补造历史资源、身份或缺失字段 |

## 开工时列出消费者和同步先后

按[同步表](coding-rules.md#同步点)识别实际消费者，包括 Worker/CLI/Schedule 等旁路；只同步受影响项。除了 JSON 字段，还需核对 route 的 status、media type、header、缓存和 Problem。[OpenAPI](../../SKM/contracts/openapi/skillmind-api.v1.json)由 app 生成，不能手补。
Schema 不验证授权/并发，OpenAPI 一致性不证明声明覆盖真实响应；例如登录 429 还须检查 Retry-After 和页面处理。

## 遇到未接齐的交付链

| 断点 | 接续动作 |
| --- | --- |
| DTO/model 有，route 未调用 | 接装配与用例，不把字段当 API |
| route 有，OpenAPI 缺失 | 核对后生成，不重造 API |
| API 有，页面缺失 | 接 validator/barrel，再验用户流程 |
| example 通过，一致性失败 | 保留两项结论，查生产与声明差异 |
| 单模块通过，合跑收集失败 | 修 fixture/import 后重跑同组合 |

读检查不启动 lifespan、不连 DB、不回写。Backend 依赖就绪后，在 `SKM/` 执行 `python3 scripts/validate_contracts.py`；在 `SKM/backend/` 执行：

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q \
  tests/contracts/test_contracts.py::test_exported_openapi_is_current
```

文档整理只报告断点。获准改 API 后才在 `SKM/` 运行 `python3 scripts/export_openapi.py`，并同步消费者和测试；不关校验或改 example 掩盖失败。

## 历史数据兼容不等于前后端版本兼容

- 新 API 读旧 Run：返回明确历史状态，不从当前资源补快照。
- 新 Web 读旧 API：只接受规定的兼容形状，必需字段不默认空数组。
- 旧 Web 读新 API：核对额外字段/enum；同批发布仍有旧浏览器、Worker 和在途请求。
- API/Worker 回退：确认 schema、队列和非终态可读，不删审计或降低校验。

可选展示损坏可按设计降级；身份、授权和必需协议错误不能降级为成功。

## 示例：Run 的冻结文档读取投影

[资源投影](../design/resource-snapshots.md#公开选择与读取投影的实施契约)贯穿选择 → 创建冻结 → detail 完整投影/history 摘要 → Web 三态。
FROZEN 有清单，LEGACY_UNAVAILABLE/INVALID 无成员。验证跨 Project/slot/hash 拒绝、旧清单不变和各层一致；清单可信不表示 blob 可读或执行成功。

## 怎样记录验证结论

按本次范围报告文档、契约、Backend/Web、真实事务与部署/模型结果，明确 mock、失败、skip 和未执行。
真实 DB/外部 write 只用获准的隔离目标；断点写计划，不追加历史日志或借用旧通过次数。
