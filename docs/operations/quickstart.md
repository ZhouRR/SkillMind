# 起動と初期管理者

Windows で構築し、Linux では初回・更新とも `make deploy` を使う。配置と設定は[配備手順](deployment.md)、データ復元は[backup](backup-recovery.md)を参照する。

## Compose の前提

Windows は Rancher Desktop（Moby）+ PowerShell + Docker Compose、Linux は Docker Compose v2 + GNU make を使う（宿主 Python/Node は不要）。共有 Traefik と external edge network は既存基盤を利用する。

## Image 移送と更新

Windows の `SKM/` で実行する（再 export は `-Force`）：

```powershell
docker compose build api web
.\scripts\export-images.ps1
```

**images/images.tar、compose.yml、Makefile** を Linux の同一 directory へコピーし、サーバー `.env` を維持する。完全 offline の初回は[基盤 image も同梱](deployment.md#windows-構築と移送)する。

## 初回設定と基盤起動

`.env` が無い初回だけ `.env.example` から作り、host/context path、TLS、Traefik network、DB/ストレージ、KEK/model を設定する。新ストレージには非零 UUID の `SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID` を設定し、更新時は既存値・project 名・データ巻を維持する。

四 file のある Linux directory で実行する：

```bash
make deploy
```

image 導入、基盤と bucket 準備、DB migration、API/Worker/Maintenance/Web 起動を順に行う。更新判断と失敗時の操作は[配備手順](deployment.md#移行前提と実行)を参照する。

## 最初の ADMIN を作成する

初回 deploy 成功後に対話入力する（既存 ADMIN があれば拒否され、password 再設定には使えない）：

```bash
make bootstrap-admin
```

## 最初の業務実行

ADMIN でログインし、Project/メンバー、Integration/資源、Skill の取込・公開を行う。業務配送には `SKILLMIND_WORKER_DISPATCH_ENABLED=true` と[対応する機能設定](deployment.md#移行前提と実行)が必要。

## 操作の区別

`make status`（一覧）、`make logs`（直近 log）、`make logs-follow`（追跡）、`make stop`（基盤を含む停止）、`make down`（container/network 削除、named volume 維持）、`make run`（deploy の別名）。

