# API 利用ガイド

形状は [Contracts](../../SKM/README.md#contracts) / 配備先の `/api/docs`、利用可能範囲は[計画](../planning/roadmap.md)を参照する。
以下は context path 配下の `/api/v1` 起点。保存 OpenAPI と実装の一致は[契約確認](contract-workflow.md#遇到未接齐的交付链)で調べる。

## 認証してから呼び出す

password/Cookie/CSRF を command 行、history、URL、共有 log に置かず、session token を Web storage に保存しない。

| 順序 | API と必要な情報 |
| --- | --- |
| 1 | GET /auth/login-context：context cookie と login CSRF を取得 |
| 2 | POST /auth/login：cookie、Origin、CSRF と [login request](../../SKM/contracts/auth/v1/login-request.schema.json) |
| 3 | 業務 API/SSE：session cookie。unsafe request は Origin と業務用 X-CSRF-Token も送る |

login-context/login は両方とも来源配額を消費する。429 は Retry-After に従って停止し、503 を password 誤りと扱わない。
再試行は新 challenge から明示的に行う。自動 password 再送・polling・来源切替で制限を回避せず、通信失敗/abort を session 作成取消と扱わない。

v2 の GET /auth/session は同一 session に同じ CSRF を返すが idle を更新し得る。別 login・期限・role/失効後は旧値を使わず、403 や通信失敗で業務 mutation を自動再送しない。[認証分診](../operations/runbook.md#认证故障的只读分诊)を参照する。

## Task を選び、一回の Run を作る

1. GET /projects/{id}/tasks で精確 SkillVersion/task_key、入力 Schema と resource candidate を取得。
2. POST /projects/{id}/task-runs に[入力契約](../../SKM/contracts/runs/task-create/v1/request.schema.json)と新規用 Idempotency-Key を送る。input/sources の空 object は常に合法ではない。
3. 返された ID で GET /projects/{id}/runs/{run_id}/detail と SSE を読む。作成成功と実行成功は別。

## 再送・継続・権限の規則

| 操作 | 規則 |
| --- | --- |
| 作成の確認 | 同 actor・原内容/key を維持。同一要求は 200 / Idempotent-Replay、同 key の別意図は 409。新実行のみ別 key |
| 結果不明 | timeout/abort は取消でない。自動で新 key を作らない。ページ内確認は離頁/refresh/actor・Project 切替を越えて残らない |
| 授権 | 越権/不存在は同じ 404。Skill/Integration 管理は ADMIN、通常業務は ACTIVE Project member（組織内 ADMIN は bypass） |
| 取消 | POST /runs/{run_id}/cancel 後も状態を確認。先に SUCCEEDED/FAILED なら 409。CANCELLED は process 退出の証明でない |
| SSE | GET /runs/{run_id}/events は Last-Event-ID / after で持続 event を再開。一時 TEXT_DELTA は event ID を進めない |
| 外部批准 | Proposal の version/checksum/key を固定し、Run 起動者か ADMIN が判断。Secret locator/config/credential は公開しない |

原要求確認にも現在の認証/Project 授権が必要。公開停止後の確認と新規資源検証は別で、未知の旧形式は拒否する。
無効・権限外の明示 Project を別 Project に自動変更しない。詳細は[Run 作成](../design/run-creation.md)。

## 待機中の回答と結果評価

普通答復と Proposal decision、Evaluation は別 endpoint。

- CLARIFICATION/CHOICE/REVIEW：POST /projects/{id}/runs/{run_id}/interactions/{interaction_id}/responses に原 interaction version・[回答](../../SKM/contracts/runs/interaction-response/v1/request.schema.json)・Idempotency-Key を送る。初回 201、同内容/版/key の確認は 200。
- 答復の 409 は競合、410 は期限切れ。410 は過期 continuation の commit 後にも返るため rollback と見なさず、現在の Interaction/Run を再読する。通信失敗時の Web 確認は原 POST で、初回保存になり得る。
- 評価：POST /projects/{id}/runs/{run_id}/evaluation-submissions に submission_key・result_id と評価を送る（初回 201、原要求再送 200）。同 path の /{submission_key}?result_id=… を GET して読取専用確認する。404 は先行要求の未実行証明ではない。
- 旧 /evaluations POST に原要求確認はない。履歴の同文/時刻から成功を推定しない。評価は追加式で原 Result を変えず、修正 pointer は detail.result.data の根を指す。

返却された現在の Run status を QUEUED 固定と仮定しない。詳細は[普通答复](../design/user-interactions.md)と[結果・評価](../design/results-evaluation.md)。

## 文書を選び、元の範囲を確認する

単一、2 件以上の集合、明示的な全集から選ぶ。任意 slot 未使用は省略し、候補先頭を同意にしない。必須未選択、失効 ID、空/上限超過の全集は拒否される。

detail.document_snapshots は作成時の清単で、FROZEN のみメンバーを持つ。LEGACY_UNAVAILABLE/INVALID は snapshot:null、history.selected_sources は摘要のみ。いずれも現在の blob 可読性を保証しない。

token/上限は[資源設計](../design/resource-snapshots.md#公开选择与读取投影的实施契约)が正本。旧 Run の明示的な歴史状態と旧 API の必需 field 欠落を混同せず、空集合に fallback しない。更新/回退時は[版互換](contract-workflow.md#历史数据兼容不等于前后端版本兼容)を確認する。
