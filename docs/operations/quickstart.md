# 起動と初期管理者

Windows で構築し、Linux では初回・更新とも `make deploy` を使う。詳しい配置・失敗時の確認は[配備手順](deployment.md)、データの保全・復元は[backup](backup-recovery.md)へ。

## Compose の前提

Windows/Rancher（Moby）+ PowerShell + Docker Compose、Linux + Docker Compose v2 + GNU make を使う。宿主 Python/Node は不要。共有 Traefik と external edge network は既存基盤を利用する。

## Image 移送と更新

Windows の SKM/ で実行する：

```powershell
docker compose build
.\scripts\export-images.ps1
```

必要な image だけなら `docker compose build api` / `docker compose build web`。再 export は `-Force` を付ける。**images/images.tar、compose.yml、Makefile** を Linux の同一 directory へコピーし、サーバー .env は別に用意・維持する。archive は backup ではない。

サーバーに基盤 image がなければ配備時に取得する。完全 offline の初回には Windows で事前 pull し、[基盤も同梱](deployment.md#windows-构建与移送)する。

## 初回設定と基盤起動

.env が無い初回だけソースの .env.example を雛形に作る。host/context path、TLS、Traefik network、DB/ストレージ password、KEK/model を設定する。Web context path は構築時と一致させ、production は HTTPS を使う。

新ストレージには SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID を新 UUID として設定する。[旧文書の帰属](../design/document-lifecycle.md#存储归属与配置切换)は自動移行しない。既存 project 名やデータ卷を意図せず変更しない。

四 file のある Linux directory で：

```bash
make deploy
```

image 導入、基盤起動、bucket 作成、DB migration、API/Web と **Worker の起動**を順に行う。追加 script・release manifest・手動批准変数は不要。失敗したらその stage の原 error を確認し、続行/再実行はしない。更新前の backup・全 writer 停止/在途確認は[配備手順](deployment.md#迁移前置与执行)に従う。

## 最初の ADMIN を作成する

初回 deploy 成功後、email/display name/password を対話入力する：

```bash
make bootstrap-admin
```

既存 ADMIN があれば拒否する。password 再設定や data 再初期化は行わない。

## 最初の業務実行

ADMIN でログインし、Project/メンバー、実 Skill の導入・説明確認・公開、必要な Integration/資源を設定する。手動 Run、結果/根拠、人工評価を検証してから普通利用を開く。

Worker は deploy で起動する。新 Run/Effect 配送には SKILLMIND_WORKER_DISPATCH_ENABLED=true が必要だが、false でも旧 job/cron は止まらない。実モデルや外部書込の検証は承認済み入力・環境に限定する。

## 操作の区別

`make status` は一覧、`make logs` は直近 log、`make stop` は基盤を含む停止、`make down` は container/network の削除（named volume は維持）。`make run` は deploy の別名。deploy に volume 削除・DB rollback・password 上書きは含めない。
