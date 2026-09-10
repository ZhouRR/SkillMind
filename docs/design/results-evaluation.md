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

[Run detail](../../PJM/contracts/runs/detail/v1.schema.json)读取保存事实，不按今天的文档库或外部地址重拼原结果。Artifact 使用成功 Tool 的 Evidence 扩展保存，另经授权接口读取；模型 URL 或工作区路径不是下载身份。子任务的输出派生见[子分析](subagents.md#子任务指令与结果的边界)。

## 结果校验的实际保证

[ResultValidator](../../PJM/backend/src/projectmind/agent/result_validation.py)由主 Executor 和子收集器共用，首次查询前复制整个候选，避免等待期间被替换。当前边界：

| 检查 | 实际保证与缺口 |
| --- | --- |
| 结构 | 包络及冻结业务 Schema 合法，不验证推理 |
| 敏感信息 | 递归可疑字段名，不证明普通文本无秘密 |
| Evidence | source_ref/evidence_refs，以及 effects 的 before_ref/after_ref 均查同 Run；不证明支持结论或当前 blob 可读 |
| Proposal | 顶层与 effects.proposal_ref 合并核验同 Run，拒绝仍在批准/执行中的提案 |
| 效果声明 | 单条 SELECT 核对 Proposal、精确 Approval、EffectExecution、ToolCall 及 before/after Evidence；不是重新访问远端 |
| Artifact | 收集顶层与 deliverables.artifact_ref，查同 Run/原成功 Tool 的不可变字节、大小与 hash；缺失、错归属或损坏拒绝，不接受模型自造 art_ ID |

只按包络的明确位置解释 effects/artifact_ref，不把业务 structured_data 中任意同名字段或 URL 当作平台身份；原业务 Schema 的 source_ref/evidence_refs 约定保持不变。遗漏顶层数组不能绕过嵌套引用核验，保存的去重集合与计数包含所有已收集位置。

### 效果摘要核对到哪一步

| 声明 | 必须匹配的保存事实 |
| --- | --- |
| PROPOSED / REJECTED | 分别为无批准/执行的 DRAFT，或原 version/checksum 的拒绝批准且无执行；不得附加虚构 before/after |
| STALE / FAILED | 匹配原提案和执行的终局；批准前失效可以没有执行，VERIFICATION_FAILED 只映射 FAILED，不表示远端未变 |
| APPLIED | 原批准、Provider/version、请求身份、成功 ToolCall、精确 before/after、Evidence snapshot 的实际 hash，以及 READ_BACK 的完整匹配路径一致 |

模型提供 before/after 时须精确等于该执行记录，不能借同 Run 的其他 Evidence；即使省略，也核对平台 APPLIED 所需的两份保存证据。Tool 的结果摘要须与执行记录一致。未配置 lookup、缺记录、跨 Run/Project、坏关联或状态/内容矛盾拒绝为 effect_summary_invalid，不保存成功 Result；错误不包含原引用或候选正文。

此检查只确认保存时的平台记录一致，不证明原远端执行身份、语义摘要正确、当前远端值或完整停止/回滚。当前仅接受已注册的 Provider 版本；新增版本须保留旧版本的只读核验规则，不能改旧记录或以当前 Integration 配置重授历史权限。

### 引用可信性的修正要求

1. 保持主/子共享的完整位置收集与同 Run 核验，不允许同 Project 的另一个 Run 借用。
2. 只有原字节、归属/hash 与 Tool 成功回执一起提交后才交付 art_ 引用；可覆盖路径、hash/短 excerpt 不够，不以当前文件回填历史附件。
3. 将平台记录一致与[远端可靠性](repository-effects.md#可靠性修正要求)分别验收；不从本次引用核验推导远端精确执行已经完成。
4. 错误候选不保存为成功 Result，保留脱敏审计；旧结果只读并说明原校验范围，不补造通过。

扩大校验须同步主/子 validator、保存与 Web，审查冻结 Schema/混合 Worker/公开 validation 兼容，不改旧 Result 迎合新规则。

### 保存的验证范围

新 Result 的 validation.reference_checks 使用 projectmind.result-reference-checks/v2，分别声明 Evidence 同 Run、Proposal 归属/状态、包络效果与平台记录匹配；STRUCTURED_OUTPUT 的效果项为 NOT_APPLICABLE。Artifact 项为 RUN_OWNERSHIP_AND_CONTENT，并要求 artifact_refs_valid=true：只说明保存时引用的归属与实际字节通过，不承诺未来下载、文件安全或远端正确。

已保存 v1 的 Artifact 项保持 NOT_VERIFIED，不能改成 v2 或与 artifact_refs_valid=true 混用；旧结果不重新验证后覆盖原范围。没有附件时，v2 表示已检查的集合为空，不是发现了历史附件。

API 白名单投影保留原省略项，Web 校验后显示保存时范围；旧结果没有此字段时明确显示历史未记录，不从今天的记录补算通过。新字段缺项、未知版本、额外项、格式或结果类型不匹配，以及与原校验标志矛盾时拒绝读取，不裁剪成通过。旧 result.data 与历史 validation 不回写；升级须匹配 API/Web 并替换旧 Worker，旧 Worker 不执行这版完整检查。

## 可信附件的发布与读取

[workspace.write/v2](../../PJM/contracts/tools/workspace.write/v2/response.schema.json)保留受控 UTF-8 写入边界：workspace/ 是中间文件，artifact_refs 为空；output/ 每次写入产生一份附件。v1 仍只保存写入 Evidence，不自动获得 v2 权限。新系统 Interpreter 可生成 v2 声明，既有 SkillVersion/Run 的权限和 hash 不改写。

```text
原调用获准、保留原字节
  → 受控写入
  → 网关核参数、发号
  → 重验原执行权
  → 字节与成功回执一同保存
  → 提交确认后交付引用
  → 结果与检查点核验
```

[Artifact repository](../../PJM/backend/src/projectmind/artifacts/repository.py)复用 Evidence 的 Run/Tool/hash/时间，只增加私有字节与发布元数据。0030 模型预算与附件存量不同：单份最多 1 MiB，每 Run 最多 100 份、总计 10 MiB，由同一 Run 锁检查，跨 Segment/Attempt 累计。原路径被覆盖时发放新引用、保留旧字节；没有按路径更新或删除附件接口。

| 边界 | 处理 |
| --- | --- |
| 执行权失效、取消或候选校验失败 | 不发布附件；已经改变的工作区文件不因此回滚 |
| 保存失败或 commit 响应未知 | 不交付候选引用、不补写失败或重跑；原成功可在合法重放时读取原引用，未决保持未决 |
| 同 Project 的另一个 Run、缺失原 Tool/Attempt、非成功 Tool | 引用拒绝；原成功不能被改归当前 Attempt |
| 旧 Evidence/工作区文件 | 不补造附件身份或原字节，不重新采样签发历史回执 |

[读取接口](../../PJM/backend/src/projectmind/api/routes/artifacts.py)为 GET /api/v1/projects/{project_id}/runs/{run_id}/artifacts 及 /{artifact_ref}/content，使用当前 ProjectReadActor 与精确 Run 归属，允许授权历史读取。列表是最多 100 项的发布元数据，不证明正文现在完整；普通 Run detail 不自动载入私有字节。下载和引用核验都从同次有界 SELECT 取得原字节，再校验实际大小/hash/UTF-8，不读取当前 workspace、任意 URL 或模型指定存储地址。

越权/不存在沿原 Project/Run 404，未知引用为 artifact_not_found；损坏为 409 artifact_content_invalid，存储故障为 503 artifact_storage_unavailable，不公开内部异常。成功下载只作为 text/plain 附件，带 no-store、nosniff 和安全文件名；它不是 HTML 预览或二进制生成协议。

页面按授权索引显示已发布附件，区分最终结果采用与未采用；只有模型引用而无索引时保留文字，不生成下载入口。下载按实际字节限额、原大小/hash/UTF-8 核对，再交给浏览器；取消、30 秒期限及 actor/会话/Project/Run 切换使旧响应失效，临时对象 URL 及时回收。下载成功不表示在其他应用打开原文件安全。

[0040 migration](../../PJM/backend/migrations/versions/0040_evidence_artifacts.py)不回填旧行；任何新绑定字段非空都阻止丢列降级。附件字节随数据库备份，不依赖可变工作区恢复；发布与回退见[迁移审查](../operations/deployment.md#迁移与回退审查)。SQL/fake 回归和浏览器 mock 不替代真实 PostgreSQL 竞争、迁移与恢复验收。

## 保存与显示不是同一个提交

候选只读校验 → 终态事务重验 lease/取消并保存允许的 Result、末次事件与 Outbox → 提交后 detail/SSE 各自读取 → 另一个 Evaluation 事务。

校验查询与终态不原子；锁后持久取消可覆盖成功候选。失败/取消可以无 Result，不能伪造空成功。此前 Tool Evidence 和后续评价均不属于终态事务；SSE 末次快照也不含完整 Result，须授权读取 detail。提交/停止边界见[执行监督](run-supervision.md#提交时谁决定最终状态)。

## 评价请求与历史

[请求 Schema](../../PJM/contracts/evaluations/v1/create-request.schema.json)规定 rating 1–5、verdict accurate/partially_accurate/inaccurate/uncertain，comment/revisions 可省略，最多 100 条修订。每份评价独立针对原 Result，多用户和多次评价并存，不默认最后一条为正式结论。

评价页面使用以下独立协议；实现与验证范围在[计划](../planning/roadmap.md#r07-run-与审计)登记。路径均位于 /api/v1/projects/{project_id}/runs/{run_id} 下：

| 入口 | 责任 |
| --- | --- |
| POST /evaluation-submissions | [原请求](../../PJM/contracts/evaluations/v1/submission-request.schema.json)的 submission_key、精确 result_id 和评价内容一起提交；首次 201，同一原请求重放 200 |
| GET /evaluation-submissions/{submission_key}?result_id=… | 当前同一用户只读确认原记录；不创建评价，未见原记录为 404 evaluation_submission_not_found |
| GET /evaluations/page?limit=20&after=… | 同一 Result 的完整游标分页入口；不在前端筛选旧接口第一页 |
| 旧 GET/POST /evaluations | 保留旧响应形状和追加语义；旧 POST 没有原请求保证，旧记录不补造 submission_key |

新提交正文在原评价字段外必带非空 UUID submission_key/result_id。服务端将省略的 comment/revisions 规范化为 ""/[]，复制整个请求后，以共享 canonical JSON/hash 绑定原键、用户、Project/Run/Result、评分、判断、评论及有序修订。对象 key 顺序不影响身份，修订顺序和 JSON 值影响身份；不接受非 JSON 数值或无法编码的 UTF-8。

每个 Result/用户/submission_key 最多一条。相同键而内容不同返回 409 evaluation_submission_conflict，不覆盖或追加；请求 Result 与该 Run 的原 Result 不符为 409 evaluation_result_mismatch，无 Result 为 409 result_not_available。无论 Run 的技术终态如何，都不得只凭页面 SUCCEEDED 授权。

[成功回执](../../PJM/contracts/evaluations/v1/submission.schema.json)只含 project_id、run_id、submission_key 和原 Evaluation 的九个公开字段；客户端须核对原用户、Result、键和全部已发送内容。服务端读取原回执也核对保存内容与原请求 hash、修订原值与原 Result，不从相似历史推断成功。数据库提交确认前不交付成功；flush 不是 commit，提交异常后不补写失败或自行重发。[0041](../../PJM/backend/migrations/versions/0041_evaluation_submissions.py)的两列成对保存，旧评价不回填；任何新绑定存在时不得降级丢列。

### 评价的授权事务

POST 经 ProjectWriteActor/Origin/CSRF，user_id 只来自当前 actor。新旧写入、原提交确认及新分页均复用 UserAccess 和现有项目授权：Organization → User → 原 AuthSession → Project/成员，再读取原 Run/Result。只读 actor 锁与已有外键取锁兼容，不复制另一份凭据验证。

锁等待、领域读取以及 flush 后用新时刻重新验证原会话、当前账户/成员；首次、重放、未命中和领域拒绝都经过最终资格检查。写入要求 ACTIVE 项目，归档仍可授权读取历史/原回执；同一用户的新有效会话可人工查询，不把旧会话的凭据带入新请求。判定点是最终持锁检查，不承诺物理 commit 瞬间的会话期限或全系统立即停止。

### 历史分页

[响应](../../PJM/contracts/evaluations/v1/page.schema.json)固定为 project_id、run_id、result_id、items、next_cursor。limit 为 1–100，默认 20；按 created_at/evaluation_id 升序，以最后一项的 evaluation_id 继续，服务端只读 limit+1 项判定下一页。无下一页时 next_cursor 为 null。

游标须属于同一 Result；格式错误、空 UUID、不存在或跨 Result 均为 400 invalid_evaluation_cursor，不能借其他结果的位置。空 items 是没有当前页评价，不代表 Result 不可用。稳定排序不是跨页快照；并发追加时可重新读取完整历史，不伪造总数或偷偷忽略未加载页。

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

评价表单分开可编辑草稿、已发送原请求和历史；一次可添加多条修订，不自动采用建议。复用共享的同步防重、30 秒期限和当前请求检查，页面 owner 包含 actor、会话、Project、Run 和 Result。

| 情况 | 修正要求 |
| --- | --- |
| 成功 | 核对原回执与全部原内容，按 Evaluation ID 合并一次；晚到列表不覆盖刚确认的记录 |
| 拒绝 | 分开输入错误、结果不可用、身份/权限错误，不统一自动重试 |
| 未知 | 断网/超时/abort/无效成功响应可能已提交；冻结原键与内容，禁止自动 POST 或换键重试 |
| 只读确认 | 人工 GET 原回执；匹配才确认。404 只表示这次未见，不证明原 POST 未到达或回滚 |
| 显式重发 | 仍未确认时，用户可沿原键/原内容 POST，可能完成此前未落库的首次提交；不得混入新草稿 |
| 刷新/切换 | 同上下文切页签/重读详情保留未决；换 actor/会话/Project/Run/Result 后，旧成功、错误和 401 均不更新新页 |

历史刷新和相似评价不能解除未决状态；已知输入拒绝才允许修改草稿后另作新提交。同键冲突不提供“换键重试”捷径。短期草稿和原正文只存内存，不进 URL/持久存储/日志；离页/整页刷新不承诺恢复。

用户可保留原键并手动只读查询，没有原正文时不能据回执重造 POST。仅手输键的 GET 查询可结束并更换查询键，保留未发送草稿；结束时使旧查询响应立即失效，不表示撤销原评价。此出口不适用于本页已发送的未决 POST；abort 不是撤销。

## 兼容与开发接续

引用收集、效果记录核验、可信 UTF-8 Artifact 保存/读取与范围投影已接。评价的新提交/查询/分页作为独立公开契约交付，同步 API/OpenAPI、Schema/example、validator、三语与浏览器；不改变旧接口响应，不将旧无键记录认作可重放记录。新包络与旧 STRUCTURED_OUTPUT 分别可读，损坏不变成空成功。唯一 Result 约束不等于禁止任意 UPDATE，追加式服务、数据库迁移/竞争与运维保护分别验收。

## 验收条件

覆盖 PARTIAL/技术终态/人工判断分离、冻结 Schema、所有跨 Run/缺失引用、模型假称 APPLIED、两次评价原值不变、非法 pointer 整条回滚、合法 null/转义、提交丢响应与上下文切换。真实 commit/rollback 和浏览器时序分别验证；入口见[代码根 README](../../PJM/README.md)。
