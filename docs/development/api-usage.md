# API 利用ガイド

公開形状は [Contracts](../../PJM/README.md#contracts) と配備先の `/api/docs`、状態は[計画](../planning/roadmap.md)を参照する。保存 OpenAPI と工作副本の不一致は[契約確認](contract-workflow.md#遇到未接齐的交付链)で切り分ける。以下の path は context path 配下の `/api/v1` 起点。

## 認証してから呼び出す

全業務 API と SSE は認証必須。password、Cookie、CSRF を command 行、shell history、URL、共有 log に置かず、保護された client 入力を使う。session token は Web storage に保存しない。

| 順序 | API と必要な情報 |
| --- | --- |
| 1 | `GET /auth/login-context` で context cookie と login CSRF を取得 |
| 2 | `POST /auth/login` に上記 cookie、Origin、CSRF と [login request](../../PJM/contracts/auth/v1/login-request.schema.json) を送信 |
| 3 | 業務 API に session cookie、unsafe request にはさらに Origin と業務用 `X-CSRF-Token` を送信 |

login-context / login は両方とも来源配額を消費する。429 は Retry-After を確認して止め、503 を password 誤りと扱わない。再試行は利用者が新 challenge から明示的に行い、自動 password 再送・challenge polling・來源切替で制限を回避しない。送信後の通信失敗は結果不明で、abort は session 作成を取り消さない。

v2 の `GET /auth/session` は同一 session に同じ CSRF を返すが、idle を更新し得る。別 login、期限、role/失効が変われば旧値は使えない。403 や通信失敗を理由に業務 mutation を自動再送しない。[認証分診](../operations/runbook.md#认证故障的只读分诊)と[会話切替](../operations/deployment.md#会话协议切换检查)を参照する。

Project preference/UI language は[アカウント管理](../design/user-lifecycle.md)とは別資源。管理 API/Schema/client は存在するが、OpenAPI・ページ接線と全体検証の状態は計画で確認し、配備済みと仮定しない。無効・権限外の明示 Project を別 Project へ暗黙に切り替えてはならない。

## Task を選び、一回の Run を作る

1. `GET /projects/{id}/tasks` から精確 SkillVersion/task_key、入力 Schema と resource candidate を取得する。
2. `POST /projects/{id}/task-runs` に[作成契約](../../PJM/contracts/runs/task-create/v1/request.schema.json)の入力と、新規要求用の Idempotency-Key を送る。`input:{} / sources:{}` は常に合法な既定値ではない。
3. 返された Run ID で `GET /projects/{id}/runs/{run_id}/detail` と SSE を取得する。作成成功は実行成功ではない。

## 再送・継続・権限の規則

| 操作 | 保持する規則 |
| --- | --- |
| 作成の再送 | 同じ actor、原内容と key で確認する。同一要求は 200 / Idempotent-Replay、同 key の別意図は 409。新しい実行だけ別 key にする |
| 結果不明 | timeout は取消ではない。原要求を保全し、自動で新 key を発行しない。Web のページ内確認は refresh/離頁/actor・Project 切替を越えて保持されない |
| 授権 | 不存在と越権は同じ 404。Skill/Integration 管理は ADMIN、通常業務は Project の ACTIVE member（組織内 ADMIN は bypass） |
| 取消 | `POST /runs/{run_id}/cancel` の REQUESTED/CANCELLED と後続状態を確認する。SUCCEEDED/FAILED が先なら 409。HTTP 成功や CANCELLED は process 退出の証明ではない |
| SSE | `GET /runs/{run_id}/events` は Last-Event-ID / after から持続 event を再開する。一時 TEXT_DELTA は event ID を進めない |
| 外部批准 | Proposal の version/checksum/key が必要。Run 起動者か system ADMIN が判断し、Secret locator/config/credential は公開しない |

原要求確認は現在の認証・Project 授権を満たしたうえで行う。公開停止や資源変更後の確認と、新規作成時の資源検証は別であり、後者を緩めない。未知の旧形式は拒否する。詳細は[Run 作成](../design/run-creation.md)。

## 待機中の回答と結果評価

普通の CLARIFICATION / CHOICE / REVIEW は `POST /projects/{id}/runs/{run_id}/interactions/{interaction_id}/responses` に、表示中の interaction version と[回答契約](../../PJM/contracts/runs/interaction-response/v1/request.schema.json)、原回答の key を送る。

初回は 201、同じ内容/版/key の重放は 200。返却する現在の Run status は必ずしも QUEUED ではない。409 は競合、410 は期限切れだが、後者は過期 continuation の commit 後にも返る。通信失敗・410 を rollback と解釈せず、原 actor/内容を保って確認する。現在の回答 Web に完全な原要求確認はないため、detail と保存済み回答を先に読む。

Proposal decision と Evaluation は別 endpoint。Evaluation は追加式で原 Result を変更せず、pointer は `detail.result.data` の根を指す。回答と同じ幂等契約はなく、履歴の同文・時刻から原要求成功を断定して自動再送しない。詳しくは[普通答复](../design/user-interactions.md)と[结果与评价](../design/results-evaluation.md)。

## 文書を選び、元の範囲を確認する

候補から単一文書、2 件以上の集合、明示的な全集を選ぶ。任意 slot を使わない場合は省略し、候補先頭を同意の代用にしない。必須未選択、失効 ID、空/上限超過の全集は拒否される。token と上限の正本は[資源設計](../design/resource-snapshots.md#公开选择与读取投影的实施契约)。

detail の `document_snapshots` は作成時の清単で、FROZEN のみ検証済みメンバーを含む。LEGACY_UNAVAILABLE / INVALID は snapshot:null。history の `selected_sources` は摘要だけである。どれも現在の blob 可読性や実行成功を保証しない。

現行 Web は清単 field を必須として検証する。旧 Run の明示的な歴史状態と、旧 API が field を返せない契約エラーを混ぜず、空集合への fallback をしない。片側更新・回退は[版互換](contract-workflow.md#历史数据兼容不等于前后端版本兼容)を確認する。
