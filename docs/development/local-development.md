# ローカル開発と検証

> 全体の入口は [PJM README](../../PJM/README.md)。以下の repository root は `PJM/` を指す。

## 前提と設定

[backend/pyproject.toml](../../PJM/backend/pyproject.toml) は Python 3.12、[web/package.json](../../PJM/web/package.json) は Node.js 26 / pnpm 11.7.0 を指定する。対応版を用意し、lockfile を更新せずに依存を導入する。

この作業環境では venv を作らず、Backend 依存を user site に入れる。別環境でも interpreter と依存の保存先を明確にする。README の手順だけで PostgreSQL/Redis/object storage が起動するわけではない。

`.env.example` は Compose の production 雛形。HTTP localhost 開発では `PROJECTMIND_ENVIRONMENT=development`、許可 Origin、接続先、書込可能な Run workspace を設定する。`.env` は作業ディレクトリ基準で読まれるため、`backend/` から実行する場合はそこで使う設定または process environment を明示する。root の `.env` が自動で親から読まれるとは仮定しない。

## Backend

`PJM/backend/` で実行する。

```bash
python3 -m pip install --user -e ".[dev]"
alembic upgrade head
python3 -m uvicorn projectmind.api.main:app --reload --port 8000
```

API の health は `http://localhost:8000/internal/health/live` と `/internal/health/ready`。後者は必要な外部サービスも検査する。DB migration は指定した接続先を変更するので、開発専用 DB を使う。テストにも DB 作成/削除を伴う経路があるため、実行前に[実 PostgreSQL の前提](#実-postgresql-の前提)を確認する。

Worker の起動方法と依存は [backend README](../../PJM/backend/README.md)、本番に近い一括起動は [Compose 起動](../operations/quickstart.md) を参照する。

### 隔离 Redis 登录防护验证

この回帰は既存 Redis の URL を使わず、自分で起動した Redis process と Unix socket だけを操作する。Backend の Python 依存に加え、信頼できる入手元・checksum を確認済みの Redis 8.2 系 executable を用意する。system service の起動や既存 DB の消去は不要である。

`PJM/backend/` で実行する。以下の executable path は placeholder なので、確認済みの絶対 path に置き換える。

```bash
PYTHONDONTWRITEBYTECODE=1 \
  PROJECTMIND_TEST_REDIS_SERVER=/absolute/path/to/redis-server \
  python3 -m pytest -p no:cacheprovider -o addopts= \
  tests/auth/test_login_protection.py \
  tests/auth/test_real_login_protection.py \
  tests/auth/test_real_login_http.py \
  tests/auth/test_auth_service.py \
  tests/auth/test_session_service.py \
  tests/api/test_login_protection_api.py
```

未指定時は PATH の redis-server を探し、無ければ実 Redis test が skip する。明示 path が不正なら fail するため、skip を避ける目的で未確認の executable を渡さない。fixture は TCP port と永続化を無効にし、終了時には自分の process だけを停止する。socket を作れる短い一時 path が必要であり、これは PostgreSQL の作成/削除 fixture とは別の検証である。

共通 fixture は [tests/auth/conftest.py](../../PJM/backend/tests/auth/conftest.py)。実 Lua/TTL と複数 service object の競争、ASGI → AuthService → Redis の拒否、成功時の配額維持を確認する。ただし Session 保存は fake、ASGI は in-process であり network server は起動しない。専用状態の時刻を移動して退避を検査する case も含む。

実 PostgreSQL、数分の実待機、複数 API process、Redis 切替/駆逐や HTTPS proxy は別の検証である。[設計の受入条件](../design/login-protection.md#开发接续与验收)から残る層を選び、通常の API/契約回帰も別に確認する。

### 実 PostgreSQL の前提

全件の pytest は純粋な単体テストだけではない。[migrated_database_url](../../PJM/backend/tests/db/test_real_database_invariants.py) は PROJECTMIND_DATABASE_URL の接続先で管理 DB に接続し、専用 scratch DB の作成、全 migration の適用、終了時の DROP DATABASE WITH (FORCE) を行う。実会話と予算の test もこの fixture を再利用する。

| 実行前の確認 | 条件と停止点 |
| --- | --- |
| 接続先 | 承認済みの隔離 PostgreSQL と専用 account を使う。URL の database 名を変えるだけでは server の隔離にならない |
| 副作用 | scratch DB と必要権限、作成/強制削除の許可を確認する。疎通確認や文書作業のために実行しない |
| 環境不足 | 未接続時の skip は未実施として記録。権限/認証/migration の失敗を skip に書き換えず、起動や権限の安全制限を回避しない |

許可された実 DB が無い場合は確認済みの局部 test を指定する。広い非 PG 回帰が必要なら、`PJM/backend/` で現在の三つの実 DB module を明示的に除外できる：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -o addopts= \
  --ignore=tests/db/test_real_database_invariants.py \
  --ignore=tests/db/test_real_auth_sessions.py \
  --ignore=tests/db/test_real_budget_ledger.py
```

これは実 PostgreSQL を除く選択であり、全 test が無副作用という意味ではない。隔離 Redis などの一時 process は別途確認する。新しい実 DB test を追加したら fixture の利用箇所と除外対象を同期し、未実施の PG 範囲を成功件数に含めない。

## Web

`PJM/web/` で実行する。

```bash
CI=true npx -y pnpm@11.7.0 install --frozen-lockfile
npx -y pnpm@11.7.0 dev
```

Vite は既定で `http://localhost:5173/projectmind/` を提供し、context path 下の API を `http://127.0.0.1:8000` へ proxy する。異なる context path は Web と API の設定を揃える。Cookie/session は同じ browser Origin で利用する。

### ログイン client の局部回帰

`PJM/web/` で既存の依存を使う。これは mock fetch と純粋な文案の検証で、Backend、browser、実 credential を使わない。

```bash
node_modules/.bin/vitest run \
  tests/api/http.test.ts \
  tests/api/authProjects.test.ts \
  tests/api/mutationCsrf.test.ts \
  tests/lib/loginFeedback.test.ts
```

確認するのは Retry-After の保持/不正値、challenge 失敗後の停止、遅い応答と abort、三語の拒否表示。実 LoginPage の同時 submit、離頁、focus/alert、窄屏と cookie はこの command では検証しない。[Web の接続表](../../PJM/web/README.md#ログインと書込失敗を切り分ける)と[提出の境界](../design/login-protection.md#提交离页与结果未知)から実操作の受入へ進む。下記 Workspace 用 browser script をログインの検証として流用しない。

### ログイン画面のブラウザ回帰

[専用 harness](../../PJM/web/tests/browser/login.tsx)は実 LoginPage を StrictMode で描画する。[runner](../../PJM/web/tests/browser/check_login.py)が全 API を mock し、異なる origin を遮断する。使用するのはテスト専用値で、実 account・Backend・PostgreSQL・Redis は使わない。既存の browser 依存を再利用できる。

`PJM/web/` の専用 terminal で Vite を起動し、別 terminal の同じ directory で runner を実行する。未導入の場合のみ、下の Workspace 回帰と共通の requirements/Chromium を導入する。

```bash
node_modules/.bin/vite --host 127.0.0.1 --port 5191 --strictPort
```

```bash
python3 tests/browser/check_login.py \
  --url http://127.0.0.1:5191/projectmind/tests/browser/login.html
```

同じ描画 tick の二重 submit、手動再試行の新 challenge、context 拒否後の停止、native abort と signal を無視する transport の遅い応答、三語 catalog 切替、Tab/Enter と 390/1440px のエラー表示を確認する。fixture の HttpOnly cookie と storage 非保存も確認するが、実 session の作成/撤销や HTTPS の属性は検証しない。

`--output <明示した新しい外部 directory>` で screenshot を保存できる。占有済み port は奪わず別の port と URL を使い、終了時は自分の Vite だけを停止する。App 全体の初回会話チェック、多 tab、実 DB/代理、長時間の Redis 復旧は別途受入する。この runner を実環境 URL へ向けない。

### 原要求確認のブラウザ回帰

この検証は実 Workspace を描画し、全業務 API を mock に置き換える。応答喪失、異常応答、timeout、同じ key/body の再送、明示的な新規実行、actor/Project 切替、refresh、三語とキーボードを確認する。Backend・DB・モデル・実 credential は使わず、通常の `vitest run` には含まれない。

`PJM/web/` でブラウザ検証専用の依存を導入し、専用 Vite を起動する。初回の依存/browser 導入は download を伴う。

```bash
python3 -m pip install --user -r tests/browser/requirements.txt
python3 -m playwright install chromium
node_modules/.bin/vite --host 127.0.0.1 --port 5189 --strictPort
```

別 terminal の `PJM/web/` で実行する。

```bash
python3 tests/browser/check_run_submission.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/run-submission.html
```

`--case translations` で単独の表示/キーボード確認、`--output` に工作区外の明示 path を渡すと screenshot を保存できる。custom context path を使う場合は URL も揃える。user site が書けない環境では外部依存 directory を PYTHONPATH、browser 保存先を PLAYWRIGHT_BROWSERS_PATH で指定し、venv やアプリ依存への追加で回避しない。

古い dev server は工作副本と異なる transform を返すことがある。port が使用中なら既存 process を無断で止めず、別 port で新規起動して URL を合わせる。検証後は自分が起動した Vite だけを終了する。fixture はローカル専用であり、mock の成功を実 DB の唯一制約・transaction・認証や全画面の受入証拠としない。

### 文書範囲と調度入力のブラウザ回帰

同じ専用 Vite と browser 依存を使い、`PJM/web/` で実行する。Backend の依存導入も必要だが、使うのは凍結文書の純 parser だけであり、Backend server/DB/モデルは起動しない。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=../backend/src \
  python3 tests/browser/check_document_sources.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/run-submission.html
```

外部に置いた Python 依存を使う場合は、既存の依存 directory も PYTHONPATH に追加する。単一/集合/全集/任意未選択を即時実行・Schedule 作成の双方で送り、実際の input/sources、CSRF、三語、keyboard、390px 幅、Run detail の再読込を検証する。現在の catalog から候補を消しても凍結表示が変わらないことを確認するが、mock であるため実 blob の削除や Worker の物化は検証していない。

`--output` は明示した directory に screenshot を保存する。5000 件の表示性能、調度編集、異なる時区の入力/表示、認領後 crash は別の検証であり、この script の成功へ含めない。残る実装と受入は[計画](../planning/roadmap.md#13-当前执行状态)を確認する。

## 変更に応じた検証

| 対象 | 実行位置 | コマンド |
| --- | --- | --- |
| Backend | `PJM/backend/` | `python3 -m ruff check .`、`python3 -m mypy src`、`python3 -m pytest` |
| Web | `PJM/web/` | `node_modules/.bin/tsc -b --pretty false`、`node_modules/.bin/vitest run`、`node_modules/.bin/vite build` |
| 契約 | `PJM/` | `python3 scripts/validate_contracts.py` |
| Compose 静的規約 | `PJM/` | `python3 scripts/validate_compose.py` |
| SDK offline probe | `PJM/` | `PYTHONPATH=backend/src python3 scripts/probe_claude_agent_sdk.py` |
| Compose 実設定 | `PJM/` | `docker compose --env-file .env config --quiet` |
| 文書 | `PJM/` | [文書更新手順](documentation.md)の build/check |

API 契約テストの in-memory fake と、実 PostgreSQL invariant は別の検証である。上表の pytest 全件は[DB 操作の前提](#実-postgresql-の前提)を満たす場合に選ぶ。PostgreSQL 未到達時の skip を成功に数えない。Docker がない環境で Compose の実起動/復旧を確認したとは報告しない。変更に無関係な全テストを繰り返す必要はない。

Compose の通常 `config` 出力には展開した Secret が含まれ得るため、構成検査は `--quiet` を使う。現行 `ENV_FILE` と Backend の `env_file` は同一の切替ではない。[設定境界](../operations/deployment.md#环境文件与配置边界)を確認し、二つの環境 file を混在させない。

## Skill Interpreter の検証

Backend 依存を入れた後、`PJM/` で model を使わない fixture runner を実行できる。

```bash
projectmind-skill-interpret-fixture \
  skills/examples/repository-review \
  contracts/examples/skill-interpreter-response.v1.json
```

本番解釈は source、system Skill、Schema、catalog、model、parameter が同一なら既存 execution を再利用する。別候補は明示的に `force_regenerate=true` とする。

構造化出力を既定で必須にする。互換 endpoint 用に `PROJECTMIND_SKILL_INTERPRETER_ACCEPT_PROMPT_JSON=true` を明示した場合のみ、本文 JSON を候補として受け取り、同じ Schema/identity/修復検査へ通す。自由文を検証済み Manifest として扱う設定ではない。

モデル反復測定は実際の API 利用・課金を伴い、fixture runner と分けて実行する。

```bash
python3 scripts/measure_skill_interpretations.py \
  skills/examples/jaf-ticket-quality \
  skills/examples/repository-review \
  --repeats 3 --timeout-seconds 900
```

コマンドは構造有効率、意味 signature、field path、修復と review item 数を出す。基準は構造有効率 1.0 と意味安定性、その後の人手による規則保真・証拠・調整量の評価。過去の model timeout 記録を現在の品質結論として再利用しない。

## 作業後の一時物

今回の検証で生成した cache、`__pycache__`、`web/dist`、`node_modules/.tmp`、egg-info を確認して片付ける。既存のユーザー成果物を一括削除せず、node_modules 本体は残す。削除の前に対象を具体的に確定する。
