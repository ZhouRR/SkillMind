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

文書 sources の新しい選択/快照は工作副本で联调中であり、現状の公開型だけで全挙動を保証できない。とくに全集の変更・削除後の idempotent replay は[資源設計の未完了事項](../design/resource-snapshots.md#创建重放与调度)を確認する。同じ依頼の再送には同じ key、入力を変えた新規実行には新しい key を使用し、異常回避のために監査 snapshot を変更しない。
