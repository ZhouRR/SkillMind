# 并行子分析

> 现行实现与预算修正设计。并行子分析属于一个 Run，不改变其目标、主 Session 或审批链。

## 责任与数据流

```text
PRIMARY Session ── subagent.dispatch/v1
                          ├── 子分析 A（只读）
                          ├── 子分析 B（只读）
                          └── 子分析 C（只读）
                                      ↓
                         结果/失败摘要 + 子 Session ID
                                      ↓
                     主 Session 的一条 ToolCall + Evidence
```

子 Session 使用 `SUBAGENT / BRANCH`，共享父 RunAttempt。数据库仅限制 PRIMARY 同时唯一活动，不把子分支当作新的主执行。子 Agent 不独立写 RunEvent sequence。

## 能力与故障边界

- 子能力不得超出主能力集合，且排除 write、effect/propose、interaction 与再次 dispatch。
- 唯一决定点为 [resolve_subagent_capabilities](../../PJM/backend/src/projectmind/agent/subagent.py)；显式请求禁止能力时拒绝整个请求，不能静默裁掉后继续。
- 单次最多 4 个分支；每个分支有独立超时，取消应终止整组。
- 单路失败返回失败摘要，其余结果可以保留。Web 必须区分完成和未完成范围。
- Session 持久化失败时实现会省略无法查询的 ID，结果不因此全部丢弃；这代表审计明细不完整，不能声称每次分支都有完整持久记录。

[Provider](../../PJM/backend/src/projectmind/agent/subagent_provider.py) · [Session 记录](../../PJM/backend/src/projectmind/agent/subagent_sessions.py) · [Tool 契约](../../PJM/contracts/tools/subagent.dispatch/v1/request.schema.json)

## 预算现状与修正设计

当前 `SubagentDispatchProvider.execute` 把 `parent.limits.max_turns/max_output_bytes` 传给 `split_budget`。一次 dispatch 内采用整除分配，所有分支的分配总和不超过这次传入的上限。

但这些值是冻结上限，不是扣除主 Agent 与之前 dispatch 消耗后的实时余额。实现中没有 Run 共享账本，也没有主 Session 扣减。因此“全 Run 总预算已经严格切分”是未实现的保证；多次 dispatch 和主子混合执行会绕过这种文档层面的总额假设。

后续修正必须形成统一预算协议：

1. Run 级预算服务维护 consumed/reserved/remaining，并把主 Session 与所有子分支记入同一账户。
2. dispatch 先原子预留，再分配各分支；并发 dispatch 不得读到同一份可重复使用的余额。
3. 结束、超时和取消按实际用量结算，未消耗额度可释放；同一结算不能重复扣减或重复返还。
4. Segment/Attempt 重试不能恢复成完整 Run 上限。恢复使用持久账本或可验证的审计累计值。
5. SDK 允许的单会话上限只是局部防线。跨会话的成本、turn 和输出额度语义需分别定义，不能用输出字节代表模型成本。
6. 扩展前补上“连续两次 dispatch”“主子共同消耗”“并发预留竞争”“取消/重试结算”的有状态测试。

该修正是[计划 §13.2](../planning/roadmap.md#132-下一步与当前决策)的优先项，不能以文档描述或一次 dispatch 的局部测试宣称全 Run 预算已落实。

## 验收

| 范围 | 必须证明 |
| --- | --- |
| 权限 | 子能力不超出父集合，拒绝写入/交互/递归 |
| 预算 | 单次分配与全 Run 累计分别验证；后者当前待实现 |
| 失败 | 部分失败不会伪造全面完成；取消清理子执行 |
| 审计 | 可查询的 Session ID 真实存在，缺失明确表达 |
| 模型效果 | 适时拆分独立工作，引用证据汇总，并写出未覆盖范围 |
