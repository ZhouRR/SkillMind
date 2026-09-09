# 结果、证据与人工评价

> 定位：结果的含义、引用可信性、原值与人工修订，以及评价提交的失败边界。执行与终态见 [Runtime](agent-runtime.md)，运行中的提问见[普通交互](user-interactions.md)，外部写入见[受控效果](repository-effects.md)。实现状态与验证证据统一查[计划](../planning/roadmap.md#r07-run-与审计)。

本页不设计第二个执行器，不自动应用人工建议，不从评分推导 Skill 发布许可，也不把模型声明的效果当作外部系统事实。

## 先分清四种事实

| 看见的内容 | 它说明什么、不能说明什么 |
| --- | --- |
| Run 终态 | 执行线程如何结束。SUCCEEDED 不等于每个业务判断正确，也不替代进程停止或用量结清证明 |
| Result / Outcome | 保存的模型交付。COMPLETED、PARTIAL、BLOCKED 描述交付完整程度，不是 Run 状态或人工结论 |
| Evidence / Artifact | 前者用于追溯依据，后者是报告、补丁等产物。一个合法引用字符串不证明内容存在、归属正确或现在可下载 |
| Evaluation | 某位用户对同一 Result 的评分、判断和建议。追加记录，不覆盖原值、不批准外部写入、不恢复 Run |

### 一个例子：执行结束，结论仍需修订

一次分析按协议结束，Run 是 SUCCEEDED；因为材料不全，Outcome 是 PARTIAL，模型给出 confidence 0.8。人工核对后发现摘要有误，提交 2/5、partially_accurate，并建议修改 `/summary`。

这些值可以同时成立。confidence 是模型自述，不是平台校准过的正确率；rating 是一次人工评分，不直接变成 Benchmark 指标。页面并列展示原摘要和建议，原 Result 与终态仍保持不变。

needs_review 也只是结果中的待人工核对标记，不等于已经创建一个 OPEN 的 REVIEW 交互，不自动让终态 Run 回到等待状态。查看评价、回答运行中的问题和批准外部写入仍是不同操作。

```text
同一 Run
├── 原 Result：摘要与限制不变
├── Evaluation A：原值 → 建议甲
└── Evaluation B：原值 → 建议乙
    两条建议不自动合并或互相覆盖
```

若要让模型根据建议再做一次新目标，须明确创建新 Run。当前 Evaluation 没有“采用修订”“重开原 Run”或自动建立父子 Run 的协议。[JAF Benchmark](../acceptance/jaf-benchmark.md)另外管理样本、Gold、分母与人工发布判断。

## 结果的形状与读取来源

一个 Run 最多保存一个 Result，载体是 `run_results`；Evaluation 通过 result_id 关联它。`RunResult.data_json` 是原输出，公开为 detail.result.data，外层的 summary、validation、usage 等是独立投影字段，不是可由评价直接修改的业务内容。

新格式由 [OutcomeEnvelope](../../PJM/contracts/outcomes/envelope/v1.schema.json)定义：交付物、发现、证据、限制、待确认问题、效果摘要与可选业务数据分开。准确必填项以 Schema 为准；没有业务 Schema 时仍须合法包络，不能把任意文字当作结构化成功结果。

只有冻结的任务声明了可选输出 Schema，才允许 `structured_data`，并按那份精确 Schema 校验。未声明却返回该字段会被拒绝；子分析也不能仅因共用 validator 就被要求冒充完整主结果，派生协议见[子任务输出](subagents.md#子任务指令与结果的边界)。

公开读取使用 [Run detail 契约](../../PJM/contracts/runs/detail/v1.schema.json)与 [runs route](../../PJM/backend/src/projectmind/api/routes/runs.py)。Evidence 的 locator、hash、excerpt 是保存时的证据描述；输入文档清单是另一份冻结事实，不从今天的文档库或外部地址重新拼出“原结果”。Artifact 目前是引用/产物概念，不假设存在独立 artifact 表、公开下载入口或自动解析任意 URL 的能力。

## 结果校验的实际保证

现有确定性入口是 [ResultValidator](../../PJM/backend/src/projectmind/agent/result_validation.py)。下表来自工作副本的调用与查询范围，不是全链路验收结论。

| 检查对象 | 当前检查及边界 |
| --- | --- |
| 输出结构 | 校验包络、冻结输出 Schema，以及已声明的业务数据结构；不验证业务推理是否正确 |
| 敏感信息 | 递归检查可疑字段名；不能证明普通文本内容没有秘密，也不能替代输入、Tool 和发布边界的脱敏 |
| Evidence | 收集 source_ref / evidence_refs，并按同一 Run 查询；不证明证据真的支持该结论或 blob 仍可读取 |
| Proposal | 对顶层引用检查同一 Run 归属，并拒绝尚在批准/执行中的提案；不逐项核对模型的效果摘要 |
| Artifact | 当前只收集顶层引用并计数，未查询产物归属、内容 hash 或可读性。不能标为已验证附件 |
| 校验 metadata | 说明实际执行过的结构/引用检查；数量不是验证结果，不据此生成“所有结果可信”的标记 |

具体缺口：收集器没有把 `effects[].before_ref / after_ref` 当作 Evidence 引用核验；也没有把 `effects[].proposal_ref` 纳入 Proposal 查询或对比 EffectExecution 的实际状态。`deliverables[].artifact_ref` 同样没有完整归属检查。即使模型写 APPLIED、字符串满足 Schema，也不能据此宣称远端修改已完成。

现有 Web 的 OutcomeEnvelopeResult 直接显示包络中的 effects 文本，平台批准/执行记录另由 ControlledEffectsSection 展示；尚未逐项比对两份内容。后续须标明“模型声明”与“平台记录”的来源，冲突时显示未证实，不用相同状态文案掩盖差异。

### 引用可信性的修正要求

1. 新输出必须检查所有契约声明的引用位置；同一 Project 内另一个 Run 的 Evidence 也不可借用。引用发现与校验使用一个共享入口，主/子输出、保存与 Web 的含义同步。
2. Artifact 须由已授权的产物索引证明同一 Run 的归属、内容身份及保存结果，再开放受控读取。当前缺少这条链；不能只按 `art_` 前缀放行，也不能把模型提供的 path/URL 直接变成下载地址。
3. 模型的 effects 只是待核对声明，平台事实来自 Proposal / Approval / EffectExecution / read-back。保存新结果前验证关联和状态一致性；不得把缺失、冲突或尚未证实的声明展示为已执行成功。
4. 失败时阻止该候选成为成功 Result，保留已有审计，不在日志回显原输出。旧结果继续只读展示，并明确当时没有的验证不能补造为通过；完整状态/恢复规则仍归[受控效果](repository-effects.md)。

这是后续实现要求，不是在文档中把当前缺口修成了已实现保证。扩大校验范围也须评估历史冻结 Schema、混合 Worker 和公开 validation 投影，不修改旧 Result 来迎合新规则。

## 保存与显示不是同一个提交

```text
候选输出 → 结构及引用校验
                 ↓
终态事务：重验 lease 与取消意图
  ├── 保存允许的 Result 与终态事件
  └── 同事务保存 Outbox
                 ↓
提交成功 → detail / SSE 分别读取
                 ↓
人工评价 → 另一个追加事务
```

实际接线是 [Executor](../../PJM/backend/src/projectmind/worker/executor.py)调用 validator，再经 [RunService](../../PJM/backend/src/projectmind/runs/service.py)与 [RunRepository](../../PJM/backend/src/projectmind/runs/repository.py)终态化。校验的只读查询与终态保存不是一个事务；末次锁后发现持久取消时可以不保存候选 Result，而以 CANCELLED 收尾。失败或取消的 Run 可以没有 Result，不能在页面伪造一份空成功结果。

终态事务不包含此前每次 Tool 的 Evidence 保存，也不包含后续 Evaluation。SSE 的终态快照不携带完整 Result；页面仍要经授权读取 detail。先前 Evidence 的存在或一条局部测试通过都不能替代真实 commit/rollback 证明，停止边界见[执行监督](run-supervision.md#提交时谁决定最终状态)。

## 评价请求与历史

当前 GET / POST 共用 `/api/v1/projects/{project_id}/runs/{run_id}/evaluations`；[请求](../../PJM/contracts/evaluations/v1/create-request.schema.json)、[单条响应](../../PJM/contracts/evaluations/v1/evaluation.schema.json)和[历史列表](../../PJM/contracts/evaluations/v1/history.schema.json)各有独立 Schema。

- POST 经 ProjectWriteActor 验证 Session、Origin/CSRF 和 Project 权限，评价者 user_id 来自服务端 actor，不接受调用者冒填身份。
- Repository 在业务事务中确认 Run 属于请求的 Project，再查该 Run 的 Result。有 Run 但没有可评价 Result 返回 409；它检查的是 Result 存在，不额外以客户端显示的 SUCCEEDED 作授权条件。
- rating 为 1–5，verdict 为 accurate / partially_accurate / inaccurate / uncertain。comment 与 revisions 可省略；一次 API 请求最多 100 条修订，而现有 Web 表单只提供一条可选修订。
- 每次成功 POST 返回新 Evaluation，当前没有按原请求去重。多位评价者或同一人的多次判断可以并存，不默认“最后一条就是正式结果”。
- GET 返回全部记录，按 created_at、ID 排序；当前没有分页。空 items 表示该 Result 暂无评价，409 表示 Result 不可用，二者不能互换。分页扩展须同步响应、旧消费者和稳定排序，不先在 Web 截断成假完整历史。

权限依赖的认证事务与评价的业务提交分开，不能从 actor 参数推导撤权和写入已经在同一锁内原子化；横向边界见[认证与业务提交](authentication.md#认证与业务提交不是同一个事务)。

## 修订指向哪份原值

pointer 相对于保存的 Result **内容**，即 API 的 detail.result.data；不是整个 detail，也不是某条前序 Evaluation 的 suggested_value。服务端从该内容读取 original_value，客户端只提交建议与理由。

| 结果形状与指针 | 读取的原值 |
| --- | --- |
| 包络的 /summary | 原模型摘要，不是外层截短的 summary 投影 |
| 包络的业务字段 | 从 /structured_data 开始，再接已存在的字段/数组位置 |
| 历史结构化输出 | 从原业务对象根开始，不擅自加 /structured_data 前缀 |
| 以 /data 开始 | 只在原内容真的有 data 字段时有效；不是 API 包装层的快捷方式 |

例如，原包络确有 summary 时，以下是完整、无真实业务数据的评价请求示例：

```json
{
  "rating": 2,
  "verdict": "partially_accurate",
  "comment": "应说明材料不足。",
  "revisions": [
    {
      "pointer": "/summary",
      "suggested_value": "材料不足，结论有限。",
      "reason": "原结果已列出限制。"
    }
  ]
}
```

当前解析器只接受以 `/` 开始且存在的目标，支持 `~0` / `~1` 转义；空指针、缺失字段、无效数组位置、穿过标量都会拒绝，同一评价中重复 pointer 也拒绝。原值可以合法地是 null，不能把 null 当作“找不到”。这是字段修订建议，不是任意 JSON Patch：不新增目标，不执行删除/追加操作。

suggested_value 是任意 JSON，当前不会重新套用原业务字段 Schema，也不会合并到 Result；不同类型的建议仍只是建议。若未来引入“采用”，须另设计授权、版本和审计，不能复用本接口暗中修改原记录。

## 提交未知与界面责任

现有 [EvaluationSection](../../PJM/web/src/components/RunResultPanel.tsx)经 [evaluations client](../../PJM/web/src/api/evaluations.ts)提交，用 state 禁用按钮、卸载时 abort。读取和写入成功回调尚未完整核对当前请求/上下文；原列表的晚到响应还可能覆盖刚追加的本地显示。不能把这些保护描述为完整的防重、切换隔离或故障恢复。

| 用户看到的情况 | 后续界面必须保证 |
| --- | --- |
| 已确认成功 | 校验返回的 Project 上下文及 Run / Result / 评价身份，追加一次显示；原 Result 不变 |
| 明确拒绝 | 400 的修订错误、409 的结果不可用、401/403 的会话或权限问题分别提示，不当作同一种重试 |
| 响应未知 | 网络断开、超时、abort 或无法校验的成功响应不证明没提交。禁止自动重发，明确可能已追加 |
| 刷新历史 | 可帮助用户查看事实，但内容、时间或条数相似不能证明是哪次请求，也不能证明没有晚到提交 |
| 切换或离页 | 旧 actor / Project / Run / Result 的响应不得写入新页面或清空新草稿；不把中止 fetch 当作撤销评价 |

当前没有原请求确认接口。需要再提交时，必须由用户明确决定新增一条并理解可能重复，不能以“重试”偷偷再次 POST；不通过删除审计行消除重复。短期草稿只存内存，评价内容、原结果和 token 不进入 URL、浏览器持久存储或共享日志。

若后续提供安全重放，应先定义由服务端绑定的原评价身份、actor/Result 作用域、规范化原内容、唯一约束与原记录重放；同键异内容拒绝，用户明确新增仍生成另一条记录。现有 Schema/HTTP 尚未声明这个协议，不能只加一个客户端 header 就声称幂等，也不回填历史记录的虚构请求键。

## 兼容与开发接续

1. 先补齐结果引用核验及验证范围的明确投影，保持新包络与历史 STRUCTURED_OUTPUT 分别可读。未知/损坏内容显示契约问题，不改写成空成功包络。
2. 分开验证终态事务与评价事务：取消竞争、回滚、结果不存在、同 Run 多评价、原值不变。当前唯一 Result 约束不等于数据库任意 UPDATE 都被禁止；追加式服务与数据库运维保护分别验收。
3. 补评价 Web 的当前请求隔离、读写竞争与结果未知状态，再按独立协议设计重放和分页；同步 API/OpenAPI、Schema/example、运行时 validator、三语及浏览器测试。
4. 沿 [Backend 接线](../../PJM/backend/README.md#結果検証と人工評価を追う)、[契约入口](../../PJM/contracts/README.md#結果と人工修訂の契約を読む)、[Web 接线](../../PJM/web/README.md#結果と人工評価を接続する)实施。验收当前范围见计划 [R07](../planning/roadmap.md#r07-run-与审计) / [R10](../planning/roadmap.md#r10-全部-web-页面)，质量判断另归 [R12](../planning/roadmap.md#r12-业务质量验收)。

## 验收条件

| 给定条件与操作 | 必须观察到的结果 |
| --- | --- |
| 合法 PARTIAL 包络 | 技术终态、交付完整性、模型 confidence 与人工评分分别显示，不自动改成 FAILED 或业务全对 |
| 未声明业务 Schema | structured_data 被拒绝；已声明时严格使用冻结 Schema，不取最新版本代替 |
| 缺失或跨 Run 引用 | 所有契约引用位置均检查；Artifact、效果摘要不能只靠格式或计数通过 |
| 模型声称 APPLIED | 必须对应受控效果事实；错误引用/状态不被当作外部成功，也不触发新的外部写入 |
| 同一 Result 两次评价 | 两条独立记录的 original_value 都来自原 Result，彼此不覆盖；旧 Result byte/内容不变 |
| 不合法或重复 pointer | 拒绝整条评价，不留下部分修订；合法 null 和转义键仍可读取 |
| 提交后丢响应/晚到响应 | 不自动重复 POST、不清空新上下文；历史刷新不能冒充幂等确认 |
| 真实事务与浏览器 | 单独验证 commit/rollback、并发、三语、键盘和窄屏。mock repository、静态渲染与文档浏览不替代这些证据 |
