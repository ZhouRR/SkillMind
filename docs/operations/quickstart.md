# 起動と初期管理者

初回起動の手順。既存環境の更新は[公開・移行](deployment.md)、データ復元は[backup・復元](backup-recovery.md)、故障時は [Runbook](runbook.md)へ進む。コマンドは `PJM/` で実行する。

## Compose の前提

Linux、Docker Engine / Compose v2、共有 Traefik と external edge network が必要。ProjectMind は Traefik を配備せず、host port を公開しない。

`.env` が無い初回だけ実行し、既存設定を上書きしない。

```bash
umask 077
cp .env.example .env
```

host/context path、Traefik network/entryPoint、DB/storage password、model 設定を対象環境に合わせる。production は HTTPS を使う。`ENV_FILE` は Backend の固定 `.env` を切り替えないため、[設定境界](deployment.md#环境文件与配置边界)を確認する。

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env build
make run
make
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
```

各段階の失敗時は停止する。`--quiet` は展開した Secret を表示しない。`make run` は build しないため、移送済みの検証済み image を使う場合だけ build 行を省く。migration、bucket 初期化と各 service の状態を確認する。preflight は DB migration/Redis の基線で、業務実行の保証ではない。

新 Run/Effect の配送は既定で無効。業務実行には `PROJECTMIND_WORKER_DISPATCH_ENABLED=true` が必要だが、false は既存 job・Schedule・recovery を止める[保守 mode ではない](deployment.md#一个例子关闭-dispatch-后仍有工作)。

## 最初の ADMIN を作成する

migration 成功後、既存データを消さない CLI を一度実行する。

```bash
docker compose --env-file .env exec api python -m projectmind.ops.bootstrap_admin
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

Windows PowerShell の `PJM/` で `./scripts/export-images.ps1` を実行する。既定出力は `images/projectmind-images.tar`。script は本地の既存 image を保存するだけで build/pull せず、存在しない第三者 image を除外することがある。archive を上書きせず、実 image ID と必要 image を確認する。context path を変える場合は Web も再 build する。

転送後は[公開・移行](deployment.md#迁移前置与执行)へ進む。`make deploy` は旧 app image を削除し、archive を load して Worker を含む全 service を再起動するため、分段放行が必要な現場では直接使わない。

## 操作の区別

`make` は状態表示、`make stop` は container を保持して停止、`make down` は container/network を削除して永続 volume を保持する。起動・更新・全初期化は上記の異なる操作であり、相互の代用にしない。
