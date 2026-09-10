# 并行子分析

子分析不改变父 Run 的目标、主 Session 或审批链。额度见[预算](run-budgets.md)，停止见[监督](run-supervision.md)，状态见[计划](../planning/roadmap.md)。

## 责任与数据流

PRIMARY 调用 subagent.dispatch/v1 → 多个只读子分析 → 结果/失败摘要与子 Session ID → Gateway 保存主 Session 的一条 ToolCall/Evidence → 返回主 Agent 汇总。

SUBAGENT/BRANCH 共用父 RunAttempt，不另写 RunEvent sequence；唯一活动限制仅针对 PRIMARY。

## 一个例子：完成的是哪一层

A 完成配置检查，B 读日志后失败，两支 Session 与 Gateway 审计都保存：

| 看到什么 | 只说明什么 |
| --- | --- |
| Tool success | 整组符合返回协议，不是每支成功 |
| A COMPLETED / B FAILED | 仅 A 有有效结论，主结果保留 B 未完成的限制 |
| Session 关闭 | 生命周期已保存，不保证进程停/预算结清 |
| 主 Run 仍执行 | 主 Agent 还需综合证据与提交 Result |

Session 保存未知时，v1 仍须 Tool 错误，不省略必需 ID 或自动重跑补数据。

## 能力与故障边界

[resolve_subagent_capabilities](../../SKM/backend/src/skillmind/agent/subagent.py)唯一决定权限：不超父集合，排除 write/effect/propose/interaction/递归 dispatch；显式要求禁用能力则拒绝整组，不裁剪。

最多 4 支，各有 timeout，TaskGroup 持有全组并等取消清理。单支失败可留其余结论，审计失败则整组错误；coroutine 退出不证明进程停止。

## 当前返回值的可信边界

[Provider](../../SKM/backend/src/skillmind/agent/subagent_provider.py)/[收集器](../../SKM/backend/src/skillmind/agent/subagent_result.py)的边界：

| 边界 | 当前处理 |
| --- | --- |
| 终端/结果 | 唯一有效终端、正常收尾后调用共享 ResultValidator；中途文字不是成功 |
| 失败 | ENGINE_FAILED/SESSION_INTERRUPTED、非法等待/提案、空流、冲突终端或终端后事件拒绝；本支 deadline 为 TIMED_OUT |
| 身份/顺序 | 验 Run/Attempt、SDK Session identity 与本支 sequence |
| Session | recorder/validator 必需；保存异常、ID 数量/格式错按 unavailable，不省略 |
| 摘要 | 已验证 summary，最多 20,000 字符；Gateway UTF-8 响应上限另验 |
| 未闭合部分 | 子 prompt 仍继承父 Schema/Brief/checksum；budget 是分配值、cost 空，未接共享账本 |

主/子虽共用清理入口，提交顺序不同。

## 完成、失败与审计如何表示

COMPLETED 须有效终端/结果，不是无异常；失败保留安全原因/已知 Session identity，不用中途文本凑摘要。结论、审计、结清分别判断。

将来可版本化表达“结论有效但 Session 审计缺口”，v1 不支持，不伪造 ID 或降级预算保存。收费身份在启动前取得，不靠事后 Session 行补造。

### 子任务指令与结果的边界

derive_child_context 只改 prompt/权限/Tool/局部额度；SDK 格式和 validator 仍用父 Schema，Brief/options checksum 仍属父配置。子任务可能被迫交完整父结果或丢规则，父 hash 不证明实际子指令。

| 目标责任 | 要求 |
| --- | --- |
| 子指令 | 从冻结来源派生，保留必需规则/出处，只收窄目标/能力；记录 dispatch/branch 与实际模型指令依据/checksum |
| 子输出 | 平台版本化只读分析结果，表达结论/证据/未覆盖范围，不生成父全部交付或效果 |
| 共享校验 | 复用敏感信息/引用归属/结构校验，不复制宽松 validator 或接受模型自选 Schema |
| 主结果 | 综合各支、保留失败/未知，仍满足原 OutcomeEnvelope/任务 Schema |

子指令不另建 Run/Segment、不覆盖父 Brief。prompt、SDK 格式、validator、审计/汇总共用子契约版本；外形不变也须兼容审查，不先塞字段进 v1。

### 提交顺序与版本兼容

```text
各支终端检查 → 关闭 stream → 结果校验
  → 全组收束
  → TX A：保存整组 Session
  → Provider response / Evidence 草稿
  → Gateway 引用/敏感信息/Schema/大小校验
  → TX B：Tool response / Evidence → 主 Session
```

A 成功、B 失败可留 Session 而无成功 Tool，不删行/重跑，锁内不等模型。SDK UUID 非平台行 ID，格式不证明存在。

[v1](../../SKM/contracts/tools/subagent.dispatch/v1/response.schema.json)要求每支 agent_session_id，保存失败拒绝；审计降级须新版本并同步所有消费者/历史。Tool 不省略，提交未知查原调用；Session 重放/取消/跨事务恢复尚缺。

主/子 Gateway 共用父 Worker [原执行权](run-supervision.md#tool-调用的提交与重放)，未决 dispatch 不重启、B 未知不补失败；不代表 A 的 recorder 已有同等 fencing/停止回执。

## 预算现状与修正设计

split_budget 每次重拆父 turns/output、不拆美元，分配非消费，Provider 未接账本。须按[预留协议](run-budgets.md#原子性与重复请求)整组原子预留后启动、逐支结算/保留未知；主模型等 Tool 不释放承诺，账本不可用拒绝收费。

## 开发接续顺序

先补停止/审计恢复和子指令版本，接通可信计量/统一预算后再开放新协议；保留既有终端/validator/Gateway/清理门禁，旧结果不改、权限额度不扩。

## 验收

覆盖缩权与显式禁止、空流/冲突终端/非法等待、部分失败、复杂父 Schema 的独立子产出、实际 prompt/options 身份、v1 ID 可查询、A/B 提交失败与不重跑、主子同时占用/连续 dispatch/未知费用。入口：[Gateway 生命周期回归](../../SKM/backend/tests/agent/test_subagent_lifecycle.py)；真实 DB、SDK 和模型汇总效果分别验收。
