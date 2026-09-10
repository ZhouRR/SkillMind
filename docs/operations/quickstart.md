# 起動と初期管理者

初回起動の手順。既存環境の更新と Windows build は[公開・移行](deployment.md)、復元は[backup](backup-recovery.md)、故障は [Runbook](runbook.md)へ。

## Compose の前提

構築側は Windows + Rancher Desktop（Moby）+ PowerShell + Docker Compose。配備側は Linux + Docker Compose v2 + GNU make と通常の Unix 工具を使う。宿主 Python/Node/jq は不要。config JSON / up --wait、対象 CPU architecture と本機 bind mount を実環境で検証する。

共有 Traefik と external edge network は既存基盤を使用し、本工程では配備・host port 公開しない。

## Image 移送と更新

Windows の `SKM/` で初回用の完全 offline release を作る：

```powershell
./scripts/export-images.ps1 -Version 0.1.0-preview1 -Platform linux/amd64 -ContextPath /skillmind -IncludeInfrastructure
```

[構築・転送・checksum 手順](deployment.md#windows-构建与移送)に従い、完成した版 directory 全体を Linux にコピーする。以後はその directory で操作する。通常更新では `-IncludeInfrastructure` を省略できるが、現用基盤 image の version は Compose と一致させる。

## 初回設定と基盤起動

受信 package を[実行前に検証](deployment.md#linux-核验与部署)する。既存環境は ENV_FILE に元の設定の絶対 path を指定し、既存 project 名を保つ。初回で .env が無い場合だけ新設し、既存値を上書きしない：

```bash
umask 077
cp -n .env.example .env
```

host/context path、Traefik、DB/storage password、model を設定し、production は HTTPS を使う。context path は Web build 時の値と一致させる。COMPOSE_PROJECT_NAME は shell/make で選ぶ（dotenv 内の同名値では target を選ばない）。

新 storage 世代の UUID を SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID に設定し、API/Worker と復元清単で共有する。未設定では文書 blob 操作を拒否する。[旧文書の帰属](../design/document-lifecycle.md#存储归属与配置切换)は自動移行しない。

新規環境も target/入口閉鎖/復元手段を確認し、[清単変数](deployment.md#linux-核验与部署)を export してから各段階を実行する。失敗したら進まない：

```bash
make config
make deploy-load
sh scripts/compose.sh up -d --no-build --no-deps --pull never --wait postgres redis object-storage
sh scripts/compose.sh up --no-build --no-deps --pull never --exit-code-from object-storage-init object-storage-init
make deploy-migrate
make deploy-api
```

ここでは Worker は起動しない。preflight は DB/Redis の基線であり、storage/KEK/認証/model の業務保証ではない。既存環境では基盤作成を繰り返さず、[make deploy](deployment.md#linux-核验与部署)へ進む。

## 最初の ADMIN を作成する

migration と API 起動後、volume を消さない CLI を一度実行する：

```bash
make bootstrap-admin
```

email/display name/password を TTY で入力し、既定 Organization に ADMIN と CREATED 安全イベントを追加する。DISABLED を含め既存 ADMIN があれば拒否する。password 再設定・復元・データ再初期化の機能ではない。

## 最初の業務実行

1. ADMIN でログインし、Project とメンバーを用意する。
2. 旧 queue/在途と背景処理を確認し、[Worker 起動](deployment.md#启动与放行)を別途承認する。
3. 実際の Skill を導入・解釈・確認・公開し、Project で精確 SkillVersion を有効化する。
4. 必要な Integration、SecretReference、binding/資源を設定し、Task Center から実行する。
5. 待機、結果、Evidence と Evaluation を検証し、普通入口を人工で開放する。

新 Run/Effect 配送には SKILLMIND_WORKER_DISPATCH_ENABLED=true が必要。既定 false でも既存 job/Schedule/recovery は止まらず、[保守 mode ではない](deployment.md#一个例子关闭-dispatch-后仍有工作)。

## 操作の区別

`make` は状態表示、`make stop` は基盤を含む container を保持して停止、`make down` は container/network を削除して永続 volume を保持する。配備では必要な業務 service だけを停止し、基盤を動かしたまま検査する。`make run` は deploy-api の別名。全データ削除の make target は設けない。
