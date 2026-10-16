# Skillmind 運用・障害対応

症状から対象サービスと原要求を特定して対処を決める。起動は [Quickstart](quickstart.md)、更新は[配備](deployment.md)、データ復元は[復元](backup-recovery.md)、実行境界は[実行・データ保守ガイド](../development/runtime-guide.md)を参照。

## 運用原則

原応答・Run/Attempt/Effect ID・マスク済みログを優先確認する。PostgreSQL が正本であり、エラー解消のために状態書換・監査削除・キュー消去・キー変更を行わない。共有するのは UTC・版・オブジェクト ID・公開エラーコード・影響範囲のみとし、`.env`・資格情報・業務本文は出さない。調査コマンドは `ENV_FILE`/project を確認した配備ディレクトリで実行する。

## サービスとキュー

```bash
make status
make runtime-check
docker compose --env-file "${ENV_FILE:-.env}" logs --no-log-prefix --tail=200 api worker maintenance
```

| 症状 | 確認事項 |
| --- | --- |
| API 不健全または版不一致 | 配備段階・migration head・DB/Redis・`runtime-check`。[配備](deployment.md#linux-検証と配備)に従い元エラーを特定 |
| 新規タスク未実行・承認後に継続しない | dispatch スイッチ・`worker` と `maintenance` の健全性・原 Outbox/job。両キューの稼働が必要であり、Run の再作成で実行を促さない |
| 実行や画面更新が遅い | [性能観測](run-performance.md)に従い準備・モデル/ツール待機・収束・表示を切り分ける |
| モデル認証や通信の失敗 | Worker のデバイスログイン状態・プロキシ・認証/モデルサービスへの疎通 |

## 認証障害の読取トリアージ

path（クエリなし）・status/code/request_id を記録し、ヘッダ有無・cookie 属性・Origin のみを確認する（機密値は記録しない）。

| 応答 | 対処 |
| --- | --- |
| 401 `invalid_credentials` / `authentication_required` | 環境・セッション期限・権限を確認。`bootstrap-admin` は最初の ADMIN 作成専用でありアカウント初期化には使えない |
| 403 `csrf_rejected` | Origin・CSRF header・現セッションを確認。未確認要求を保全し、そのまま再送しない |
| 403 `administrator_required` / 404 | システム ADMIN と ProjectMember を区別する（404 は越権の秘匿でも返る） |
| 409 `project_archived` / 版競合 | 対象オブジェクトの最新状態を読み、原要求を保全する（アーカイブは処理中 Run/Effect を取消さない） |
| 422 | 公開契約に従い入力を修正し、パスワードを含む原値は共有しない |

`login-context` は challenge を発行しクォータを消費するため、ヘルスチェック用途で反復呼出しない。

## ログイン防護の調査と復旧

429 は `Retry-After` に従い再試行を止め、共有出口 IP や信頼プロキシを確認する。503 `login_protection_unavailable` は Redis の権限・容量・TTL・タイムアウトを確認し、制限回避のための `FLUSHDB`・クォータ削除・送信元ローテーションは行わない。

## 文書保存と削除の読取トリアージ

Project/document ID・原 upload key・応答を保全する。改名・移動・復元は同一文書 ID を維持するため、ID と内容ハッシュで照合する。

過去実行から参照されている知識ベース等のファイルは行メニュー「ファイルを更新」を使うか、ファイル/フォルダの一括投入で同ディレクトリ同名ファイルを上書き更新できる（旧ファイルの事前削除は不要）。新規タスクは新版を選択し、過去実行は旧版を維持する。更新競合時は一覧を再読込してから再操作する。

| 症状 | 対処 |
| --- | --- |
| アップロード結果不明・`PENDING`・原 key 不在 | 原 key のまま回執を確認し、キーを変えて再送しない |
| `PUBLISHED` だが一覧にない | 原 document ID で後続の移動/削除を確認し、同名で新規作成しない |
| 削除 409 | Run・スケジュール・履歴参照を確認し、参照解消を確認できない間は保持する |
| 削除 503 または応答中断 | まず原文書の現在状態を読み、原アップロード記録とストレージを照合する（ID を変えてやり直さない） |
| ダウンロード欠落/hash 不一致・ストレージ切替後の読取不可 | 原 ID/key/hash と復元点で照合し、hash 書換や namespace 流用を行わない |
| 削除後もオブジェクトやクォータが残る | メタデータ削除はストレージ清掃やクォータ精算の完了を意味しない。ストレージ接頭辞を手動再帰削除しない |

ごみ箱からは復元可能だが、完全削除は復元できない（原入力・共有参照・結果不明操作の保護対象）。Project の実行一覧が空でも削除監査等の参照が残る場合はアーカイブを維持する。

## 準備障害の読取トリアージ

`RUNNING` で Session がない場合は Context 準備・入力回執・Brief を確認する。`preparation_timeout` は準備タイムアウト設定と資源応答時間を、`context_build_failed` は凍結ソースと束縛を確認する。引継ぎが繰り返される場合は lease・Attempt 時系列・エラーを確認する（lease 期限切れや取消受理は旧リモートプロセスの終了を意味しない）。

## WAITING 状態

`WAITING_FOR_INPUT/APPROVAL` は非終端状態であり、原 Interaction/Proposal と期限を確認して回答・承認する。要求タイムアウト・410・通信断時はまず原状態を読み、自動承認範囲は原 Run 開始時の同意に従う。

## 結果と評価の読取トリアージ

結果の `PARTIAL`/`BLOCKED`・証拠・プラットフォーム書込回執を併せて確認する（モデル自身の保存完了申告は read-back の代替にならない）。評価は原 Result（pointer は `detail.result.data` 相対）に対して行い、送信原キーを保持して結果不明時は重複 POST しない。

## Schedule と Recovery の監視

`SCHEDULING_ENABLED`・dispatch・保守 Worker・ルールタイムゾーン・原 occurrence を確認する。`next_run_at` が進んで Run がない場合は「処理中照合」を確認し、カウンタ消去やスケジュール再作成で補填実行しない。

## Incident と recovery

原 Proposal/Effect・要求キー・承認・リモート回執に沿って事実を照合する。読取照合の `CONFIRMED` は原操作が観測されたことのみを示し、`NOT_OBSERVED`/`CONFLICT` は自動再送を認可しない。結果不明や部分成功は原記録を保持し、キー変更・強制 push・Worker 再起動による復旧促進を行わない。外部 Git・DB・文書・MCP の変更はローカル収束より先に完了し得るため、DB 復元では外部変更を取り消せない。

## MANAGED Secret の KEK 運用

`SKILLMIND_MANAGED_SECRET_KEK` は `version:base64key` のカンマ区切り keyring（各 32 バイト、先頭が新規暗号化用、以降は復号用）を使う。ローテーション時は旧鍵を保全し、鍵を扱う全プロセスに「新 active + 旧鍵」を読み込ませてから以下を実行する。

```bash
docker compose --env-file "${ENV_FILE:-.env}" exec api python -m skillmind.ops.rotate_secrets
```

DB 内の暗号文が再ラップされる（終了コードと `rotated`/`skipped` を確認）。旧バックアップに必要な鍵は独立して保管する（DB バックアップから紛失した KEK は復元できない）。

## 通常 smoke

承認された検証環境でのみ実行する（モデル呼出・Run/Evaluation 作成・取消検証を行う）。

```bash
docker compose --env-file "${ENV_FILE:-.env}" exec \
  -e SKILLMIND_SMOKE_PROJECT_ID="${SMOKE_PROJECT_ID:?Specify the test project UUID}" \
  api python -m skillmind.ops.smoke
```

パスワードは TTY から入力する。`SKILLMIND_SMOKE_SKILL_VERSION_ID`・`TASK_KEY`・`TASK_INPUT_JSON`・`TASK_SOURCES_JSON` で対象タスクを指定できる。

