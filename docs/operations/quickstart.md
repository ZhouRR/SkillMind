# 起動と初期管理者

Windows で構築し、Linux では初回・更新とも `make deploy` を使う。配置と設定は[配備手順](deployment.md)、データ復元は[backup](backup-recovery.md)を参照する。

## Compose の前提

Windows は Rancher Desktop（Moby）+ PowerShell + Docker Compose、Linux は Docker Compose v2 + GNU make を使う。宿主 Python/Node は不要。共有 Traefik と external edge network は既存基盤を利用する。

## Image 移送と更新

Windows の `SKM/` で実行する：

```powershell
docker compose build api web
.\scripts\export-images.ps1
```

再 export は `-Force` を付ける。**images/images.tar、compose.yml、Makefile** を Linux の同一 directory へコピーし、サーバー `.env` を維持する。完全 offline の初回は[基盤 image も同梱](deployment.md#windows-构建与移送)する。

## 初回設定と基盤起動

`.env` が無い初回だけ `.env.example` から作り、host/context path、TLS、Traefik network、DB/ストレージ、KEK/model を設定する。Web context path は構築時と一致させ、production は HTTPS を使う。新ストレージには非零 UUID の `SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID` を設定し、更新時は既存値・project 名・データ卷を維持する。

四 file のある Linux directory で：

```bash
make deploy
```

image 導入、基盤と bucket の準備、DB migration、API/Worker/Maintenance/Web 起動を順に行う。失敗時は表示された stage と原 error を確認する。更新の判断と失敗時の操作は[配備手順](deployment.md#迁移前置与执行)を参照する。

## 最初の ADMIN を作成する

初回 deploy 成功後、email/display name/password を対話入力する：

```bash
make bootstrap-admin
```

既存 ADMIN があれば拒否する。password 再設定や data 再初期化には使わない。

## 最初の業務実行

ADMIN でログインし、Project/メンバー、Integration/資源、Skill の導入・説明確認・公開を行う。業務配送には `SKILLMIND_WORKER_DISPATCH_ENABLED=true`、使用能力には[対応する機能設定](deployment.md#迁移前置与执行)が必要。承認済み環境と入力で実行し、成果・根拠・保存先を確認する。実モデルや外部書込は計費・永続化を伴う。

## 操作の区別

`make status` は一覧、`make logs` は直近 log、`make logs-follow` は追跡、`make stop` は基盤を含む停止、`make down` は container/network の削除（named volume は維持）。`make run` は deploy の別名。通常操作で volume を削除しない。
