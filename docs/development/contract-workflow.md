# 公开契约变更与联调

> 用途：把一条设计规则安全地交给 Backend、Web 和其他消费者。先在[变更指南](change-guide.md)确定负责的领域；本页解释跨层交付顺序，不定义第二套 API 字段或实现进度。

## 先看整条交付链

```text
领域设计：规则、授权与兼容
  ↓
公开契约：Schema / example
  ↓
Backend 生产 → Web / Worker 消费
  ↓
契约、业务与用户流程验证
  ↓
版本匹配、发布与回退判断
```

这表示检查依赖，不要求每次修改全部层。只改显示文案不需要 migration；只改内部实现且公开语义不变，也不必增加公开字段。相反，保留同一个 URL 或 `v1` 文件名不代表兼容：新增 required、删字段、收窄 enum 或改变空值含义，都可能使现有消费者失败。

## 三种数据不要混成一份

| 数据 | 谁负责 | 不能怎样使用 |
| --- | --- | --- |
| 持久快照 | domain / repository 保存当时事实、格式版本与校验值 | 为了匹配新页面而改写旧 Run 或重算旧 hash |
| 公开响应 | 授权后的 projection / response model 明确列举可公开字段 | 直接返回内部 JSON 列，或者把新增内部字段自动暴露出去 |
| 页面状态 | Web validator 验证后生成用户能理解的视图 | 自行推导服务端身份、把缺字段当成成功、用当前目录补造历史选择 |

公开摘要可以比持久快照少，但必须说明省略的含义。例如 Run history 只提供资源摘要，完整的冻结文档成员由 detail 的专门投影负责；摘要不是执行时的授权输入。安全投影缩减公开字段，也不能修改数据库原值来完成脱敏。

## 开工时列出消费者和同步先后

| 检查面 | 实际入口 | 要回答的问题 |
| --- | --- | --- |
| 请求与响应 | [资源 Schema](../../PJM/contracts/README.md)、[examples](../../PJM/contracts/examples/)、[routes](../../PJM/backend/src/projectmind/api/routes/) | required / nullable / enum / 额外字段是否一致？请求和响应是否被混用？ |
| 业务与授权 | 对应 service / domain / projection、[actor dependencies](../../PJM/backend/src/projectmind/api/auth_dependencies.py) | 形状合法后，归属、权限、hash、幂等与历史格式由谁验证？ |
| 持久与执行 | [DB](../../PJM/backend/src/projectmind/db/)、repository、[Worker](../../PJM/backend/src/projectmind/worker/) | 是否改持久格式？旧记录及旧 Worker 能否继续读取？是否真的需要 migration？ |
| Web 消费 | [api](../../PJM/web/src/api/)、index.ts barrel、组件、[三语 catalog](../../PJM/web/src/lib/i18n/) | 类型与运行时 validator 都同步了吗？缺失、拒绝、历史和正常状态如何呈现？ |
| 旁路使用者 | Schedule、CLI、mock browser、fixture、其他已知 client | 是否也生产/消费相同 payload？是否遗漏了非主页面入口？ |
| 派生契约 | [OpenAPI snapshot](../../PJM/contracts/openapi/projectmind-api.v1.json) | 是否由当前 app 生成并逐项一致，而不是手工补一个字段？ |

