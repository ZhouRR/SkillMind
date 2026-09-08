# API 利用ガイド

> コマンドは `PJM/` 起点。例の ID・host・token は placeholder。初回の疎通確認は [運用 smoke](../operations/runbook.md#41-通常-smoke) が対話ログインを扱うが、専用 Project に Run/Evaluation を作る検証であり読取専用ではない。

## 認証してから呼び出す

全業務 API（Project、Skills、Run、Result、Evidence、Evaluation、SSE）は認証必須である。まず login して session cookie と CSRF token を取得する。

| 順序 | API | 保存・送信するもの |
| --- | --- | --- |
| 1 | `GET /api/v1/auth/login-context` | login context cookie と response の login CSRF |
| 2 | `POST /api/v1/auth/login` | 上記 cookie、Origin、login CSRF、[login request](../../PJM/contracts/auth/v1/login-request.schema.json) |
| 3 | 業務 API | login response の session cookie と業務用 csrf_token |

実 password を `curl --data '{...}'` の command 行や shell history に置かない。HTTP client の保護された入力を使い、request/response の Secret を log しない。以降の例は認証済み cookie jar の path を `COOKIE_JAR`、業務 CSRF を `SESSION_CSRF` として渡した後の例である。cookie jar は owner のみ読み書き可能にし、共有せず、利用後は安全に破棄する。

以降の unsafe request（POST/PATCH/PUT/DELETE）は session cookie に加えて `Origin` と `X-CSRF-Token`（login/session response の値）を送る。Session token を Web storage に保存してはならない。

Project 選択は `GET/PUT /api/v1/users/me/project-preference` で User account に保存する。Web の hash URL は `?project=<uuid>` を含められ、URL で指定した Project が無効または権限外の場合は別 Project へ暗黙に fallback しない。

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
- `POST /runs/{run_id}/cancel` は取消 intent を永続化する。実行前 Run は即時 `CANCELLED`、実行中は Worker が SDK session を interrupt してから終態化する。
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
