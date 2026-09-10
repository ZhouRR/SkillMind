# 起動と初期管理者

初回起動の手順。更新は[公開・移行](deployment.md)、復元は[backup](backup-recovery.md)、故障は [Runbook](runbook.md)へ。コマンドは対象環境の `SKM/` で実行する。

## Compose の前提

Linux、Python 3.12 標準 library、Docker Engine / Compose v2、共有 Traefik と external edge network が必要。config JSON / up --wait 対応版を対象環境で検証する。Traefik の配備と host port 公開は行わない。

`.env` が無い初回だけ実行し、既存設定を上書きしない。

```bash
umask 077
cp .env.example .env
```

host/context path、Traefik、DB/storage password、model を設定し、production は HTTPS を使う。[共通入口](deployment.md#环境文件与配置边界)の ENV_FILE を使い、非既定 project は shell の COMPOSE_PROJECT_NAME または明示 option で選ぶ（ファイル内同名値は無効）。

新 storage 世代の UUID を `SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID` に設定し、API/Worker と復元清単で共有する。再作成時は新 UUID、未設定では文書 blob 操作を拒否する。旧文書は自動帰属されないため[移行条件](../design/document-lifecycle.md#存储归属与配置切换)を確認する。

```bash
make config
make build
```

失敗時は停止する。config は Secret を表示しない。検証済み image を移送した場合だけ build を省き、[配備清単](deployment.md#迁移前置与执行)と確認変数を使って deploy-load を行う。新規環境も同名 project の不存在、入口閉鎖、復元手段を確認する。

承認済み環境で基盤 service と bucket を順に作成する。失敗時は進まない：

```bash
python3 scripts/compose.py -- up -d --no-build --no-deps --pull never --wait postgres redis object-storage
python3 scripts/compose.py -- up --no-build --no-deps --pull never --exit-code-from object-storage-init object-storage-init
```

同じ清単/変数で `make deploy-migrate` → `make deploy-api` を個別実行する。make run も API/Web のみ。ADMIN/権限確認後に[背景放行](deployment.md#启动与放行)を別途承認する。preflight は DB/Redis の基線であり業務保証ではない。

新 Run/Effect 配送には `SKILLMIND_WORKER_DISPATCH_ENABLED=true` が必要。既定 false でも既存 job/Schedule/recovery は止まらず、[保守 mode ではない](deployment.md#一个例子关闭-dispatch-后仍有工作)。

## 最初の ADMIN を作成する

migration 成功後、既存データを消さない CLI を一度実行する。

```bash
python3 scripts/compose.py -- exec api python -m skillmind.ops.bootstrap_admin
```

email/display name/password を対話入力し、既定 Organization に ADMIN と CREATED 安全イベントを追加する。DISABLED を含め既存 ADMIN があれば拒否する。匿名 API や password 再設定/復元機能ではない。

**`make bootstrap-admin` は PostgreSQL・Redis・object storage・Run workspace の全 volume を削除する再初期化。通常の管理者作成には使わない。** 全消去を明示的に選び、対象と復元手段を確認した場合だけ使用する。

## 最初の業務実行

1. ADMIN でログインし、Project とメンバーを用意する。
2. Skill を導入・解釈・確認・公開し、Project で精確 SkillVersion を有効化する。
3. 必要な Integration、SecretReference、binding と文書を用意する。
4. Task Center で就緒度と入力を確認し、Workspace で実行する。
5. 待機、結果、Evidence と Evaluation を確認する。

## Image 移送と更新

Windows PowerShell の `SKM/` で `./scripts/export-images.ps1`（Python 3.12、必要なら -PythonCommand）を実行する。既定出力は `images/skillmind-images.tar`。build/pull はせず、application image 不足は失敗、第三者 image 不足は警告して除外する。context path 変更時は Web を再 build する。

既存 tar は既定で上書きしない。-ArchiveName で版別に保存し、-Force は成功後に同名 tar を置換するため唯一の回退資産には使わない；失敗時は旧 tar を保持する。実 image ID と送受信双方の checksum を清単で照合するが、checksum は出所の真正性を証明しない。export 成功後に[公開・移行](deployment.md#迁移前置与执行)へ進み、旧 image を保持する。make deploy は help のみで、失敗後に自動続行しない。

## 操作の区別

`make` は状態表示、`make stop` は container を保持して停止、`make down` は container/network を削除して永続 volume を保持する。起動・更新・全初期化の代用にはしない。
