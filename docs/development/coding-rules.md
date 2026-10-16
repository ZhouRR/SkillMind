# 変更種別ごとの実装細則

[AGENTS](../../AGENTS.md)に従い今回の変更に関連する章のみを参照する。領域規則は[変更ガイド](change-guide.md)、実行コマンドは[ローカル開発](local-development.md)を参照。
Backend パスは `SKM/backend/src/skillmind/` 相対、Web パスは `SKM/web/` 相対で記す。

## Backend

route は認証・入出力変換のみを担い、業務規則は domain/service、クエリ・ロック・永続化は repository に置く。公開 route は `api/routes/` の資源モジュール単層で実装し、未認証の並行入口・転送 wrapper・ARQ job 内の業務分岐を設けない。

同義の処理は単一入口を再利用する。

| 処理 | 入口 / 制限 |
| --- | --- |
| JSON / SHA-256 | `core/hashing.py`：`canonical_json` / `sha256_hex`。パッケージ索引は `skills/importer.py` の形式で計算し、JSON と生バイト要約を混用しない |
| 機密項目 | `core/redaction.py`：`find_sensitive_key`。ツール応答・Evidence・最終結果は `password` 項目名のみで拒否せず、設定・資格情報の宣言は厳格検査を維持 |
| Lease token | `runs/domain.py`：`lease_token_hash` |
| RunEvent / Outbox | `runs/repository_base.py` の `RunRepository`：`_snapshot_event`、`_event_outbox`、`_dispatch_outbox`、`_next_sequence` |
| 認証 Problem | `api/auth_dependencies.py` の共有 factory。Run は `routes/runs.py` の `run_not_found_problem` / `authorized_run` |
| binding / Secret | `agent/run_binding.py`：`load_bound_run_resource` / `resolve_binding_secret`。checksum・Integration 状態・provider/revision/capability を検証 |
| 外部 repository | `agent/repository_client.py` / `repository_source.py`。実体化・read・write で共用し、独自の subprocess や資格情報解決を書かない |
| apply 可能な effect | `effects/catalog.py`：`EFFECT_CAPABILITIES`。作成・実行・承認で共用 |
| Binary 文字列化 | `agent/binary_text.py`：`render_text` / `convert_excel_to_markdown`。Provider 側へ複製しない |

`documents/__init__.py` は domain 型のみを公開し、service/repository/source は明示 import して DB → Run → 文書の循環を防ぐ。

Git 書込は上記共有実装に従い、`direct` は既定 branch の fast-forward のみ、`branch` は `skillmind/` 名前空間の新規 branch のみとする。認可・承認・結果不明時の扱いは[外部効果](runtime-guide.md#外部効果)を参照。

## Run lifecycle

実行・claim・継続・取消を変更する際は[実行と復旧](runtime-guide.md#実行と復旧)を、画面表示のみは [Workspace](../design/workspace.md) を参照。

- 状態遷移は `plan_run_transition` / `ALLOWED_RUN_TRANSITIONS` のみを通す。ロック順は `Run → Segment → Attempt`（旧 migration 経路は `Run → Attempt` を維持し、`_lock_claimed_execution` を逆転させない）。
- 終端 `RUN_SNAPSHOT` が最後の RunEvent となる。`sequence` は Run 内で単調増加し、`TEXT_DELTA` は sequence を消費するが永続化せず、RunEvent Schema に従う。
- 再 dispatch は `SKILLMIND_RUN_MAX_ATTEMPTS` で制限し、上限到達時は `retry_exhausted` で `FAILED` に閉じる。`wall_timeout_seconds` はイベント待機のみを中断し、終端トランザクションは中断しない（ARQ `job_timeout` はさらに長く設定する）。
- 子能力は `resolve_subagent_capabilities` で絞り込み、予算配線と隔離は[後置能力](runtime-guide.md#後置能力)に従う。
- 文脈準備は[入力と文書](runtime-guide.md#入力と文書)、結果検証は[結果と評価](runtime-guide.md#結果と評価)に従い、`TaskSchedule` は `RunService.create_task_run` を再利用する。

## Web

- 資源 client は `src/api/` に置き、画面からは `index.ts` barrel 経由で呼ぶ。HTTP は `requestApiJson` / `requestApiEmpty` に統一し、mutation は `csrfToken` / `X-CSRF-Token` を渡し、純粋ロジックは `src/lib/` に置く。
- `TasksPage` でタスクを選択し、`WorkspacePage` で待受とレポートを扱い、Run 詳細は実行履歴に属する。即時起動とスケジュールは `taskDraft.ts` / `TaskLaunchFields.tsx` を共用する。
- フロントエンドでサーバー側 ID（`derive_task_id` 等）を再計算せず、絞り込みとページングはサーバーを正とする。三語は `src/lib/i18n/{zh,ja,en}.ts` で管理し、画面は `useMessages()` を使う。
- 要求状態は対応 hook/コンポーネントが保持し、同期多重送信防止と遅延応答の帰属確認はボタンの `disabled` に依存しない。子コンポーネント定義はモジュール最上位に置き、下書き編集時のサブツリー再構築によるフォーカス喪失を防ぐ。生成 `FrontendModule` の隔離要件は[後置能力](runtime-guide.md#後置能力)を参照。

## 同期点

影響箇所のみを更新し、公開契約は[契約 workflow](contract-workflow.md)も併せて遵守する。

| 変更対象 | 同期範囲 |
| --- | --- |
| Settings | `core/settings.py` の Field 範囲 → `.env.example` → API lifespan / Worker startup 注入 |
| AgentEventType | `agent/domain.py` → `engine.py` mapper → Web `RUN_EVENT_NAMES` → RunEvent Schema |
| SkillExecution / Brief / OutcomeEnvelope / 旧 Blueprint | contract → DTO/validator/projector → frozen checksum → Worker/Web → example/test |
| Segment / Interaction / Proposal | transition → DB/repository → event/outbox/SSE → 認可/冪等 → Web → recovery/Compose |
| Tool capability | versioned request/response/error → Provider → `tool_catalog.py` 登録 → `context_builder.py` 凍結束縛/`allowed_capabilities` → 回帰 |
| endpoint | 資源 route/actor alias → response allowlist/Problem → Schema/example/OpenAPI → Web validator/barrel → 回帰 |
| DB model | `db/models.py` → migration → repository DTO/projection → 回帰。relationship のない FK 親子を同一トランザクションで追加する際は明示的に `flush()` する |
| Schema / example | `scripts/validate_contracts.py` と `backend/tests/contracts/test_contracts.py` の双方に登録 |
| OpenAPI | endpoint 変更後に `scripts/export_openapi.py` を実行して整合を検証（読取専用レビューでは再出力しない） |
| 構造化 log | `core/logging.py` の `_CONTEXT_FIELDS` allowlist に登録 |
| system Interpreter Skill | SemVer/content hash・example identity・prompt checksum・テストを同期 |

`tests/` はモジュール責務を反映し、API テストは `tests/api/fakes.py` / `conftest.py` を共用する。fake SQL と[実 DB](local-development.md#実-postgresql-の前提)の証跡は区別して記録する。

## Ingress

共有 Traefik を再利用し、`web`/`api` のみを edge network に参加させ、host port は公開しない。外部入口は `SKILLMIND_CONTEXT_PATH` 配下に統一し、API 転送時にプレフィックスを除去する。設定と移行は[配備手順](../operations/deployment.md)を参照。

