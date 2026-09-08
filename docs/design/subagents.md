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

预算由主/子执行共同拥有，不应仅在子 Agent 规范内定义。计量口径、持久账户、原子预留、结算和恢复统一见[Run 预算与执行限额](run-budgets.md)；本节保留旧链接入口。

当前 `split_budget` 只把每次传入的父快照上限整除分配。dispatch 返回的 turns/output 是分配值，不是实际消耗；美元上限也没有按分支拆分。连续 dispatch、主子混合执行和跨 Attempt/Segment 均不能由此得到累计保证。

子分析接入共享预算时，须在启动每支前取得有效预留，并在完成、超时、取消后结算。Session 记录失败可以省略不可查询的 ID，但预算持久化失败不能沿用这种“保留结果后忽略”的处理方式继续收费执行。具体失败规则和验收只在预算规范维护。

## 验收

| 范围 | 必须证明 |
| --- | --- |
| 权限 | 子能力不超出父集合，拒绝写入/交互/递归 |
| 预算 | 单次分配与全 Run 累计分别验证；后者当前待实现 |
| 失败 | 部分失败不会伪造全面完成；取消清理子执行 |
| 审计 | 可查询的 Session ID 真实存在，缺失明确表达 |
| 模型效果 | 适时拆分独立工作，引用证据汇总，并写出未覆盖范围 |
