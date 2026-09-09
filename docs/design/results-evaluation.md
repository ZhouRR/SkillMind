# 结果、证据与人工评价

本页定义模型交付、引用与追加评价。执行终态见 [Runtime](agent-runtime.md)，运行中回答见[普通交互](user-interactions.md)，远端事实见[受控效果](repository-effects.md)；当前缺口见[计划 R07](../planning/roadmap.md#r07-run-与审计)。

## 先分清四种事实

| 对象 | 含义与边界 |
| --- | --- |
| Run 终态 | 执行如何结束；SUCCEEDED 不等于业务全对、进程已停或费用结清 |
| Result / Outcome | 原模型交付；COMPLETED/PARTIAL/BLOCKED 是完整度，不是 Run 状态 |
| Evidence / Artifact | 依据与产物；引用合法不证明存在、归属、内容或现在可读 |
| Evaluation | 用户评分/判断/修订建议，追加保存，不改原值、不批准效果、不恢复 Run |

### 一个例子：执行结束，结论仍需修订

Run SUCCEEDED、Outcome PARTIAL、模型 confidence 0.8 与人工 2/5、partially_accurate 可以同时成立。confidence 不是校准正确率，单次 rating 不直接成为整体质量指标。两份 Evaluation 都针对原 Result，不互相覆盖或合并。

needs_review 不自动创建 REVIEW 或重开终态。采用建议、再次分析须另有明确操作；当前无“采用修订/重开/自动父子 Run”协议。

## 结果的形状与读取来源

一个 Run 最多一个 run_results，Evaluation 用 result_id 关联。data_json 公开为 detail.result.data，是修订原值根；外层 summary/validation/usage 是投影，不是原业务内容。

[OutcomeEnvelope](../../PJM/contracts/outcomes/envelope/v1.schema.json)区分交付物、发现、证据、限制、待确认与效果摘要。无业务 Schema 也必须有合法包络；只有冻结任务声明可选 Schema 才允许 structured_data，并严格用原 Schema，不取最新版本。

[Run detail](../../PJM/contracts/runs/detail/v1.schema.json)读取保存事实，不按今天的文档库或外部地址重拼原结果。Artifact 暂不能假定已有独立表、索引/下载 API 或任意 URL 解析能力。子任务的输出派生见[子分析](subagents.md#子任务指令与结果的边界)。

## 结果校验的实际保证

[ResultValidator](../../PJM/backend/src/projectmind/agent/result_validation.py)当前边界：

| 检查 | 实际保证与缺口 |
| --- | --- |
| 结构 | 包络及冻结业务 Schema 合法，不验证推理 |
| 敏感信息 | 递归可疑字段名，不证明普通文本无秘密 |
| Evidence | 收集 source_ref/evidence_refs 查同 Run，不证明支持结论或 blob 可读 |
| Proposal | 顶层引用同 Run，拒绝仍在批准/执行中的提案；未核对完整效果摘要 |
| Artifact | 顶层收集计数，无归属/hash/可读性查询；不能显示“已验证附件” |

effects[].before_ref/after_ref/proposal_ref 与 deliverables[].artifact_ref 尚未完整核验，effects 状态未逐项对比 EffectExecution。Web 一处显示模型摘要、另一处显示平台记录，尚未对应核对；数量或 APPLIED 文本不能冒充验证通过。

### 引用可信性的修正要求

1. 统一收集所有契约引用位置，校验同 Run 归属；同 Project 的另一个 Run 也不可借用。
2. 建立受控产物索引，证明归属、内容身份及保存后才开放读取，不把 art_ 前缀或模型 path/URL 当许可。
3. effects 是待核对声明；与 Proposal/Approval/EffectExecution/read-back 关联及状态一致才可确认，否则明确未证实。
4. 错误候选不保存为成功 Result，保留脱敏审计；旧结果只读并说明原校验范围，不补造通过。

扩大校验须同步主/子 validator、保存与 Web，审查冻结 Schema/混合 Worker/公开 validation 兼容，不改旧 Result 迎合新规则。

## 保存与显示不是同一个提交

候选只读校验 → 终态事务重验 lease/取消并保存允许的 Result、末次事件与 Outbox → 提交后 detail/SSE 各自读取 → 另一个 Evaluation 事务。

校验查询与终态不原子；锁后持久取消可覆盖成功候选。失败/取消可以无 Result，不能伪造空成功。此前 Tool Evidence 和后续评价均不属于终态事务；SSE 末次快照也不含完整 Result，须授权读取 detail。提交/停止边界见[执行监督](run-supervision.md#提交时谁决定最终状态)。

## 评价请求与历史

GET/POST 共用 /api/v1/projects/{project_id}/runs/{run_id}/evaluations；[请求 Schema](../../PJM/contracts/evaluations/v1/create-request.schema.json)规定 rating 1–5、verdict accurate/partially_accurate/inaccurate/uncertain，comment/revisions 可省略，最多 100 条修订，Web 仅提供一条。

POST 经 ProjectWriteActor/Origin/CSRF，user_id 来自 actor；业务事务验证 Run/Project 与 Result 存在，无 Result 返回 409，不额外以页面 SUCCEEDED 授权。认证与业务提交不是同一锁内事务。

每次成功 POST 新增一条，当前无请求幂等。GET 按 created_at/ID 返回全部、无分页；空列表是没有评价，不等于 Result 不可用。多用户/多次评价并存，不默认最后一条为正式结论。

## 修订指向哪份原值

pointer 相对 detail.result.data，服务端读取 original_value；客户端只提交建议和理由。

| 目标 | 指针 |
| --- | --- |
| 原包络摘要 | /summary，不是外层截短投影 |
| 包络业务字段 | /structured_data 下已存在的位置 |
| 旧结构化输出 | 原业务对象根，不擅加 structured_data |
| /data | 仅原内容真有 data 时有效，不代表 API 包装层 |

例如原包络存在 summary 时：

```json
{
  "rating": 2,
  "verdict": "partially_accurate",
  "revisions": [{
    "pointer": "/summary",
    "suggested_value": "材料不足，结论有限。",
    "reason": "原结果已列出限制。"
  }]
}
```

解析器要求 / 开头的既有目标，支持 ~0/~1；拒绝空指针、缺字段、无效数组位置、穿过标量及同次重复 pointer。合法 null 不是缺失；不是任意 JSON Patch，不新增/删除/追加目标。

suggested_value 可为任意 JSON，当前不再套业务字段 Schema、不合并 Result；类型不同仍只是建议。未来“采用”须另定义授权、版本、审计。

## 提交未知与界面责任

当前 [EvaluationSection](../../PJM/web/src/components/RunResultPanel.tsx)只有 state 禁用和卸载 abort，当前请求/context 校验、读写竞争尚未闭合，晚到列表可覆盖刚追加显示。

| 情况 | 修正要求 |
| --- | --- |
| 成功 | 核对 Project/Run/Result/Evaluation 身份，只追加一次显示 |
| 拒绝 | 分开输入错误、结果不可用、身份/权限错误，不统一自动重试 |
| 未知 | 断网/超时/abort/无效成功响应可能已提交；禁止自动 POST |
| 刷新/切换 | 相似历史不能证明原请求；旧 actor/Project/Run/Result 响应不更新新页或清空新草稿 |

当前无原请求确认接口。再次提交须明确是可能重复的新增，不删审计消重复；短期草稿只存内存，不进 URL/持久存储/日志，abort 不是撤销。

安全重放须先定义服务端绑定 actor/Result/原内容、唯一键及原记录确认，同键异内容拒绝；不能只加 header 声称幂等，也不回填虚构旧键。

## 兼容与开发接续

先补引用与验证范围投影，再补评价 Web 当前请求隔离/未知状态；重放、分页另定协议，同步 API/OpenAPI、Schema/example、validator、三语与浏览器。新包络与旧 STRUCTURED_OUTPUT 分别可读，损坏不变成空成功。唯一 Result 约束不等于禁止任意 UPDATE，追加式服务与 DB 运维保护分别验收。

## 验收条件

覆盖 PARTIAL/技术终态/人工判断分离、冻结 Schema、所有跨 Run/缺失引用、模型假称 APPLIED、两次评价原值不变、非法 pointer 整条回滚、合法 null/转义、提交丢响应与上下文切换。真实 commit/rollback 和浏览器时序分别验证；入口见[代码根 README](../../PJM/README.md)。