每条新 example 必须同时进入 [validator 注册表](../../PJM/scripts/validate_contracts.py)和 [Backend 契约测试](../../PJM/backend/tests/contracts/test_contracts.py)。JSON Schema 通过不会执行资源授权、计算内容 hash 或模拟数据库竞争；这些仍需业务测试。精确的强制同步规则见 [AGENTS.md](../../PJM/AGENTS.md#よくある変更の同期点)。

## 历史数据兼容不等于前后端版本兼容

| 情况 | 处理原则 |
| --- | --- |
| 新 API 读取没有新快照的旧 Run | 返回明确的历史不可用状态；保持原结果可读，不从当前资源补齐 |
| 新 Web 收到旧 API 的响应 | 按明确的兼容策略识别“不支持/信息不可用”或提示版本不匹配；不能把字段缺失默认成空集合。当前 Run detail 缺少必需清单字段时采用契约错误，不静默回退 |
| 旧 Web 收到新 API 增加的字段 | 检查旧 validator 是否拒绝额外字段；不能仅凭“JSON 可扩展”认定兼容 |
| API 与 Worker 读取不同持久格式 | 在上线前验证读写顺序和版本识别；旧消费者不能猜测未知快照格式 |
| 回退到旧版本 | 先核对旧版本能否读新数据、是否保留安全边界；不能靠删快照或降级校验完成回退 |

向后兼容的投影、新版本协议或明确协调升级，需按实际消费者选择并记录。工作副本尚未联调的字段不能直接用于独立升级一端。即使 Web/API 同批部署，也要考虑浏览器旧页面仍在打开、Worker 尚未退出和在途请求。

校验失败必须与“合法的空值”分开。允许缺少旧字段的兼容分支应只接受已知旧形状，并显示能力不可用；不要以宽泛 `any`、全字段 optional 或空数组默认值绕过整份响应校验。损坏的可选展示数据如何降级，由领域设计定义；身份、权限或必需协议错误不能降级为成功。

## 示例：Run 的冻结文档读取投影

语义正本是[资源快照：公开选择与读取投影](../design/resource-snapshots.md#公开选择与读取投影的实施契约)，精确文件映射见[Run 文档契约](../../PJM/contracts/README.md#run-文書契約を読む)。这条链路的生产/消费入口已有代码，下面说明每个责任应怎样验证；当前覆盖范围见[计划](../planning/roadmap.md#13-当前执行状态)，不从本表推导整项目或部署已验收。

| 检查 | 可观察的验收 |
| --- | --- |
| 输入与选择 | 即时执行和调度都确认实际范围；必需未选、集合不足两个或失效选择不能提交；可选未选不授权 |
| 服务端冻结与读取 | 创建固定成员；新上传不进入旧 Run；跨 Project、slot 或 checksum 不符的清单不向客户端公开 |
| 正常与历史投影 | `FROZEN` 才携带清单；`LEGACY_UNAVAILABLE` 和 `INVALID` 不携带成员，且不破坏其他合法结果的读取 |
| 摘要与完整信息 | detail/history 的 `selected_sources` 都遵守白名单；history 不复制完整成员或内部 binding scope |
| 消费与显示 | validator 区分三种状态；界面说明“创建时冻结”而不是“今天的文档目录”，三语表达同义 |
| 交付一致性 | 当前 route 输出、Schema、代表 example、OpenAPI 与 Web fixture 相符；不能只给 example 补一个空数组掩盖未完成的页面和兼容处理 |

这里的三种状态描述清单的可验证性，不表示 blob 今天仍可读取、物化已经完成或 Run 执行成功。文档摘要与执行结果是不同的事实。

## 怎样记录验证结论

命令与工作目录见[本地开发](local-development.md#変更に応じた検証)。按下面顺序解释结果，某一层通过不替代其他层：

1. 文档 build/check：链接、章节、嵌入示例和浏览版一致。
2. Schema/example 与 OpenAPI 一致性：已注册数据形状和 app 公共面一致。
3. Backend/Web 回归：业务负向校验、历史分支、运行时解析、类型与组件行为。
4. 有状态检查：真实 DB 的唯一约束、事务回滚、竞争、取消和恢复。
5. 用户链路：实际创建/重放/详情/调度、三语、键盘和窄屏；mock API 场景单独标注。
6. 配备与外部资源：匹配的 API/Web/Worker 版本、迁移、真实 Provider 与模型；使用经过确认的测试目标。

若发现工作副本尚未同步，保留已有改动，把失败命令、具体入口和待补齐消费者写入计划与新一条历史记录。文档整理可以修正表述和导航，但不应通过改应用契约、关闭校验或复用旧测试数字把当前状态写成绿色。
