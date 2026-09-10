# 并行子分析

子分析属于一个 Run，不改变主目标/Session/审批链。共享额度见[Run 预算](run-budgets.md)，停止见[执行监督](run-supervision.md)；当前实现与接续范围见[计划](../planning/roadmap.md)。

## 责任与数据流

PRIMARY 调用 subagent.dispatch/v1 → 多个只读子分析 → 结果/失败摘要与子 Session ID → Gateway 保存主 Session 的一条 ToolCall/Evidence → 返回主 Agent 汇总。

子 Session 为 SUBAGENT/BRANCH，共用父 RunAttempt；数据库只限制活动 PRIMARY 唯一，子支不写独立 RunEvent sequence，不成为第二执行器。

## 一个例子：完成的是哪一层

A 完成配置检查，B 读日志后失败，两支 Session 与 Gateway 审计都保存：

| 看到什么 | 只说明什么 |
| --- | --- |
| Tool success | 整组符合返回协议，不是每支成功 |
| A COMPLETED / B FAILED | 仅 A 有有效结论，主结果保留 B 未完成的限制 |
| Session 关闭 | 生命周期已保存，不保证进程停/预算结清 |
| 主 Run 仍执行 | 主 Agent 还需综合证据与提交 Result |

结论已有但 Session 保存未知时，v1 只能 Tool 错误，不能省略必需 ID；不自动再跑收费分支补数据。

## 能力与故障边界

[resolve_subagent_capabilities](../../PJM/backend/src/projectmind/agent/subagent.py)是唯一权限决定点：子能力不超父集合，排除 write、effect/propose、interaction、递归 dispatch；显式要求禁止能力时拒绝整组，不静默裁剪。

单次最多 4 支，各自 timeout，TaskGroup 持有全组并等待取消清理。单支失败可保留其余结论，但 Session/Gateway 审计失败是整组错误；coroutine 退出不等于真实进程停止。

## 当前返回值的可信边界

[Provider](../../PJM/backend/src/projectmind/agent/subagent_provider.py)和[收集器](../../PJM/backend/src/projectmind/agent/subagent_result.py)已有以下接线：

| 边界 | 当前处理 |
| --- | --- |
| 终端/结果 | 唯一有效终端、正常收尾后调用共享 ResultValidator；中途文字不是成功 |
| 失败 | ENGINE_FAILED/SESSION_INTERRUPTED、非法等待/提案、空流、冲突终端或终端后事件拒绝；本支 deadline 为 TIMED_OUT |
| 身份/顺序 | 验 Run/Attempt、SDK Session identity 与本支 sequence |
| Session | recorder/validator 必需；保存异常、ID 数量/格式错按 unavailable，不省略 |
| 摘要 | 已验证 summary，最多 20,000 字符；Gateway UTF-8 响应上限另验 |
| 未闭合部分 | 子 prompt 仍继承父 Schema/Brief/checksum；budget 是分配值、cost 空，未接共享账本 |

主/子共用 stream 清理入口不等于同提交顺序；真实 SDK 退出另验。

## 完成、失败与审计如何表示

COMPLETED 必须有符合该执行输出要求的有效终端，不是“无 Python 异常”。失败保留安全原因与已知 Session identity，不拿中途文字凑摘要。执行结论、Session 审计完整性、消费结清分别判断。

目标可在版本化协议下保留已验证结论并说明 Session 审计缺口；当前 v1 不支持，不能伪造 ID，也不能把预算保存变为 best-effort。收费执行身份必须先取得，不能等事后 Session 行产生。

### 子任务指令与结果的边界

当前 derive_child_context 只改 prompt、权限、Tool 与局部分配；SDK output_format/validator 仍读父 result_schema，task_brief/checksum 与 engine_options_checksum 仍属父配置。局部子任务可能被迫输出完整父结果，也可能丢必需规则；父 hash 不证明实际子指令/options。

| 目标责任 | 要求 |
| --- | --- |
| 子指令 | 从冻结来源派生，保留必需规则/出处，只收窄目标/能力；记录 dispatch/branch 与实际模型指令依据/checksum |
| 子输出 | 平台版本化只读分析结果，表达结论/证据/未覆盖范围，不生成父全部交付或效果 |
| 共享校验 | 复用敏感信息/引用归属/结构校验，不复制宽松 validator 或接受模型自选 Schema |
| 主结果 | 综合各支、保留失败/未知，仍满足原 OutcomeEnvelope/任务 Schema |

子指令不是新 Run/Segment，不覆盖父 Brief。prompt、SDK 格式、validator、审计与汇总引用同一子契约版本；即使 response 外形不变也要兼容审查，不先把目标字段塞进 v1。

### 提交顺序与版本兼容

```text
各支终端检查 → 关闭 stream → 结果校验
  → 全组收束
  → TX A：保存整组 Session
  → Provider response / Evidence 草稿
  → Gateway 引用/敏感信息/Schema/大小校验
  → TX B：Tool response / Evidence → 主 Session
```

A 成功、B 失败可留下 Session 而无成功 Tool 结果，不删行、不自动重跑；DB 锁内不等模型。SDK Session UUID 不等于平台行 ID，格式也不证明存在。

[v1 response](../../PJM/contracts/tools/subagent.dispatch/v1/response.schema.json)要求每支 agent_session_id，保存失败保持拒绝。审计降级另定可表达缺口的版本，同步 registry/Provider/Gateway/Evidence/Web/历史；旧消费者不得接收新结构。Tool 审计不可省略，提交未知查原调用。Session 重放、取消记录与跨事务恢复还须补齐。

主/子 Gateway 均绑定父 Worker 的[原执行权](run-supervision.md#tool-调用的提交与重放)。已有未决 dispatch 不重新启动整组，B 提交未知不补写失败；此保护不等于 A 的 Session recorder 已具备相同 fencing 或停止回执。

## 预算现状与修正设计

split_budget 每次重拆父冻结 turns/output，美元上限不拆，返回分配不是消费；连续 dispatch、主子混合和跨 Attempt/Segment 无累计保证。内部账本已有但 Provider 未调用。

目标全组原子预留后逐支启动，完成/超时/取消分别结算或保留未知。主模型等 Tool 不等于释放原承诺，不可复制父可花费额度；账本不可用拒绝新收费。规则唯一来源见[预留协议](run-budgets.md#原子性与重复请求)。

## 开发接续顺序

保持已有终端/validator/Gateway/整组清理回归；补停止核对、Session/Tool 未知提交/重放及子指令版本，接通可信计量与创建/主子统一预算后才开放新子协议。旧结果不覆盖，修异常不扩权/扩额。

## 验收

覆盖缩权与显式禁止、空流/冲突终端/非法等待、部分失败、复杂父 Schema 的独立子产出、实际 prompt/options 身份、v1 ID 可查询、A/B 提交失败与不重跑、主子同时占用/连续 dispatch/未知费用。入口：[Gateway 生命周期回归](../../PJM/backend/tests/agent/test_subagent_lifecycle.py)；真实 DB、SDK 和模型汇总效果分别验收。
