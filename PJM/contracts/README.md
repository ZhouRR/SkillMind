# 実行可能なインターフェース契約

本 directory は ProjectMind の公開契約を JSON Schema と代表 example で管理する。仕様の本文は
[文書ガイド](../../docs/README.md)から辿る `../../docs/design/` に置き、ここには機械検証可能な Schema、example、OpenAPI snapshot だけを置く。

[配置](#配置) · [認証](#認証と-secret-の契約を読む) · [ユーザー管理](#ユーザー管理の公開面を準備する) · [Run 文書](#run-文書契約を読む) · [取消](#run-の取消と終態を読む) · [Skill](#skill-の公開と-project-有効化を読む)

[子分析と用量](#子分析と用量の契約を読む) · [外部変更](#外部変更の契約を読む) · [調度](#schedule-の公開契約を読む) · [生成 module](#生成-module-と既存-module-api-を分ける) · [同期と検証](#同期義務)

## 配置

- 資源別 directory(`runs/`、`skills/`、`users/`、`projects/`、`events/`、`errors/` など):公開 API・event・error の versioned Schema。
- `tools/<capability>/`:`issue.read/v1` のような versioned Tool capability の request/response/error Schema。
- `agent-task-brief/`、`capability-blueprint/`、`runtime-manifest/`、`outcomes/`、`view-spec/`:Interpreter と Runtime が共有する meta 契約。業務固有 field を追加しない。
- `examples/`:各 Schema の代表 example。
- `fixtures/`:offline 回帰用の固定入力。
- `openapi/projectmind-api.v1.json`:`python3 scripts/export_openapi.py` が現在の FastAPI application から再生成する snapshot。手編集しない。

## データ形状と設計の読み分け

Schema は field/type/required を、[設計](../../docs/README.md)は権限・凍結時点・幂等・失敗時の意味を担当する。両方を満たす必要があり、Schema validation だけで所有権や runtime の保証は証明できない。route と snapshot が一致しない場合は[未接続の交付連鎖](../../docs/development/contract-workflow.md#遇到未接齐的交付链)として扱い、ファイルが新しい方を無条件に正本としない。

業務の正解を判定する [JAF Benchmark](../../docs/acceptance/jaf-benchmark.md) は、この directory の Schema/example とは別の評価資産である。case、Rubric や Gold を公開 API の fixture へ移したり、Schema 通過を業務正解率へ読み替えたりしない。

内部 JSON 列や作業中 DTO の field をそのまま公開契約と見なさない。文書快照は[資源設計](../../docs/design/resource-snapshots.md)に従って公開投影を明示し、[共有予算](../../docs/design/run-budgets.md#持久账本的当前载体)の既存内部 DTO/台帳は、まだ公開面へ接続されていないものとして読む。公開面を変える際は response allowlist、Web validator、version/checksum と歴史互換を確認し、説明用の仮 field を先に既成事実化しない。

[Run 作成](../../docs/design/run-creation.md)の versioned identity は、工作副本の内部 `task_snapshot_json.creation_request` に保存する。これは公開 request に fingerprint を追加する変更ではなく、既存の作成 request/response を使用する。[調度](../../docs/design/task-scheduling.md)の持続在途 occurrence はまだ目標設計であり、同名の公開 Schema や table が存在すると仮定しない。

[入力回执](../../docs/design/resource-snapshots.md#输入准备与可信缓存)には RunInputSnapshot model/migration、Worker/Tool と準備監督の接続があるが、公開 API の追加ではない。内部の snapshot_id、PREPARING/READY や物理世代 path を Run detail の document_snapshots へ混ぜない。作成時の文書選択が FROZEN であることと、実ファイルの準備完了は別の契約である。準備 timeout や開始前 gate も内部実行境界であり、説明のために RunEvent enum や応答へ未定義 field を追加しない。

既存の 201/200/409 の形が正しくても、資源変化や transaction 間の crash に対する保証は別途有状態テストで確認する。データの形、意味、実装状態の三つを混同しない。

[TaskFlowProjection](../../docs/design/task-flow.md#3-taskflowprojection-目标契约) は目標構造であり、この directory に同名 Schema はまだ無い。後続の導入では計画の意味を識別する checksum、純粋な表示 layout、実 event の関連を分け、旧版の未宣言と新版の破損を同じ空値へ畳まない。現行 Blueprint/Brief の追加禁止を仮 field で迂回しない。

公開 field の追加・削除・required 化は[契約変更と結合確認](../../docs/development/contract-workflow.md)に従い、既存 consumer と版互換を確認する。Schema、example、OpenAPI、Web が一致しても、混合版の配備や実 transaction まで検証済みとは扱わない。検証の範囲と残項目は[計画](../../docs/planning/roadmap.md#13-当前执行状态)に記録する。

## 生成 module と既存 module API を分ける

| 名前 | 現在の契約と制限 |
| --- | --- |
| Project の module | [OpenAPI](openapi/projectmind-api.v1.json) の ModuleWriteRequest / ModuleResponse → [compositions route](../backend/src/projectmind/api/routes/compositions.py) / [Web modules](../web/src/api/modules.ts)。SkillVersion の組合であり、生成コードを受け取らない |
| 生成表示の参照 | [RuntimeManifest](runtime-manifest/v1alpha1.schema.json) の ui.frontend_module は宣言時 null のみ。[正規化](../backend/src/projectmind/skills/runtime_defaults.py)も None を設定する。仮の bundle URL を入力しても公開 protocol にならない |
| Host と構築結果 | 内部の module_api_version、hash、CSP、report 列はあるが、対応する公開 Host/build Schema、dispatcher、SDK は未実装 |

意味と実装差距は[生成設計の公開境界](../../docs/design/generated-modules.md#现有代码与公开契约)を正本とする。後続の[内容 identity と歴史互換](../../docs/design/generated-modules.md#内容身份与历史兼容)は model/migration と同時に審査し、版付き参照、Manifest 正規化、公開 gate、Host validator、旧 Skill/Run の standard 表示を同期する。データ形状のテストは CSP/通信隔離の証拠ではなく、[投放と回退](../../docs/design/generated-modules.md#展示选择与全局停用)の実ブラウザ検証を別途要する。

## 認証と Secret の契約を読む

| 境界 | 形状と実装の同期先 |
| --- | --- |
| ログイン前 | [login context](auth/v1/login-context.schema.json) / [login request](auth/v1/login-request.schema.json) → [auth route](../backend/src/projectmind/api/routes/auth.py) / [Web client](../web/src/api/auth.ts)。短期 challenge と session CSRF は別物 |
| ログイン防護の拒否 | [Problem](errors/problem/v1.schema.json) / [公開 factory](../backend/src/projectmind/api/login_protection.py) → auth route / [HTTP client](../web/src/api/http.ts)。429/503、Retry-After と no-store は HTTP 契約であり、body Schema の通過だけでは確認できない |
| 認証後の Session | [session](auth/v1/session.schema.json) / [example](examples/auth-session.v1.json) → AuthService / Web の App。公開 csrf_token と HttpOnly session cookie を取り違えない |
| Secret の作成・一覧・無効化 | [OpenAPI](openapi/projectmind-api.v1.json) の CreateSecretReferenceRequest / SecretReferenceResponse → [integration route](../backend/src/projectmind/api/routes/integrations.py)。MANAGED の secret_value は入力専用で、response に locator・暗号文・元の値を追加しない |
| 拒否の表現 | [Problem](errors/problem/v1.schema.json) → [actor dependency](../backend/src/projectmind/api/auth_dependencies.py) / [HTTP client](../web/src/api/http.ts)。401、CSRF/role の 403、所有権の 404 を同じ retry 指示に潰さない |

内部の[会話 protocol v2](../../docs/design/authentication.md#会话凭据-v2-与切换要求)でも公開 SessionResponse は既存 v1 の field を使い、csrf_token は opaque string のままである。credential_version と system_role_at_login は DB 内部列で、response に追加しない。形状が不変でも旧会話の失効・再ログインが必要であり、新旧 API の混在が互換になるわけではない。

[ログイン防護の応答](../../docs/design/login-protection.md#公开响应与客户端责任)は新しい request/Session field を要求しない。429/503 は application/problem+json の既存 Schema、no-store、request ID、429 の Retry-After を OpenAPI に同期済み。login/session/logout の 401/403/422 も実際の Problem handler に合わせて宣言し、Framework 既定の validation JSON と混同しない。現在の実施範囲は[計画の現在証拠](../../docs/planning/roadmap.md#当前证据怎么用)で確認する。

body の [429 example](examples/login-rate-limited.v1.json) / [503 example](examples/login-protection-unavailable.v1.json) は両方の example 表に登録済み。[共有 Problem 定義](../backend/src/projectmind/api/problems.py)は配備先の file path に依存せず、正本との完全一致を test で守る。変更時は [HTTP と JSON の分担](../../docs/development/contract-workflow.md#开工时列出消费者和同步先后)に従い、実 response の media type/body/header、exporter と Web 消費を別々に確認する。snapshot 一致だけで全 API の宣言が完全になったとは扱わない。

token の寿命、安定 CSRF と role/失効は[セッション設計](../../docs/design/authentication.md#会话读取与多页面)、key_version と kek_version の違いは[Secret の実保存形式](../../docs/design/secret-storage.md#managed-的实际加密结构)が決める。SessionResponse に管理 field や DEK を混ぜない。ユーザー管理は下記の独立 Schema を読み、旧セッション、Web の確認/retry、HTTP cache と[契約変更手順](../../docs/development/contract-workflow.md)を同期する。

## ユーザー管理の公開面を準備する

[管理 API の操作境界](../../docs/design/user-lifecycle.md#用户操作与目标公开面)に対応する route、以下の Schema/example は工作副本に存在する。保存 OpenAPI には管理操作が未収録で、専用 Web client/page も未接続。これは交付途中の契約であり、配備先の利用可能性を保証しない。見出しは旧リンクの互換のため保持する。

| データの用途 | Schema と代表 example |
| --- | --- |
| 本人・管理対象のアカウント | [account](users/v1/account.schema.json) / [example](examples/user-account.v1.json) |
| ユーザーの検索 page | [list](users/v1/list.schema.json) / [example](examples/user-list.v1.json)。items と全体件数/limit/offset を一緒に扱う |
| 一件の安全操作 | [security-event](users/v1/security-event.schema.json) / [example](examples/user-security-event.v1.json) |
| 一人の安全履歴 page | [security-events](users/v1/security-events.schema.json) / [example](examples/user-security-events.v1.json) |
| 変更の成功応答 | [mutation](users/v1/mutation.schema.json) / [example](examples/user-mutation.v1.json)。user、失効行数、現在会話の失効は別の事実 |
| ADMIN の作成要求 | [create-request](users/v1/create-request.schema.json) / [example](examples/user-create-request.v1.json) |
| ADMIN の資料・role・status 更新 | [update-request](users/v1/update-request.schema.json) / [example](examples/user-update-request.v1.json) |
| 本人の改密要求 | [password-request](users/v1/password-request.schema.json) / [example](examples/user-password-request.v1.json) |
| 本人/ADMIN の会話失効要求 | [version-request](users/v1/version-request.schema.json) / [example](examples/user-version-request.v1.json) |

example は架空の値であり、配備先に存在する account や初期 password ではない。writeOnly の指定も入力を log から自動除去する機構ではない。既存 /users/me の Project preference と UI language は別資源のまま保持する。

[users route](../backend/src/projectmind/api/routes/users.py)の response allowlist と Schema/example（両検証表）を照合し、OpenAPI exporter / [一致性 test](../backend/tests/contracts/test_contracts.py)と [Web API](../web/src/api/)の資源別 validator・index.ts を接続する。失敗中の test を残したまま snapshot だけ生成して交付済みとは扱わない。[Backend の接続入口](../backend/README.md#ユーザー管理の接続を引き継ぐ)と[契約変更](../../docs/development/contract-workflow.md)から継続する。

特に[版と公開データ](../../docs/design/user-lifecycle.md#版本查询与公开数据)、[結果不明と拒否](../../docs/design/user-lifecycle.md#生效与界面)、[監査相関](../../docs/design/user-lifecycle.md#审计与请求关联)を同時に確認する。会話無効の 401 と現 password の誤り、版競合と最後の活動 ADMIN、失効行数と online 人数を分ける。HTTP cache/header と middleware の request ID は JSON Schema の通過だけでは検証できない。

## Run の取消と終態を読む

| 公開する事実 | 契約と確認先 |
| --- | --- |
| 取消要求の処理結果 | [cancel response](runs/cancel/v1/response.schema.json) / [example](examples/cancel-run-response.v1.json) → [runs route](../backend/src/projectmind/api/routes/runs.py)。REQUESTED と CANCELLED を区別する |
| Run の状態と event 順序 | [RunEvent](events/run-event/v1.schema.json) / [取消意図の example](examples/cancel-requested-run-event.v1.json) → [runReplay](../web/src/lib/runReplay.ts)。Run status は RUN_SNAPSHOT で読み、interrupted の名前から推定しない |
| 取消できない終態 | [Problem](errors/problem/v1.schema.json) / [OpenAPI](openapi/projectmind-api.v1.json) → [API test](../backend/tests/api/test_run_api.py)。先に確定した SUCCEEDED / FAILED は 409 run_not_cancellable |

[提交の判断点](../../docs/design/run-supervision.md#提交时谁决定最终状态)が lock・lease・取消意図の意味を決める。Schema が一致することは、待機への遷移も同じ transaction を通るという証拠ではない。Backend 内部の候補 status、service の commit、Web の受信を別の段階として検証する。

cancel の成功や terminal snapshot は process 退出回执ではない。停止理由・用量の未確認情報を公開する場合は [版互換](../../docs/design/run-supervision.md#兼容与开发接续)に従い adapter → Executor → repository → Schema/example → Web validator を同期する。payload が object として通るだけでは原因の意味は検証されず、歴史 event に推測で原因を補わない。

## Skill の公開と Project 有効化を読む

| データの責任 | 契約と同期先 |
| --- | --- |
| 解釈の意味と frozen 本文 | [Blueprint](capability-blueprint/v1.schema.json) / [Manifest](runtime-manifest/v1alpha1.schema.json) → Backend validator / projector。compatibility の native は Interpreter を省略する許可ではない |
| 公開版と gate の状態 | [SkillVersion](skills/version/v1.schema.json) → [skills route](../backend/src/projectmind/api/routes/skills.py) / [Web validator](../web/src/api/skills.ts)。gate_passed と PUBLISHED は別の事実 |
| Project の可視性 | [enablement](skills/project-enablement/v1.schema.json) → repository / 同 Web validator。現行 response に有効化/無効化の全履歴や再有効化 token があると仮定しない |

状態の意味と[再有効化の目標](../../docs/design/skill-contract.md#112-可审计的重新启用与回滚)は Skill 設計に集約する。追加時は DB の履歴と現在投影、公開 DTO/Schema、Web の確認/競合表示を同時に設計し、既存 disabled_at を消すだけで監査要件を満たしたと扱わない。

## Run 文書契約を読む

| 確認する場面 | Schema / example | 実装で合わせる入口 |
| --- | --- | --- |
| どの文書を選んで新規作成するか | [task-create request](runs/task-create/v1/request.schema.json)、[単一](examples/create-task-run-documents-single.v1.json) / [集合](examples/create-task-run-documents-set.v1.json) / [全集](examples/create-task-run-documents-all.v1.json) | [選択 parser](../backend/src/projectmind/documents/snapshot.py)、[Web 選択](../web/src/lib/documentSelection.ts) |
| 作成済み Run の元の文書一覧を読む | [detail](runs/detail/v1.schema.json)、[文書なしの detail](examples/run-detail.v1.json)、[三状態を含む detail](examples/run-detail-documents.v1.json) | [公開投影](../backend/src/projectmind/runs/resource_projection.py)、[Web validator](../web/src/api/runResources.ts) |
| 履歴一覧で資源摘要を読む | [history](runs/history/v1.schema.json)、[history example](examples/run-history.v1.json) | detail と同じ摘要 allowlist。内部 binding や全メンバーを一覧へ複製しない |

選択 request と凍結 response は逆方向の契約である。detail の checksum を request に付けて授権を省略しない。三状態の意味は[資源設計](../../docs/design/resource-snapshots.md#读取清单和资源摘要)を正本とし、この表へ enum の説明を複製しない。

業務テストは [test_run_documents.py](../backend/tests/contracts/test_run_documents.py) と[実 API 応答](../backend/tests/api/test_run_api.py)、消費側は [runResources.test.ts](../web/tests/api/runResources.test.ts)へ進む。example は固定データであり、その UUID が配備先に存在する保証ではない。

## 子分析と用量の契約を読む

| 境界 | 現行の契約と同期先 |
| --- | --- |
| 子分析の要求/結果 | [request](tools/subagent.dispatch/v1/request.schema.json) / [response](tools/subagent.dispatch/v1/response.schema.json) → [Provider](../backend/src/projectmind/agent/subagent_provider.py) → Gateway。budget の説明は本 dispatch の分配値に修正済みで、実消費や残額ではない |
| Session identity | 同 response は各 result の agent_session_id を必須とする。Provider も保存確認ができなければ unavailable とし、省略した成功応答を返さない。SDK の Session UUID と平台の行 ID を区別する |
| 子実行の出力 | [収集器](../backend/src/projectmind/agent/subagent_result.py) → [共有 validator](../backend/src/projectmind/agent/result_validation.py)。現在は父 Schema/Brief を継承し、独立した子出力契約は未定義。[子指令/出力の設計](../../docs/design/subagents.md#子任务指令与结果的边界)から prompt・SDK・validator の同期先を確認する |
| 用量の観測 | [RunEvent](events/run-event/v1.schema.json)、[Run detail](runs/detail/v1.schema.json) → mapper / repository / Web。[用量の記録経路](../../docs/design/run-budgets.md#用量现在流向哪里)が持つ usage、cost、分配 metadata を共通台帳と扱わない |

共有予算には account/reservation/receipt の内部 model、DTO と store があるが、現行の公開 Schema はまだ無い。内部保存版 `run-budget/v1` や START_INTENT を、そのまま Tool の版や RunEvent enum に追加しない。[台帳と実行の接続](../backend/README.md#台帳の実装を引き継ぐ)を先に確認し、上限、占用、確定用量、未知量を公開するときは source/version と allowlist を決め、Brief/Event/Run detail、example、実行時 validator と三語表示を同期する。既存の budget を残額として再解釈せず、Schema の説明文だけを変更して完了とはしない。

正常 example は ID を含むため、[example 検査](../backend/tests/contracts/test_contracts.py)だけでは異常経路を証明できない。[実 dispatch/Gateway 回帰](../backend/tests/agent/test_subagent_lifecycle.py)と[Gateway test](../backend/tests/agent/test_tool_gateway.py)を接続し、失敗 event、ID 不整合、応答過大、Session/Tool 監査の別々の保存失敗を確認する。現行の検証範囲と残項目は[計画](../../docs/planning/roadmap.md#13-当前执行状态)へ集約し、過去の失敗記録と区別する。

Tool の success、各 branch の COMPLETED、主 Run の成功は[別の判断](../../docs/design/subagents.md#一个例子完成的是哪一层)である。監査欠落を表す将来版は[提交と版互換](../../docs/design/subagents.md#提交顺序与版本兼容)に従い、旧 required を削るだけで互換とはしない。今回は既存 Schema を確認した文書変更であり、公開契約を変更しない。

## 外部変更の契約を読む

| 境界 | 現行の契約と消費側 |
| --- | --- |
| Agent の提案 | [change.propose request](tools/change.propose/v1/request.schema.json) → [Proposal validator](../backend/src/projectmind/effects/proposal.py)。通用 action があっても capability 別 validator の許可を超えない |
| 人工の approve/reject | [OpenAPI](openapi/projectmind-api.v1.json) の DecideProposalRequest / ProposalDecisionResponse → [effects route](../backend/src/projectmind/api/routes/effects.py) → [Web client](../web/src/api/effects.ts)。原決定の key・本文と CSRF を渡す |
| 読取結果 | [Run detail](runs/detail/v1.schema.json) の change_proposals / approvals / effect_executions → [RunResultPanel](../web/src/components/RunResultPanel.tsx) |
| Redmine adapter | [discovery](providers/redmine-effect/v1/discovery.schema.json)、[apply request](providers/redmine-effect/v1/apply-request.schema.json) / [response](providers/redmine-effect/v1/apply-response.schema.json) → Provider/transport。通常 REST update へ fallback しない |

[repository.write request](tools/repository.write/v1/request.schema.json) は branch 限定の旧定義が残り、現行 Effect Worker の入力ではない。これを direct/SVN の完全な契約や Agent が直接呼べる Tool として案内しない。[契約と実行 identity](../../docs/design/repository-effects.md#契约与幂等身份)に差距を集約し、版互換と消費側を審査して同期する。未定義の段階回执や結果不明 enum を説明用に現行応答へ足さない。

## Schedule の公開契約を読む

現行の形状は [OpenAPI](openapi/projectmind-api.v1.json) の Schedule 系 component と [route DTO](../backend/src/projectmind/api/routes/schedules.py)で確認する。専用 occurrence Schema/table は未実装であり、設計上の記録を既存 response へ仮に追加しない。

| 境界 | 現行の形状と消費側 |
| --- | --- |
| 保存・更新・状態操作 | CreateScheduleRequest / UpdateScheduleRequest / ScheduleStatusRequest → [Web client](../web/src/api/schedules.ts)。expected_row_version は更新だけにあり、状態操作には無い。field の存在だけで原子的 CAS は証明できない |
| 時間プレビュー | SchedulePreviewRequest / SchedulePreviewResponse → [ScheduleDialog](../web/src/components/ScheduleDialog.tsx)。definition の候補であり、input/sources の検証結果や将来 Run の保証ではない |
| 一覧と取得 | ScheduleListResponse は schedules / total / limit / offset、ScheduleResponse は last_* 等の摘要 → [TasksPage](../web/src/pages/TasksPage.tsx)。現行 client は先頭 100 件の配列だけを返す |

後続変更は[並行更新・履歴互換](../../docs/design/task-scheduling.md#配置并发与暂停)と[管理の可視性](../../docs/design/task-scheduling.md#保存后的管理入口)を先に確認する。API で paging が可能なことと Web が全件を発見できること、409 の投影と実 transaction の排他は別の証拠として扱う。

## 同期義務

- Schema・example の追加/変更時は `scripts/validate_contracts.py` と
  `backend/tests/contracts/test_contracts.py` の**両方**の example 表へ登録する。片方だけでは
  example は誰にも検証されない。
- 公開 endpoint の追加/変更後は `scripts/export_openapi.py` で OpenAPI snapshot を回写する。
  一致性は `backend/tests/contracts/` の dict 相等 assertion が守る。
- Backend DTO、Web validator、テストとの同期点の全体は [AGENTS.md](../AGENTS.md) の
  「よくある変更の同期点」を参照する。

## 検証

以下は `contracts/` 内ではなく `PJM/` で実行する。公開 endpoint を変更した場合のみ OpenAPI を再生成する。Schema の存在は Provider/renderer 実装済みの証明にはならない。

```bash
python3 scripts/validate_contracts.py
```
