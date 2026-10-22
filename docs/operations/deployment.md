# 配備・移行・起動

Windows でイメージを構築し、Linux で Docker Compose + GNU make により配備する。初回・更新ともに `make deploy` を実行する。初回設定は [Quickstart](quickstart.md)、データ復元は[復元](backup-recovery.md)を参照。

## 環境ファイルと設定境界

サーバーの同一配備ディレクトリに `images.tar`・`compose.yml`・`.env`・`Makefile` を配置する。[Compose](../../SKM/compose.yml) がサービスを、[Makefile](../../SKM/Makefile) が配備手順を定義する。更新時はイメージアーカイブ・Compose・Makefile を差し替え、サーバーの `.env` は保持する。イメージには system Interpreter のみが含まれ、業務 Skill は画面から取り込む。

既定ではカレントディレクトリの `.env` を読み込む（dotenv を `source` しないこと）。別パスを使う場合は `export ENV_FILE=/path/app.env` の後に `make deploy` を実行し、生の Compose コマンドには `--env-file "${ENV_FILE:-.env}"` を渡す。更新時は元の `COMPOSE_PROJECT_NAME` とデータボリュームを維持する。

既存の Traefik と外部ネットワークを再利用し、host port は公開しない。ドメイン・TLS・ネットワーク・`SKILLMIND_CONTEXT_PATH` を一致させること（Web の context path はイメージに焼き込まれるため、変更時は Web の再構築が必要）。`SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID` は非ゼロ UUID とし全 Backend で共有する（新規ストレージでは新規生成し、更新時は既存値を維持）。Excel アップロードには `.xlsx` / `.xls` の実 MIME タイプを許可リストに含める。

## Agent SDK と device code ログイン

API・業務 `worker`・`maintenance` は Backend イメージと `.env` を共有する。業務 Worker はモデルとツールを実行し、保守 Worker は専用キューで Outbox・回収・定期発火を実行する（Run/Codex ボリュームはマウントしない）。更新時は 3 サービスすべてを含める。

`SKILLMIND_AGENT_SDK` で Interpreter と Run のエンジンを選択する（既定は Codex、設定は[環境テンプレート](../../SKM/.env.example)を参照）。更新時はサーバーの既存値を維持し、不足項目のみを補う。配備後にデバイスログインを実行する（ログインとセッションは `codex-data` ボリュームに保存され、更新時も維持される）。

```bash
docker compose --env-file "${ENV_FILE:-.env}" exec -T worker python -m skillmind.agent.codex_login
docker compose --env-file "${ENV_FILE:-.env}" exec -T worker python -m skillmind.agent.codex_login --status
```

プロキシが必要な場合は `SKILLMIND_WORKER_HTTP_PROXY` / `SKILLMIND_WORKER_HTTPS_PROXY` / `SKILLMIND_WORKER_NO_PROXY` を設定して業務 Worker を再作成する。Claude を使う場合は `SKILLMIND_AGENT_SDK=claude` と Anthropic 互換エンドポイント・資格情報・モデルを設定し、切替前に処理中タスクを整理して Backend を一括更新する（エンジン間でセッションは互換しない）。

## 移行前提と実行

`make deploy` は対象 Compose project のアプリサービスを停止し、マイグレーション後にバックグラウンドを自動復旧してキューを消費する。`SKILLMIND_WORKER_DISPATCH_ENABLED=false` は新規業務 job の配送のみを止め、処理中呼出の取消や保守プロセスの停止は行わない。

必要に応じて以下の機能スイッチを有効化する（既定はすべて `false`、API/Worker/Maintenance で一致させること）。

| 設定名（接頭辞 `SKILLMIND_`） | 用途 |
| --- | --- |
| `WORKER_DISPATCH_ENABLED` | Run・Effect・解釈・照合 job の配送と実行 |
| `DATABASE_WRITES_ENABLED` | PostgreSQL INSERT/UPDATE/DELETE |
| `DOCUMENT_WRITES_ENABLED` | Project 文書ライブラリの Artifact CREATE |
| `GIT_WRITES_ENABLED` | 制御下 Git commit/push |
| `MCP_TOOLS_ENABLED` | 認可済み MCP ツール検出・読取・制御下呼出 |
| `SCHEDULING_ENABLED` | 定期発火（業務配送の有効化も必要） |
| `DEFERRED_FEATURES_ENABLED` | その他後置能力のスイッチ |

機能スイッチに加え、資源範囲・タスク束縛・承認の設定が必要となる（境界は[実行・データ保守ガイド](../development/runtime-guide.md)を参照）。原生 SQL は DB アカウントの業務テーブル権限を使い、対象 DB にプラットフォーム用テーブルを作成しない。旧 `database.write/v1` を明示利用する場合だけ[回執テーブル SQL](../../SKM/scripts/sql/postgres-effect-receipts.sql) が必要となる。MCP 接続は完全な Streamable HTTP `/mcp` URL と資格情報を入力し、資源編集画面でツールを検出してアクセス権を保存すると有効になる。

### Windows 構築と移送

ソース `SKM/` の PowerShell で実行する（再出力時は `-Force` を付与）。

```powershell
docker compose build api web
.\scripts\export-images.ps1
```

スクリプトはローカルの `skillmind/backend:0.1.0`・`skillmind/web:0.1.0` のみを出力する（ビルドアーキテクチャはサーバーと一致させること）。完全オフライン環境への初回配備では基盤イメージも取得して同梱出力できる。

```powershell
docker compose pull postgres redis object-storage object-storage-init
.\scripts\export-images.ps1 -IncludeInfrastructure -Force
```

### Linux 検証と配備

4 ファイルを置いたディレクトリで `make deploy` を実行する。主な段階は以下のとおり。

| 段階 | 動作 |
| --- | --- |
| configuration / cleanup-old-images / load-images / prepare-images | 設定検証、未参照の旧アプリイメージ削除、イメージ読込とアーキテクチャ確認 |
| stop-application / infrastructure / initialize-storage | 現アプリの停止、基盤起動、バケット作成 |
| migration-check / migrate / readiness | マイグレーション経路検証、`upgrade head`、DB/Redis/ストレージ設定検査 |
| backend / runtime-check / web / cleanup-replaced-images | API/Worker/Maintenance の一括再作成、整合確認後の Web 復旧と旧イメージ削除 |

失敗時は元エラー・段階・終了コードを表示して後続手順を停止する。状態を確認してから再試行を判断する。

```bash
make status
make logs
docker compose --env-file "${ENV_FILE:-.env}" run --rm -T --no-deps migrate alembic current
docker compose --env-file "${ENV_FILE:-.env}" run --rm -T --no-deps migrate alembic heads
```

## 一括更新と検査

`make runtime-check` は Backend 各レプリカの実 image ID と設定 fingerprint を照合する（モデル呼出や業務データのクエリは行わない）。

## 移行とロールバック審査

今回関連する [migration](../../SKM/backend/migrations/versions/) とプロトコル消費者を照合する。未知の revision・複数 head・マイグレーション失敗時はまず DB 状態を確認し、`stamp`・監査削除・キュー消去を行わない。旧イメージは現在の schema・スナップショット・キューと互換する場合のみ戻せる（[アプリ版のロールバック](backup-recovery.md#アプリ版のロールバック)を参照）。

## Web のみの更新

フロントエンドのみの変更では、`web` イメージのみを構築・出力してサーバー（`skillmind/backend:0.1.0` が存在すること）で `make deploy` を実行できる。配備前に API・契約・context path の互換性を確認すること。

