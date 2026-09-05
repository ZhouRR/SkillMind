# API 利用ガイド

> コマンドは `PJM/` 起点。例の ID・host・token は placeholder であり、実際の値へ置き換える。Cookie file は共有せず、利用後に保護・破棄する。初回の検証は [運用 smoke](../operations/runbook.md#41-通常-smoke) が対話ログインを扱う。

全業務 API（Project、Skills、Run、Result、Evidence、Evaluation、SSE）は認証必須である。まず login して session cookie と CSRF token を取得する。

```bash
BASE="https://${PROJECTMIND_HOST}${PROJECTMIND_CONTEXT_PATH}"
curl -c cookies.txt -s "$BASE/api/v1/auth/login-context"          # csrf_token を控える
curl -b cookies.txt -c cookies.txt -s -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/json" -H "Origin: https://${PROJECTMIND_HOST}" \
  -H "X-CSRF-Token: ${LOGIN_CSRF}" \
  --data '{"email":"admin@example.com","password":"********"}'    # response の csrf_token が業務用
```

以降の unsafe request（POST/PATCH/PUT/DELETE）は session cookie に加えて `Origin` と `X-CSRF-Token`（login/session response の値）を送る。Session token を Web storage に保存してはならない。

Project 選択は `GET/PUT /api/v1/users/me/project-preference` で User account に保存する。Web の hash URL は `?project=<uuid>` を含められ、URL で指定した Project が無効または権限外の場合は別 Project へ暗黙に fallback しない。

```bash
curl -b cookies.txt -s "$BASE/api/v1/projects/${PROJECT_ID}/tasks"

curl -b cookies.txt -i -X POST "$BASE/api/v1/projects/${PROJECT_ID}/task-runs" \
  -H "Content-Type: application/json" -H "Origin: https://${PROJECTMIND_HOST}" \
  -H "X-CSRF-Token: ${SESSION_CSRF}" -H "Idempotency-Key: example-request-001" \
  --data '{"skill_version_id":"<published-skill-version-uuid>","task_key":"<task-key>","input":{},"sources":{}}'
```

公開 API の全体は [OpenAPI snapshot](../../PJM/contracts/openapi/projectmind-api.v1.json)、配備先の `${BASE}/api/docs` を参照する。以下は主要な利用規則である：

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
