# API 利用ガイド

> コマンドは `PJM/` 起点。例の ID・host・token は placeholder。初回の疎通確認は [運用 smoke](../operations/runbook.md#41-通常-smoke) が対話ログインを扱うが、専用 Project に Run/Evaluation を作る検証であり読取専用ではない。

[認証](#認証してから呼び出す) · [Run 作成](#task-を選び一回の-run-を作る) · [再送と権限](#再送継続権限の規則) · [凍結文書](#文書を選び元の範囲を確認する)

このガイドは利用手順、[資源別の契約索引](../../PJM/contracts/README.md)は Schema/example、[現在の計画](../planning/roadmap.md#当前证据怎么用)は未完了の接線を担当する。保存 OpenAPI と工作副本が一致しない場合は[只読の確認手順](contract-workflow.md#遇到未接齐的交付链)へ進み、snapshot の存在だけで配備先の能力を判断しない。

## 認証してから呼び出す

全業務 API（Project、Skills、Run、Result、Evidence、Evaluation、SSE）は認証必須である。まず login して session cookie と CSRF token を取得する。

| 順序 | API | 保存・送信するもの |
| --- | --- | --- |
| 1 | `GET /api/v1/auth/login-context` | login context cookie と response の login CSRF |
| 2 | `POST /api/v1/auth/login` | 上記 cookie、Origin、login CSRF、[login request](../../PJM/contracts/auth/v1/login-request.schema.json) |
| 3 | 業務 API | login response の session cookie と業務用 csrf_token |

実 password を `curl --data '{...}'` の command 行や shell history に置かない。HTTP client の保護された入力を使い、request/response の Secret を log しない。以降の例は認証済み cookie jar の path を `COOKIE_JAR`、業務 CSRF を `SESSION_CSRF` として渡した後の例である。cookie jar は owner のみ読み書き可能にし、共有せず、利用後は安全に破棄する。

以降の unsafe request（POST/PATCH/PUT/DELETE）は session cookie に加えて `Origin` と `X-CSRF-Token`（login/session response の値）を送る。Session token を Web storage に保存してはならない。

login-context と login はどちらも[来源配額](../design/login-protection.md#一个例子一次登录两次入口请求)を消費する。429 login_rate_limited は Retry-After の秒数を確認して操作を止め、503 login_protection_unavailable は password 誤りと解釈しない。再試行は利用者が明示し、新しい challenge から開始する。challenge の polling、password 自動再送、限流を避けるための account/來源切替は行わない。公開応答と現在の Web の差は[client 責任](../design/login-protection.md#公开响应与客户端责任)を参照する。

password POST の送信後に通信が切れた場合は、上記の明確な拒否とは区別する。client の abort は Server の Session 作成を取り消さず、Login に原要求を返す Idempotency-Key 契約もない。[結果不明の扱い](../design/login-protection.md#提交离页与结果未知)に従い、原結果の確認を password 再送や自動 logout で代用しない。

現在の v2 では `GET /api/v1/auth/session` が同じ会話に同じ CSRF を返す。client は値をそのまま扱い、prefix の解析や派生を行わない。認証 route の token 応答には no-store を設定するが、会話の認証は idle 記録を更新し得るため、完全な読取専用ヘルスチェックではない。

別ログインで cookie が変わった場合や、期限/role/失効の不一致では古い値を使い続けられない。[多ページの設計](../design/authentication.md#会话读取与多页面)と[認証分診](../operations/runbook.md#认证故障的只读分诊)を参照し、403 や通信失敗から業務 mutation を自動再送しない。0031 の旧会話切替は[運用の前提条件](../operations/deployment.md#会话协议切换检查)を確認する。公開 JSON の版が変わらないことは、旧 API と混在可能という意味ではない。

Project 選択は `GET/PUT /api/v1/users/me/project-preference` で User account に保存する。Web の hash URL は `?project=<uuid>` を含められ、URL で指定した Project が無効または権限外の場合は別 Project へ暗黙に fallback しない。

`/users/me` の preference / UI language と、[アカウント管理 API](../design/user-lifecycle.md#用户操作与目标公开面)は別資源である。管理 route、Schema/example と専用 Web client は工作副本に存在するが、保存 OpenAPI、アカウント page/route と全体回帰は未完了。[管理契約の対応表](../../PJM/contracts/README.md#ユーザー管理の公開面を準備する)から接続を確認し、本ガイドでは改密・停用・一括失効を配備済みの操作例にしない。

## Task を選び、一回の Run を作る

TaskCatalog から対象の精確 SkillVersion/task_key と合法な resource candidate を取得する。`input:{}` / `sources:{}` は常に使える既定値ではない。Schema や必須資源がある task では、実際の入力と選択を[作成契約](../../PJM/contracts/runs/task-create/v1/request.schema.json)に従って補う。

```bash
BASE="https://${PROJECTMIND_HOST}${PROJECTMIND_CONTEXT_PATH}"
curl -b "$COOKIE_JAR" -s "$BASE/api/v1/projects/${PROJECT_ID}/tasks"

curl -b "$COOKIE_JAR" -i -X POST "$BASE/api/v1/projects/${PROJECT_ID}/task-runs" \
  -H "Content-Type: application/json" -H "Origin: https://${PROJECTMIND_HOST}" \
  -H "X-CSRF-Token: ${SESSION_CSRF}" -H "Idempotency-Key: example-request-001" \
  --data '{"skill_version_id":"<published-skill-version-uuid>","task_key":"<task-key>","input":{},"sources":{}}'
```

成功時に返った Run ID を使って detail/SSE を取得する。作成成功は実行成功ではない。待機中は既存 Interaction/Proposal の版に対して回答し、終態後の別目標は新 Run とする。

## 待機中の回答と結果評価

普通の CLARIFICATION / CHOICE / REVIEW は、Run detail の interaction ID / version と[回答契約](../../PJM/contracts/README.md#通常回答と評価の契約を読む)を使用する。endpoint は `/api/v1/projects/{id}/runs/{run_id}/interactions/{interaction_id}/responses`。Session / Origin / CSRF に加え、原回答の Idempotency-Key を送る。Proposal decision と Evaluation は別 endpoint である。

初回回答は 201、原内容/版/key の重放は 200。重放には原 response/segment ID と現在の Run status が含まれるため、必ず QUEUED に戻ると考えない。409 は状態/版/回答の競合、410 は期限切れであり、後者は過期 continuation の commit 後に返る場合がある。[回答が届かなかった例](../design/user-interactions.md#一个例子回答超时不等于什么都没发生)に従い、通信失敗を rollback と扱わず、元の actor と内容を保って確認する。

現在の Web 回答カードには完全な原要求確認が無い。結果不明時はまず許可された Run detail と保存済み回答を調べ、別 key/新 version で自動再送しない。Evaluation POST にも回答と同じ幂等契約は無い。[評価の原値と提出](../design/results-evaluation.md#评价请求与历史)を確認し、pointer は detail.result.data の根から指定する。履歴の内容一致は原要求の成功証明にならず、自動で追加 POST しない。本文・回答・credential を URL、共有 log やコマンド履歴へ残さない。

## 再送・継続・権限の規則

公開 API の全体は [OpenAPI snapshot](../../PJM/contracts/openapi/projectmind-api.v1.json)、配備先の `${BASE}/api/docs` を参照する。以下の短縮 path はすべて `/api/v1` 起点である：

- Run 作成は TaskCatalog の精確 `skill_version_id + task_key` を使用し、`project_id + task_id +
  Idempotency-Key` で idempotent。同一 request の再送は `200` + `Idempotent-Replay: true`、同 key の
  異なる入力は `409 idempotency_conflict`。
- `GET /projects/{id}/runs/{run_id}/detail` は検証済み Result、脱敏済み ToolCall summary、Evidence を返す。資源の不存在と越権は区別せず `404 run_not_found`。
- ADMIN は `/projects/{id}/secret-references`、`/integrations`、`/resource-bindings` と
  `/effect-preauthorizations` で locator metadata、Provider instance、精確 scope binding と LOW-only
  policy を管理する。Response に Secret locator、connection config 本文、credential は含まれない。
- `POST /projects/{id}/runs/{run_id}/proposals/{proposal_id}/decision` は表示中の Proposal version、
  checksum、`Idempotency-Key` を必須とする。初版は Run 起動者または system ADMIN だけが承認できる。
- `POST /runs/{run_id}/cancel` は現状に応じて取消 intent または取消終態を保存する。実行中などは `cancellation:REQUESTED`、即時に取消可能または既に取消済みなら `cancellation:CANCELLED` を返す。SUCCEEDED / FAILED が先に確定していれば `409 run_not_cancellable`。応答の status と後続 snapshot を確認し、HTTP 成功や CANCELLED を実 process の退出証明としない（[提交順序](../design/run-supervision.md#提交时谁决定最终状态)）。
- `GET /runs/{run_id}/events` は SSE。`Last-Event-ID` と `after` query で永続 event から再開でき、一時 `TEXT_DELTA` は event ID を進めない。
- Evaluation は追加式で、revision の `original_value` は server が不変 Result から補完する。
- `POST /skills/parse` と Organization 作用域の `/skill-imports` は ADMIN 専用。deterministic parser はモデル呼び出しも script 実行も行わない。
- Skill 管理は ADMIN、Run/Evaluation は該当 Project の ACTIVE member（ADMIN は組織内 bypass）が実行できる。

上記は現在の response 規約であり、配備先での動作確認とは分ける。工作副本は現在の認証/Project 授権後、task の公開状態や資源を再解決する前に元の作成要求を照会する。版の無効化や文書変更後も、原 actor と同じ意図なら既存記録の確認へ進めるが、未知形式や証明できない旧要求は `409 idempotency_conflict` になる。新規作成と runtime の資源検証は緩めない。詳細と未検証範囲は[Run 作成](../design/run-creation.md)を参照する。

応答が失われたときは同じ内容と key を保持する。上例の `example-request-001` は説明用であり、新しい実行へ使い回さない。復旧のためだけに自動で新しい key を生成すると、実際には作成済みの Run と重複する可能性がある。

現行 Web はページ内の原要求確認で同じ内容/key を再送し、新規実行は明示確認後に別 key を発行する。refresh・離頁・actor/Project 切替で原要求の memory は失われるため、権限内の Run 履歴/detail で確認する。CSRF 更新と actor 変更を同一視しない。HTTP timeout は Run 取消ではない。詳細は[界面の責任](../design/run-creation.md#提交结果未知时的界面责任)を参照する。

## 文書を選び、元の範囲を確認する

TaskCatalog の候補に基づき、sources に単一文書、集合、明示的な全集のいずれかを渡す。候補の先頭や Provider 名を同意の代わりに使わず、任意文書を使わない場合はその slot を省略する。必須未選択、無効 ID、2 件未満の集合、空または上限超過の全集は拒否される。正確な token と制約は[資源設計](../design/resource-snapshots.md#公开选择与读取投影的实施契约)、JSON は[契約と example の対応表](../../PJM/contracts/README.md#run-文書契約を読む)を参照する。

作成済み Run の detail.document_snapshots は作成時の文書一覧を返す。FROZEN のみ検証済みメンバーを含み、歴史欠損と検証失敗は別状態で snapshot:null になる。履歴一覧の selected_sources は摘要のみで、内部 binding や全メンバーを含まない。文書一覧の存在は現在の download 可否や実行成功を保証しない。

現在の Web は document_snapshots を必須として検証する。新 API が旧 Run を明示的な歴史状態で返す場合と、旧 API が field 自体を返せない場合を分ける。後者は契約エラーになり、空集合とは扱わない。配備側の版を確認し、片側だけの更新・回退は[版互換の手順](contract-workflow.md#历史数据兼容不等于前后端版本兼容)に従う。
