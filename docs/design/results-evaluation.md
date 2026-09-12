# 结果、证据与人工评价

本页定义模型交付、引用与追加评价。执行终态见 [Runtime](agent-runtime.md)，运行中回答见[普通交互](user-interactions.md)，远端事实见[受控效果](repository-effects.md)；当前缺口见[计划 R07](../planning/roadmap.md#开发任务)。

## 先分清四种事实

| 对象 | 含义与边界 |
| --- | --- |
| Run 终态 | 执行如何结束；SUCCEEDED 不等于业务全对、进程已停或费用结清 |
| Result / Outcome | 原模型交付；COMPLETED/PARTIAL/BLOCKED 是完整度，不是 Run 状态 |
| Evidence / Artifact | 依据与产物；引用合法不证明存在、归属、内容或现在可读 |
| Evaluation | 用户评分/判断/修订建议，追加保存，不改原值、不批准效果、不恢复 Run |

### 一个例子：执行结束，结论仍需修订

Run SUCCEEDED、Outcome PARTIAL、confidence 0.8、人工 2/5/partially_accurate 可同时成立；confidence 非校准正确率，单次 rating 非整体质量。多份评价均指原 Result，不合并。needs_review 不自动提问/续行；采用修订、重开、自动父子 Run 尚无协议。

## 结果的形状与读取来源

每 Run 最多一份 run_results，Evaluation 关联 result_id。detail.result.data 是原值根；外层 summary/validation/usage 是投影。

[OutcomeEnvelope](../../SKM/contracts/outcomes/envelope/v1.schema.json)包含交付物、发现、证据、限制、待确认和效果。无业务 Schema 也需包络；structured_data 仅在冻结任务声明 Schema 时允许，并使用原 Schema。

[detail](../../SKM/contracts/runs/detail/v1.schema.json)读保存事实，不从当前资源重拼。Artifact 依成功 Tool Evidence 扩展保存/授权读取，模型 URL/工作区路径不是下载身份；子输出见[子分析](subagents.md#子任务指令与结果的边界)。

## 结果校验的实际保证

[ResultValidator](../../SKM/backend/src/skillmind/agent/result_validation.py)由主/子共用，首次查询前复制完整候选：

| 检查 | 实际保证与缺口 |
| --- | --- |
| 结构 | 包络及冻结业务 Schema 合法，不验证推理 |
| 敏感信息 | 递归可疑字段名，不证明普通文本无秘密 |
| Evidence | source_ref/evidence_refs，以及 effects 的 before_ref/after_ref 均查同 Run；不证明支持结论或当前 blob 可读 |
| Proposal | 顶层与 effects.proposal_ref 合并核验同 Run，拒绝仍在批准/执行中的提案 |
| 效果声明 | 单条 SELECT 核对 Proposal、精确 Approval、EffectExecution、ToolCall 及 before/after Evidence；不是重新访问远端 |
| Artifact | 收集顶层与 deliverables.artifact_ref，查同 Run/原成功 Tool 的不可变字节、大小与 hash；缺失、错归属或损坏拒绝，不接受模型自造 art_ ID |

effects/artifact_ref 只解释包络明确位置，不把业务同名字段/URL 当身份；source_ref/evidence_refs 原约定不变。顶层与嵌套均收集、去重计数，省略顶层不能绕过核验。

### 效果摘要核对到哪一步

| 声明 | 必须匹配的保存事实 |
| --- | --- |
| PROPOSED / REJECTED | 分别为无批准/执行的 DRAFT，或原 version/checksum 的拒绝批准且无执行；不得附加虚构 before/after |
| STALE / FAILED | 匹配原提案和执行的终局；批准前失效可以没有执行，VERIFICATION_FAILED 只映射 FAILED，不表示远端未变。带 effect_result_unknown 的待核对执行不匹配确定失败摘要 |
| APPLIED | 原批准、Provider/version、请求身份、成功 ToolCall、精确 before/after、Evidence snapshot 的实际 hash，以及 READ_BACK 的完整匹配路径一致 |

模型 before/after 必须精确匹配原执行，省略也须核对 APPLIED 两份证据；Tool 摘要与执行一致。lookup 缺失、跨 Run/Project、坏关联或事实矛盾均为 effect_summary_invalid，不保存成功，也不泄露原引用/正文。

这只证明保存的平台记录一致，不证明远端原执行、当前值、语义正确或停止/回滚。仅接受已注册 Provider 版本，升级须保留旧版只读核验，不改历史或用当前 Integration 重授权限。

### 引用可信性的修正要求

主/子共享完整位置与同 Run 校验；art_ 必须由原字节/归属/hash/成功回执一起发布，不用当前文件补历史。平台记录一致与[远端可靠性](repository-effects.md#可靠性修正要求)分开验收；错误只留脱敏审计，旧结果不补造通过。

扩大校验须一起审查主/子、保存/Web、冻结 Schema、混合 Worker 与 validation 兼容。

### 保存的验证范围

validation.reference_checks 使用 skillmind.result-reference-checks/v2：Evidence 同 Run、Proposal 归属/状态、包络效果与平台记录匹配；STRUCTURED_OUTPUT 效果为 NOT_APPLICABLE。Artifact 为 RUN_OWNERSHIP_AND_CONTENT 且 artifact_refs_valid=true，只证明保存时归属/字节，不承诺未来下载、安全或远端正确。

旧 v1 Artifact 仍为 NOT_VERIFIED，不混用 true、不升级覆盖；v2 无附件表示已检空集。API 保留原省略项，Web 显示保存时范围，历史缺字段标未记录。

缺项、未知版本、额外项、格式/结果类型/标志矛盾拒绝读取，不裁剪通过。升级配套 API/Web 并替换旧 Worker，不回写 result.data/validation。

## 可信附件的发布与读取

[workspace.write/v2](../../SKM/contracts/tools/workspace.write/v2/response.schema.json)仅受控 UTF-8：workspace/ 是中间文件、artifact_refs 为空；output/ 每次产生新附件。v1 仅存 Evidence，不自动升级；Interpreter 可生成 v2 声明，旧 SkillVersion/Run 权限/hash 不改。

`document.convert/v1` 的显式 `publish_artifact=true` 是另一种附件来源：Worker 将返回的完整 Markdown UTF-8 原字节作为 Run Artifact 保存，不经过模型抄写或可变工作区文件。原 Excel Evidence 与 Markdown Artifact Evidence 分开，分别保留原文及转换后 hash；响应附带 `artifact` 的 path/size/hash 和提交后签发的 `artifact_refs`。默认省略或 false 保持普通转换响应且不占附件配额。此选项仅保存内部审计附件，不直接上传 MinIO，也不授予 `document.write`；备份仍使用该原 Artifact 经精确批准执行，保存 MIME 可按 Markdown 提案，原字节不转换。

Gateway 与 audit 同时核对显式选项、原转换 Tool、来源 ID/hash、转换器、两份 Evidence、全文字节和响应；缺失、暗中附带或不一致的候选不得签发。读取和成功重放沿原 Tool/Artifact 验证，无需重新取得或转换 Excel。Artifact 的 `output/document-conversions/…/source.md` 是元数据中的逻辑名称，不表示已写入工作区或文档库。新增选项与响应需要同步 Worker/契约版本；旧调用和旧成功记录不补造附件。

```text
原调用获准、保留原字节
  → 受控写入
  → 网关核参数、发号
  → 重验原执行权
  → 字节与成功回执一同保存
  → 提交确认后交付引用
  → 结果与检查点核验
```

[Artifact repository](../../SKM/backend/src/skillmind/artifacts/repository.py)复用 Evidence 的 Run/Tool/hash/时间，另存私有字节/发布元数据。同 Run 锁限制单份 1 MiB、100 份/10 MiB，跨 Segment/Attempt 累计，与模型预算分开。覆盖路径只发新引用，旧字节不变；无按路径更新/删除接口。

| 边界 | 处理 |
| --- | --- |
| 执行权失效、取消或候选校验失败 | 不发布附件；已经改变的工作区文件不因此回滚 |
| 保存失败或 commit 响应未知 | 不交付候选引用、不补写失败或重跑；原成功可在合法重放时读取原引用，未决保持未决 |
| 同 Project 的另一个 Run、缺失原 Tool/Attempt、非成功 Tool | 引用拒绝；原成功不能被改归当前 Attempt |
| 旧 Evidence/工作区文件 | 不补造附件身份或原字节，不重新采样签发历史回执 |

[读取接口](../../SKM/backend/src/skillmind/api/routes/artifacts.py)为 Run 下 GET /artifacts 和 /artifacts/{artifact_ref}/content，验当前 ProjectReadActor/精确 Run。列表至多 100 项元数据，detail 不加载私有字节；下载/引用核验从同次有界 SELECT 验原 size/hash/UTF-8，不读当前 workspace 或模型 URL。

越权/不存在沿 Project/Run 404，未知引用 artifact_not_found；损坏 409 artifact_content_invalid，存储故障静态 503 artifact_storage_unavailable。下载为 text/plain 附件，no-store/nosniff/安全文件名，不是 HTML 预览或二进制协议。

页面按授权索引区分结果采用/未采用；只有模型引用则只显示文字。客户端再验字节限额/原大小/hash/UTF-8；取消、30 秒期限、身份/Project/Run 切换使旧响应失效，及时回收对象 URL。下载不证明外部打开安全。

[0040](../../SKM/backend/migrations/versions/0040_evidence_artifacts.py)不回填；任一新绑定阻止丢列降级。原字节随 DB 备份，不以工作区恢复替代，见[迁移审查](../operations/deployment.md#迁移与回退审查)。

## 保存与显示不是同一个提交

候选只读校验 → 终态事务重验 lease/取消并保存允许的 Result、末次事件与 Outbox → 提交后 detail/SSE 各自读取 → 另一个 Evaluation 事务。

校验与终态不原子，锁后取消可覆盖候选；失败/取消可无 Result，不造空成功。Tool Evidence、评价不属终态事务，SSE 末次快照不含完整 Result，须授权读 detail。见[终态判定](run-supervision.md#提交时谁决定最终状态)。

## 评价请求与历史

[请求 Schema](../../SKM/contracts/evaluations/v1/create-request.schema.json)规定 rating 1–5、四种 verdict，可选 comment/revisions，至多 100 条修订。多用户/多次评价并存，最后一条不自动成为正式结论。以下路径位于 /api/v1/projects/{project_id}/runs/{run_id}：

| 入口 | 责任 |
| --- | --- |
| POST /evaluation-submissions | [原请求](../../SKM/contracts/evaluations/v1/submission-request.schema.json)的 submission_key、精确 result_id 和评价内容一起提交；首次 201，同一原请求重放 200 |
| GET /evaluation-submissions/{submission_key}?result_id=… | 当前同一用户只读确认原记录；不创建评价，未见原记录为 404 evaluation_submission_not_found |
| GET /evaluations/page?limit=20&after=… | 同一 Result 的完整游标分页入口；不在前端筛选旧接口第一页 |
| 旧 GET/POST /evaluations | 保留旧响应形状和追加语义；旧 POST 没有原请求保证，旧记录不补造 submission_key |

新提交必带非空 UUID submission_key/result_id；先复制请求，以共享 canonical JSON/hash 绑定键、用户、Project/Run/Result 及全部评价内容。省略 comment/revisions 规范为 ""/[]；对象键顺序不影响身份，修订顺序/JSON 值影响，不接受非 JSON 数值或非法 UTF-8。

每 Result/用户/key 唯一；异内容为 409 evaluation_submission_conflict，错 Result 为 409 evaluation_result_mismatch，无 Result 为 409 result_not_available，不凭 SUCCEEDED 授权。

[回执](../../SKM/contracts/evaluations/v1/submission.schema.json)仅含 project_id/run_id/key 和原 Evaluation 白名单。客户端验原用户/Result/key/全部内容，服务端另验保存内容/hash、修订原值/Result；相似历史不算成功。commit 确认前不交付，未知不补失败或重发。[0041](../../SKM/backend/migrations/versions/0041_evaluation_submissions.py)两列成组，旧行不补，任一新绑定阻止丢列降级。

### 评价的授权事务

POST 用 ProjectWriteActor/Origin/CSRF，user_id 取 actor。新旧写入、确认、新分页共用 UserAccess：Organization → User → 原 AuthSession → Project/成员 → 原 Run/Result；只读 actor 锁兼容外键，不复制凭据算法。

等锁、领域读取、flush 后以新时间验原会话/账户/成员；首次、重放、未命中、拒绝均经最终检查。写入限 ACTIVE，归档可授权读历史/回执；同一用户可用新有效会话人工查询，但不搬旧凭据。最终持锁检查不保证物理 commit 瞬间期限或全系统停权。

### 历史分页

[分页响应](../../SKM/contracts/evaluations/v1/page.schema.json)含 project_id/run_id/result_id/items/next_cursor。limit 1–100、默认 20；created_at/evaluation_id 升序，尾项 ID 续页，读取 limit+1 判断下一页，无则 null。

游标格式错、空 UUID、缺失或跨 Result 为 400 invalid_evaluation_cursor。空页不代表无 Result；稳定排序非跨页快照，并发追加可重读，不伪造总数或忽略未加载页。

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

pointer 以 / 指向既有目标，支持 ~0/~1；空值、缺字段、非法数组位置、穿标量、同次重复均拒绝。null 非缺失，不能新增/删除/追加目标。suggested_value 为任意 JSON，不再套业务 Schema 或合并 Result；采用须另定授权/版本/审计。

## 提交未知与界面责任

草稿、原请求、历史分开，可多修订、不自动采用。共用同步防重/30 秒期限，owner 含 actor/会话/Project/Run/Result。

| 情况 | 修正要求 |
| --- | --- |
| 成功 | 核对原回执与全部原内容，按 Evaluation ID 合并一次；晚到列表不覆盖刚确认的记录 |
| 拒绝 | 分开输入错误、结果不可用、身份/权限错误，不统一自动重试 |
| 未知 | 断网/超时/abort/无效成功响应可能已提交；冻结原键与内容，禁止自动 POST 或换键重试 |
| 只读确认 | 人工 GET 原回执；匹配才确认。404 只表示这次未见，不证明原 POST 未到达或回滚 |
| 显式重发 | 仍未确认时，用户可沿原键/原内容 POST，可能完成此前未落库的首次提交；不得混入新草稿 |
| 刷新/切换 | 同上下文切页签/重读详情保留未决；换 actor/会话/Project/Run/Result 后，旧成功、错误和 401 均不更新新页 |

历史刷新/相似评价不解除未决；已知输入拒绝才允许改草稿新提交，同键冲突不提供换键捷径。正文只存内存，不进 URL/持久层/日志，离页/刷新不承诺恢复。

有键可人工 GET，无正文不能据回执重造 POST。仅手输键的 GET 可结束/换查询键并保留未发草稿，旧响应随即失效；不撤销评价，也不解除已发 POST 的未决。Abort 不是撤销。

## 兼容与开发接续

新评价提交/确认/分页为独立契约，不改旧响应或将旧无键记录当可重放。新包络与旧 STRUCTURED_OUTPUT 分别可读，损坏不作空成功。唯一 Result 不等于禁止 UPDATE；追加式服务、真实迁移/竞争和运维保护仍须分层验收，状态见[计划 R07](../planning/roadmap.md#开发任务)。

## 验收条件

覆盖 PARTIAL/技术终态/人工判断分离、冻结 Schema、所有跨 Run/缺失引用、模型假称 APPLIED、两次评价原值不变、非法 pointer 整条回滚、合法 null/转义、提交丢响应与上下文切换。真实 commit/rollback 和浏览器时序分别验证；入口见[代码根 README](../../SKM/README.md)。
