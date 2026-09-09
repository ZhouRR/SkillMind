# 起動と初期管理者

初回起動の手順。既存環境の更新は[公開・移行](deployment.md)、データ復元は[backup・復元](backup-recovery.md)、故障時は [Runbook](runbook.md)へ進む。コマンドは `PJM/` で実行する。

## Compose の前提

Linux、Python 3.12（標準 library のみ）、Docker Engine / Compose v2、共有 Traefik と external edge network が必要。Compose は config JSON / up --wait を備えた版を対象環境で検証する。ProjectMind は Traefik を配備せず、host port を公開しない。

`.env` が無い初回だけ実行し、既存設定を上書きしない。

```bash
umask 077
cp .env.example .env
```

host/context path、Traefik network/entryPoint、DB/storage password、model 設定を対象環境に合わせる。production は HTTPS を使う。[共通設定入口](deployment.md#环境文件与配置边界)が ENV_FILE を補間と Backend の同一 source に固定する。既定以外の project は shell の COMPOSE_PROJECT_NAME または明示 option で選び、ファイル内の同名値に依存しない。

```bash
make config
make build
```

各段階の失敗時は停止する。config は展開した Secret を表示しない。移送済みの検証済み image を使う場合だけ build を省き、[配備清単](deployment.md#迁移前置与执行)の daemon/project/image ID・archive と確認変数を設定して deploy-load を行う。新規環境も既存の同名 project がないこと、入口閉鎖と必要な復元手段を確認する。

以下は対象の基盤 service と bucket を作成する操作。承認済み環境で一つずつ実行し、失敗時は進まない：

```bash
python3 scripts/compose.py -- up -d --no-build --no-deps --pull never --wait postgres redis object-storage
python3 scripts/compose.py -- up --no-build --no-deps --pull never --exit-code-from object-storage-init object-storage-init
```

同じ配備清単と確認変数で `make deploy-migrate` → `make deploy-api` を個別実行する。make run も API/Web の検査付き起動で、Worker は起動しない。初期 ADMIN と権限を確認した後に[背景放行](deployment.md#启动与放行)を別途承認する。preflight は DB migration/Redis の基線で、業務実行の保証ではない。

新 Run/Effect の配送は既定で無効。業務実行には `PROJECTMIND_WORKER_DISPATCH_ENABLED=true` が必要だが、false は既存 job・Schedule・recovery を止める[保守 mode ではない](deployment.md#一个例子关闭-dispatch-后仍有工作)。

## 最初の ADMIN を作成する

migration 成功後、既存データを消さない CLI を一度実行する。

```bash
python3 scripts/compose.py -- exec api python -m projectmind.ops.bootstrap_admin
```

email、display name、password を対話入力する。migration が作成した Organization を使い、最初の ADMIN と CREATED 安全イベントを追加する。DISABLED を含め ADMIN が既にいれば拒否する。匿名 bootstrap API、既存管理者の password 再設定・復元機能ではない。

**`make bootstrap-admin` は全データ再初期化であり、通常の管理者作成には使わない。** PostgreSQL、Redis、object storage、Run workspace の volume を削除する。全消去を明示的に選び、対象と復元手段を確認した場合だけ使用する。

## 最初の業務実行

1. ADMIN でログインし、Project とメンバーを用意する。
2. Skill を導入・解釈・確認・公開し、Project で精確 SkillVersion を有効化する。
3. 必要な Integration、SecretReference、binding と文書を用意する。
4. Task Center で就緒度と入力を確認し、Workspace で実行する。
5. 待機、結果、Evidence と Evaluation を確認する。

## Image 移送と更新

Windows PowerShell の `PJM/` で `./scripts/export-images.ps1` を実行する。Python 3.12 が必要で、必要なら -PythonCommand に executable を指定する。既定出力は `images/projectmind-images.tar`。script は既存 image を保存するだけで build/pull せず、存在しない第三者 image を除外することがある。archive と実 image ID/checksum を受控清単に記録する。context path を変える場合は Web も再 build する。

転送後は[公開・移行](deployment.md#迁移前置与执行)へ進む。make deploy は help のみで、load/migrate/api/worker を個別に進める。旧 image は削除せず、各段階の失敗時に自動復旧・後続起動をしない。

## 操作の区別

`make` は状態表示、`make stop` は container を保持して停止、`make down` は container/network を削除して永続 volume を保持する。起動・更新・全初期化は上記の異なる操作であり、相互の代用にしない。
