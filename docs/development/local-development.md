# ローカル開発と検証

全体の入口は [PJM README](../../PJM/README.md)。以下はコード root `PJM/` 起点で、各節に実行 directory を明示する。

## 前提と設定

Python 3.12、Node.js 26 / pnpm 11.7.0 を用意する。venv は作らず Backend 依存を user site に置き、lockfile を手編集しない。導入だけでは DB/Redis/object storage は起動しない。

`.env.example` は production 雛形。HTTP localhost は `PROJECTMIND_ENVIRONMENT=development`、許可 Origin、専用接続先、書込可能な Run workspace を設定する。`.env` は実行 directory 基準なので、`backend/` から親の file が自動で読まれると仮定せず、設定 file または process environment を明示する。Compose の `ENV_FILE` は別の[設定境界](../operations/deployment.md#环境文件与配置边界)を持つ。

## Backend

`PJM/backend/` で依存を入れる。migration は確認済み開発 DB を変更する操作なので、対象と権限を確認してから進む。

```bash
python3 -m pip install --user -e ".[dev]"
alembic upgrade head
python3 -m uvicorn projectmind.api.main:app --reload --port 8000
```

health は `http://localhost:8000/internal/health/live` と `/internal/health/ready`（外部 service も検査）。Worker は [Backend 入口](../../PJM/README.md#backend)、一括起動は[Quickstart](../operations/quickstart.md)を参照する。

### 実 PostgreSQL の前提

全 pytest には[実 DB fixture](../../PJM/backend/tests/db/test_real_database_invariants.py)が含まれる。`PROJECTMIND_DATABASE_URL` の server 上で scratch DB を作り、全 migration 後に DROP DATABASE WITH (FORCE) を行う。実会話・予算 test も再利用する。

承認済み隔離 server/account、作成・強制削除権限を確認する。URL の DB 名変更だけでは隔離にならない。文書確認のために実行せず、接続不能の skip は未実施、認証/権限/migration の失敗は失敗のまま報告する。

許可された DB がない場合は局部 test、または `PJM/backend/` で現在の実 DB module を除外する：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -o addopts= \
  --ignore=tests/db/test_real_database_invariants.py \
  --ignore=tests/db/test_real_auth_sessions.py \
  --ignore=tests/db/test_real_budget_ledger.py
```

これは無副作用の保証ではない。新しい fixture 利用先を追加したら除外対象を同期し、一時 Redis 等の process は別に確認する。

### 隔离 Redis 登录防护验证

信頼できる入手元と checksum を確認した Redis 8.2 系 executable が必要。fixture は TCP/永続化を無効にした自分専用 process と短い path の Unix socket を使い、終了時も自分の process だけを止める。既存 Redis の URL、system service や DB 消去は使わない。

`PJM/backend/` で placeholder を確認済み絶対 path に置き換える：

```bash
PYTHONDONTWRITEBYTECODE=1 \
  PROJECTMIND_TEST_REDIS_SERVER=/absolute/path/to/redis-server \
  python3 -m pytest -p no:cacheprovider -o addopts= \
  tests/auth/test_real_login_protection.py tests/auth/test_real_login_http.py
```

未指定なら PATH を検索し、見つからなければ skip、不正な明示 path は fail。実 Lua/TTL と ASGI→AuthService→Redis を検証するが Session 保存は fake、一部時刻は操作する。実 PostgreSQL、長時間待機、複数 API process、HTTPS proxy と Redis 切替は別の[受入条件](../design/login-protection.md#开发接续与验收)。

## Web

`PJM/web/` で実行する：

```bash
CI=true npx -y pnpm@11.7.0 install --frozen-lockfile
npx -y pnpm@11.7.0 dev
```

既定 URL は `http://localhost:5173/projectmind/`、API proxy は `http://127.0.0.1:8000`。context path は Web/API で合わせ、cookie は同じ browser Origin で使う。

### ブラウザ回帰

全 API を mock する専用 harness。実 credential、Backend、DB、model は使わず、通常の vitest に含まれない。runner を実環境 URL に向けない。初回だけ `PJM/web/` で browser 依存を導入し、専用 terminal で Vite を起動する：

```bash
python3 -m pip install --user -r tests/browser/requirements.txt
python3 -m playwright install chromium
node_modules/.bin/vite --host 127.0.0.1 --port 5189 --strictPort
```

別 terminal の同じ directory で目的の runner を選ぶ：

```bash
python3 tests/browser/check_login.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/login.html
python3 tests/browser/check_run_submission.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/run-submission.html
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=../backend/src \
  python3 tests/browser/check_document_sources.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/run-submission.html
```

| runner | 検証する範囲と限界 |
| --- | --- |
| login | 実 LoginPage の二重 submit、拒否・abort・遅延応答、三語/keyboard/窄屏。実 session、HTTPS、多 tab は別 |
| run_submission | 実 Workspace の応答喪失、同 key/body 確認、明示的新規、actor/Project 切替・refresh。実 transaction/唯一制約は別 |
| document_sources | 即時/調度入力、清単、CSRF と凍結表示。Backend 依存と純 parser は使うが、実 blob、物化、調度編集/時区/認領 crash は別 |

初回導入は download を伴う。外部依存は PYTHONPATH、browser は PLAYWRIGHT_BROWSERS_PATH で指定でき、document_sources では Backend src と両方を含める。`--output` は明示した新しい工作区外 directory に screenshot を保存する。port が使用中なら奪わず別 port と URL を使い、終了時は自分の Vite だけを停止する。

## 変更に応じた検証

| 対象 | 実行位置とコマンド |
| --- | --- |
| Backend | `PJM/backend/`：`python3 -m ruff check .`、`python3 -m mypy src`、上記副作用確認後の pytest |
| Web | `PJM/web/`：`node_modules/.bin/tsc -b --pretty false`、`node_modules/.bin/vitest run`、`node_modules/.bin/vite build` |
| 契約 | `PJM/`：`python3 scripts/validate_contracts.py`。快照一致性は[契約 workflow](contract-workflow.md#遇到未接齐的交付链) |
| Compose | `PJM/`：`python3 scripts/validate_compose.py`、Docker 利用可なら `docker compose --env-file .env config --quiet` |
| SDK offline | `PJM/`：`PYTHONPATH=backend/src python3 scripts/probe_claude_agent_sdk.py` |
| 文書 | `PJM/`：[文書維持](documentation.md)の build/check・回帰 |

通常の config 出力には Secret が含まれ得る。実 DB skip、mock と実環境、静的検査と配備を区別し、Docker がなければ実起動/復旧は未実施と報告する。

## Skill Interpreter の検証

Backend 依存導入後、`PJM/` で model 不使用の fixture runner：

```bash
projectmind-skill-interpret-fixture \
  skills/examples/repository-review \
  contracts/examples/skill-interpreter-response.v1.json
```

本番解釈は source/system Skill/Schema/catalog/model/parameter が同一なら再利用し、別候補だけ明示的に force_regenerate する。構造化出力を原則必須とし、`PROJECTMIND_SKILL_INTERPRETER_ACCEPT_PROMPT_JSON=true` も同じ Schema/identity/修復検査を省略しない。

以下は実 model API と課金を伴う。承認済み環境、入力と model 設定を確認してから実行する：

```bash
python3 scripts/measure_skill_interpretations.py \
  skills/examples/repository-review --repeats 3 --timeout-seconds 900
```

構造有効率と意味 signature を測り、その後に人手で規則保真・証拠・修正量を評価する。fixture や過去の timeout を現在の業務品質の証明にしない。

## 作業後の一時物

今回生成した cache、__pycache__、web/dist、node_modules/.tmp、egg-info だけを対象確認後に片付ける。既存成果物と node_modules 本体は残す。
