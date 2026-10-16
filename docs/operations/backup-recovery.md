# バックアップ・復元・ロールバック

本ページはデータ保護・障害復元・アプリ版のロールバックを扱う。通常の配備は[配備](deployment.md)を参照し、データ価値・移行・復元要件に応じてバックアップを実施する。

## 復元点の取得

DB・オブジェクトストレージ・実行ファイルを跨ぐ一致復元点を取得する際は、新規業務と発火を止め、処理中書込の収束を確認して全 writer を停止する。結果不明の外部効果は[原主体での照合](runbook.md#incident-と-recovery)に従う（単一インスタンスの停止や `dispatch=false` のみでは全体停止にならない）。

### 一致復元点の内容

| 資産 | 保全内容 |
| --- | --- |
| PostgreSQL | 完全 dump・DB 識別子・migration revision・書込停止時刻。スナップショット・原要求・承認・Outbox・監査を保持 |
| オブジェクトストレージ | Skill/文書/添付の原バイト・key/hash・DB 参照・ストレージ namespace/設定 |
| Run・Codex ボリューム | 復元対象の workspace・入力・transcript・原生セッション（ログイン材料は資格情報として保護） |
| イメージと設定 | アプリおよび依存 image ID・互換設定・context path |
| KEK | 現在および旧バックアップに必要な全バージョン（dump とは独立して管理保管） |
| 外部事実 | 原操作 ID・回執・リモート revision・復元点以降の書込や権限剥奪 |

復元点 ID・UTC 窓・資産参照と checksum・欠落項目・復元訓練結果を記録する。Redis は業務正本ではないため、旧キュー/lease/スケジュールは個別に照合し、Redis 消去を復元の代替としない。現在ストレージ横断の自動バックアップや統合復元ツールはなく、DB dump 単独ではシステム全体を復元できない。

### DB バックアップの取得と検査

対象配備ディレクトリで実行する（DB のみ、2 ブロックは同一シェルで実行）。失敗時に生成されたファイルは現場証跡として残し、有効なバックアップとは扱わない。

```bash
umask 077
SKM_BACKUP_DIR="$(mktemp -d ./skillmind-backup-XXXXXXXX)" &&
docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres sh -ceu \
  'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
  > "${SKM_BACKUP_DIR:?Backup directory is required}/database.dump"
```

成功後にアーカイブ目次を検査してハッシュを生成する（パスワードはコマンドラインに含めない）。

```bash
(
  set -eu
  : "${SKM_BACKUP_DIR:?Specify the directory created for this backup}"
  test -s "$SKM_BACKUP_DIR/database.dump"
  docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres pg_restore --list \
    < "$SKM_BACKUP_DIR/database.dump" > "$SKM_BACKUP_DIR/database.toc"
  docker image inspect skillmind/backend:0.1.0 skillmind/web:0.1.0 \
    --format '{{.RepoTags}} {{.Id}}' > "$SKM_BACKUP_DIR/images.txt"
  cd "$SKM_BACKUP_DIR"
  sha256sum database.dump > database.dump.sha256
  sha256sum --check database.dump.sha256
)
```

dump・TOC・実行ファイル・設定は管理下で保管する。checksum は完全性を示すが出所や復元可能性までは保証しないため、隔離環境での復元訓練で確認する。

## データベース復元

**以下の手順はデータベースを削除・置換し、復元点以降のローカルデータを失わせる。** 復元対象の特定・現状保全・dump と関連資産・互換イメージ・旧 KEK・書込停止範囲を確認してから実行する。事前に検証済みバックアップディレクトリを `SKM_RESTORE_DIR` に設定する。

### 復元前の停止条件

まず材料を検査し、失敗した場合は即中止する。

```bash
(
  set -eu
  : "${SKM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$SKM_RESTORE_DIR/database.dump"
  docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres pg_restore --list \
    < "$SKM_RESTORE_DIR/database.dump" > /dev/null
  cd "$SKM_RESTORE_DIR"
  sha256sum --check database.dump.sha256
)
```

他インスタンスの書込停止も確認した上で本 project のアプリを停止し、対象 DB とロールが復元先と一致することを確認する。

```bash
docker compose --env-file "${ENV_FILE:-.env}" stop api web worker maintenance migrate object-storage-init
docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres sh -ceu '
  psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc \
    --set=ON_ERROR_STOP=1 --command="SELECT current_database(), current_user"
'
```

### データベースの置換

以下は既定のアプリロールで復元する例である（複数 owner やカスタム権限がある場合は事前に手順を検証すること）。

```bash
(
  set -eu
  : "${SKM_RESTORE_DIR:?Specify the verified backup directory}"
  test -s "$SKM_RESTORE_DIR/database.dump"
  docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres sh -ceu '
    dropdb --force --username="$POSTGRES_USER" "$POSTGRES_DB"
    createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" "$POSTGRES_DB"
  '
  docker compose --env-file "${ENV_FILE:-.env}" exec -T postgres sh -ceu '
    pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
      --no-owner --single-transaction --exit-on-error
  ' < "$SKM_RESTORE_DIR/database.dump"
)
```

`--single-transaction` は `pg_restore` のみを保護し、失敗時は空 DB が残り得るため業務停止を維持する。DB 復元は外部書込を取り消さず、過去の権限や失効済みセッションを復活させ得る点に注意する。

### 復元後の検証

関連 blob/実行ファイル・互換設定・KEK を揃え、migration revision・資産バイト/参照・権限とセッション・キュー/lease/スケジュール・外部回執を照合する。入力・transcript・信頼できる回執を欠く Run は強制継続しない。検証中はバックグラウンドを自動起動する `make deploy` を使わず、生の Compose コマンドで段階的に事実を確認してから業務・保守 Worker を再開する。

## アプリ版のロールバック

旧 API/Web/Worker が現在の schema・データ・キュー・非終端スナップショットと互換することを確認した上で、検証済み旧イメージへ戻す。非互換の場合は隔離を維持して前方修正または完全復元を選択し、破壊的な `downgrade`・`stamp`・監査削除は行わない。

