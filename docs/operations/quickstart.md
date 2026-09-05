# 起動と初期管理者

> `PJM/` で実行する。既存環境の更新・復旧は先に [Runbook](runbook.md) を読む。

## Compose の前提

Linux、Docker Engine/Compose v2、既存の共有 Traefik と external edge network を用意する。ProjectMind は Traefik を配備せず、host port を公開しない。

```bash
cp .env.example .env
```

`.env` の host/context path/Traefik network・entryPoint、DB/object-storage password、model 設定を環境に合わせる。production は HTTPS、development の HTTP は開発専用とする。Worker dispatch は既定 false なので、業務実行する環境では `PROJECTMIND_WORKER_DISPATCH_ENABLED=true` を明示する。

```bash
docker compose --env-file .env config
docker compose --env-file .env build
make run
make
```

`make run` は配置済み image を使い、build しない。image を事前移送した server では build 行は不要。初回は migrate/object-storage-init の正常終了と API/Worker/Web の状態を確認する。

```bash
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
```

preflight は DB migration/Redis の基線であり、ログイン後の task execution まで検証しない。

## 最初の ADMIN を作成する

migration 成功後、既存データを消さずに一度だけ CLI を実行する。

```bash
docker compose --env-file .env exec api python -m projectmind.ops.bootstrap_admin
```

email、display name、password を対話入力する。既に ADMIN がいる場合は拒否される。CLI は migration が作成した Organization を使う。公開の匿名 bootstrap endpoint はない。

`make bootstrap-admin` は名前と異なり **全データ再初期化** target である。PostgreSQL、Redis、object storage、Run workspace の volume を削除してから ADMIN を作る。通常の初回管理者作成・追加・password 再設定には使わない。全消去を意図する場合だけ、backup/復元手段と対象環境を確認して使用する。

## 最初の業務実行

1. ADMIN でログインし、Project とメンバーを作成する。
2. Skill を導入・解釈・確認・公開し、その Project で精確 SkillVersion を有効化する。
3. 必要な Integration/SecretReference と binding、Project 文書を用意する。
4. Task Center で就緒度と入力を確認し、Workspace で実行する。
5. 待機、結果、Evidence と Evaluation を確認する。

旧 JAF bootstrap seed は新規タスクの前提にしない。migration 0024 は参照のない seed を退役させ、歴史 Run から参照されるものは監査用に残す。

## Image 移送と更新

Windows PowerShell、`PJM/` で実行する。

```powershell
.\scripts\export-images.ps1
```

archive の既定位置は `images/projectmind-images.tar`。第三者 image が local にない場合は archive から除外されることがあるため、server 側で必要 image を確認する。

[Runbook の backup](runbook.md#2-配備前-backup) と rollback 用 archive を確保した後、server で実行する。

```bash
make deploy
```

make deploy は停止・旧 app image 削除・archive load・再起動を行い、無停止更新ではない。別設定は `ENV_FILE=.env.production`、archive 指定は `IMAGE_ARCHIVE=images/projectmind-production.tar`。context path を変える場合は Web image も再 build する。

## 操作の区別

| 操作 | 効果 |
| --- | --- |
| `make` | service 状態 |
| `make stop` | container を保持して停止 |
| `make down` | container/network 削除、永続 volume 保持 |
| `make run` | 配置済み image で起動 |
| `make deploy` | app image を置換し再起動 |
| `python -m projectmind.ops.bootstrap_admin` | 既存データを消さず、首個 ADMIN を一度作成 |
| `make bootstrap-admin` | 全データを消去し初期化 |

対象 task を指定した smoke、Worker 接管、backup/restore、CAS adapter、KEK 運用は [Runbook](runbook.md) を参照する。
