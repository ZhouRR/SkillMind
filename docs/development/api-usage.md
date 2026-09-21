# API 利用ガイド

配備先の context path 配下に `/api/v1`、対話式仕様に `/api/docs` がある。正確な method/path/field は [OpenAPI](../../SKM/contracts/openapi/skillmind-api.v1.json) と [Contracts](../../SKM/README.md#contracts) を参照する。

## 認証

| 呼出元 | 方法 |
| --- | --- |
| 外部アプリ | `Authorization: Bearer <API_KEY>` または `X-API-Key: <API_KEY>` のどちらか一つ |
| ブラウザ session | GET `/auth/login-context` → POST `/auth/login`。cookie、Origin、login CSRF を使用し、以後の unsafe request に業務用 `X-CSRF-Token` を送る |

API Key は ADMIN が `/api-keys` で作成・一覧・失効でき、token 全文は作成時だけ取得する。現在は組織内全資源を扱う Key で、細粒度 scope はない。明示 Key の不正・重複時は拒否し、cookie へ fallback しない。Key 認証の API 操作は session login/CSRF を要しない。

資格情報を URL・command history・共有 log に置かない。login の 429 は Retry-After に従い、503 を password 誤りと扱わない。session 失効後の mutation を自動再送しない。アカウント契約は [users/v1](../../SKM/contracts/users/v1/)。

## タスクの開始と観察

1. GET `/projects/{id}/tasks` で精確な SkillVersion/task_key、入力 Schema と資源候補を取得する。
2. POST `/projects/{id}/task-runs` に[作成契約](../../SKM/contracts/runs/task-create/v1/request.schema.json)と新規 `Idempotency-Key` を送る。文書範囲と必要入力を省略しない。
3. GET `/projects/{id}/runs/{run_id}/detail` と Run events を読む。作成受理、プラットフォーム終了、業務成功は別の状態として扱う。

SSE の `Last-Event-ID` / `after` は持続 event の再開に使う。一時 TEXT_DELTA で再開位置を進めない。キャンセルは Run の cancel endpoint を使い、受付後も終態を確認する。

終了履歴は Project 配下の `/runs/{run_id}` を DELETE して回収する。このパスに付ける `/deletion-preview` の GET で対象を確認でき、`/restore` の POST で復元、`/purge` の DELETE で完全削除する。完全削除は回収後に行い、原入力・共有参照・最小監査を保護する。外部 DB や Git のデータは削除されない。

## 文書とディレクトリ

[documents route](../../SKM/backend/src/skillmind/api/routes/documents.py) と OpenAPI から、一覧・アップロード・元 byte 取得、`document-folders`、`document-operations` を利用する。改名、移動、directory 作成、回収・復元は共通操作契約を使う。完全削除は `DELETE /projects/{project_id}/documents/{document_id}?purge=true` を使い、参照保護と回収状態を確認する。表示 path を blob key として組み立てない。

タスクの文書入力用 `sources` 値は `document:<UUID>`、`documents:<UUID>,<UUID>...`、明示的な `project-documents:all` を使う。directory 選択は、候補一覧からその配下の文書 ID を集合にして送る。directory path 自体は指定せず、作成後に追加された文書を旧 Run に含めない。任意 slot 未使用は省略する。
Run detail の document_snapshots は作成時の範囲であり、現在の内容可読性を保証しない。LEGACY_UNAVAILABLE/INVALID を空の正常 snapshot に置換しない。

## 回答・批准・評価

- 普通の回答は `/projects/{id}/runs/{run_id}/interactions/{interaction_id}/responses` に原 version・[回答契約](../../SKM/contracts/runs/interaction-response/v1/request.schema.json)・Idempotency-Key を送る。
- Proposal decision は普通回答と別 endpoint。原 version/checksum を維持し、現在の権限と適用可能な起動時同意を再確認する。
- 人工評価は `evaluation-submissions` に submission_key と result_id を付ける。[評価契約](../../SKM/contracts/evaluations/v1/create-request.schema.json)に従い、元 Result を上書きしない。

## 再送と復旧

timeout/abort は取消や rollback の証明ではない。原 actor・内容・key を保って確認し、結果不明のまま別 key で再実行しない。409 は競合、410 は期限を確認し、Run/Interaction の現状を再読する。404 も原操作の未実行証明ではない。

現在の認証と Project 授権は確認時にも必要。旧 Run を現在設定から再構成せず、[版互換](contract-workflow.md#历史数据兼容不等于前后端版本兼容)と[実行境界](runtime-guide.md)を維持する。
