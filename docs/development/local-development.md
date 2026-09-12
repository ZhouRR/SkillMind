# ローカル開発と検証

全体の入口は [SKM README](../../SKM/README.md)。コマンドの実行 directory は各節に示す。

## 前提と設定

Python 3.12、Node.js 26 / pnpm 11.7.0 を使用する。venv は作らず、Backend 依存は user site、lockfile は手編集しない。依存導入と外部 service 起動は別である。

`.env.example` は production 雛形。HTTP localhost では development、許可 Origin、専用接続先、書込可能な Run workspace を設定する。
Backend の .env は実行 directory 基準なので file または process environment を明示する。Compose の配備は[共通設定入口](../operations/deployment.md#环境文件与配置边界)で ENV_FILE を固定する。通常の image build は SKM/ で `docker compose build api web`（既定 .env）；[構築時の設定範囲](../operations/deployment.md#windows-构建与移送)を参照する。

## Backend

`SKM/backend/` で実行する。migration は DB を変更するため、開発用の対象と権限を先に確認する。API と Worker は別 terminal で起動する。

```bash
python3 -m pip install --user -e ".[dev]"
alembic upgrade head
python3 -m uvicorn skillmind.api.main:app --reload --port 8000
```

```bash
arq skillmind.worker.settings.WorkerSettings
```

health は `http://localhost:8000/internal/health/live` と `/internal/health/ready`（外部 service も検査）。一括起動は[Quickstart](../operations/quickstart.md)へ。

### 実 PostgreSQL の前提

全 pytest は[実 DB fixture](../../SKM/backend/tests/db/test_real_database_invariants.py)を含む。
SKILLMIND_DATABASE_URL の **server 上で scratch DB を作り、migration 後に DROP DATABASE WITH (FORCE)** を行う。URL の DB 名変更だけでは隔離にならない。

承認済み隔離 server/account と作成・強制削除権限が必要。文書確認のためには実行しない。
接続不能による skip は未実施、認証/権限/migration の失敗は失敗として報告する。

許可された DB がなければ明示的な局部 test を選ぶか、`SKM/backend/` で実 DB module を除外する：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest --import-mode=importlib -p no:cacheprovider -o addopts= \
  --ignore=tests/db/test_real_database_invariants.py \
  --ignore=tests/db/test_real_auth_sessions.py \
  --ignore=tests/db/test_real_budget_ledger.py
```

新しい fixture 利用先も確認する。除外は全 test の無副作用保証ではなく、Redis 等の process は別に確認する。

### PostgreSQL 写入的隔离 SQL 探测

[probe_postgres_write.py](../../SKM/scripts/probe_postgres_write.py) は PGlite のメモリ DB だけに合成 table/role を作り、本番の SQL 生成・一行変更・回执保存処理を実行する。実 DB 接続・既存 Project への書込は行わない。INSERT/UPDATE、JSON/日時、generated 列、原回执、原状態競合、取消前の失権 rollback と回执 role の制限を確認する。PGlite/stdio adapter は asyncpg の実接続や多 Worker の競争を証明しない。

通常 Backend 依存に PGlite は含めない。Node.js と Backend 依存がある環境で、専用の外部 package directory へインストールし、`SKM/` から実行する：

```bash
npm install --prefix /tmp/skillmind-pglite --cache /tmp/skillmind-npm-cache --no-audit --no-fund @electric-sql/pglite@0.5.8
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend/src python3 scripts/probe_postgres_write.py \
  --pglite-root /tmp/skillmind-pglite
```

Backend 依存を外置した環境ではその path も PYTHONPATH に加える。実 PostgreSQL の検証は前節の承認済み server/account で別途行う。

同じ probe に `--rv-reviewer` を付けると、[RV 業務 DDL](../../SKM/scripts/sql/rv-reviewer-schema.sql) と[列権限](../../SKM/scripts/sql/rv-reviewer-grants.sql)もメモリ DB に適用する。空 table の列/複合主キー/生成列を本番の構造 query で取得し、本番の単行 writer で RUNNING 登録、文書/成果、PASS/FAIL、RV 後の異常と NO_TARGETS、原回执を確認し、結果/ID の不一致や権限外変更を拒否する。MinIO、Excel 変換、model、実会話は使用しない。

実環境の初期化は対象と独立 owner が確定してから行う。`psql --set=ON_ERROR_STOP=on` で業務 DDL、既存の回执 DDL、列権限の順に適用し、最後の script には `--set=rv_writer=<既存実行 role>` を指定する。接続情報は既存の安全な psql 設定で渡し、password を command に書かない。script は login を作らず、既存 schema では停止する。列権限の付与は既存の強い権限を除去しないため、専用の非 owner role を使う。リソースには三つの `test_automation` table、INSERT/UPDATE と明示列を設定する。列名は権限 script の INSERT 欄を `schema.table.column` にしたものとし、generated の `spec_status` や server 既定の成果作成時刻を含めない。成果表 UPDATE は DB 権限で拒否する。

### 隔离 Redis 登录防护验证

信頼できる入手元/checksum を確認した Redis 8.2 系を使う。fixture は TCP/永続化なしの専用 process・Unix socket を作り、自分の process だけを停止する。既存 Redis や system service は使わない。

`SKM/backend/` で placeholder を確認済み絶対 path に置き換える：

```bash
PYTHONDONTWRITEBYTECODE=1 \
  SKILLMIND_TEST_REDIS_SERVER=/absolute/path/to/redis-server \
  python3 -m pytest --import-mode=importlib -p no:cacheprovider -o addopts= \
  tests/auth/test_real_login_protection.py tests/auth/test_real_login_http.py
```

未指定なら PATH 検索、未発見は skip、不正な明示 path は fail。実 Lua/TTL と ASGI→AuthService→Redis を検査するが Session 保存は fake。
実 DB、長時間待機、複数 API、HTTPS/Redis 切替は[別の受入条件](../design/login-protection.md#开发接续与验收)。

## Web

`SKM/web/` で実行する：

```bash
CI=true npx -y pnpm@11.7.0 install --frozen-lockfile
npx -y pnpm@11.7.0 dev
```

既定 URL は `http://localhost:5173/skillmind/`、API proxy は `http://127.0.0.1:8000`。Web/API の context path と browser Origin を合わせる。

### ブラウザ回帰

専用 harness は実 Component + mock API を検証し、通常の Vitest には含まれない。
実 credential/Backend/DB/model は使わず、runner を実環境 URL に向けない。

初回だけ `SKM/web/` で依存を導入し、専用 terminal で Vite を起動する：

```bash
python3 -m pip install --user -r tests/browser/requirements.txt
python3 -m playwright install chromium
node_modules/.bin/vite --host 127.0.0.1 --port 5189 --strictPort
```

別 terminal の同 directory で `tests/browser/check_<runner>.py` を選ぶ。例：

```bash
python3 tests/browser/check_projects.py \
  --url http://127.0.0.1:5189/skillmind/tests/browser/projects.html
```

| runner | harness HTML | 主な対象 |
| --- | --- | --- |
| login / accounts | login.html / accounts.html | ログイン防重、本人安全、ADMIN 管理 |
| visual_style | projects.html | 全ページの両テーマ、PC 1366/1440/1920px 優先・三語/390px 補助、contrast、保存/別 tab/入力保持と Login の storage 拒否（`--output` 必須） |
| reading | projects.html | 概览/結果の PC 三尺寸・三語、首画面の本文、証拠/検証 drawer、評価草稿保持と focus、手機補助（`--output` 必須） |
| projects / project_members / project_management | projects.html | 精確 Project、成員、CRUD、版衝突 |
| run_submission / interaction_responses | run-submission.html | 原 key/内容の確認、答復、期限 |
| result_references / artifacts / evaluation_submissions | projects.html | 結果範囲、原 byte download、評価回执/履歴 |
| document_sources / document_library / schedule_times | run-submission.html | 入力清単と成果保存先の区別、時区/DST、preview |
| document_batches / resource_connections | projects.html | 文書一括削除・指定 directory upload、PostgreSQL/MCP 接続 form（`--output` 必須） |
| document_management / document_preview / document_upload | projects.html | 削除未知、静的隔離、有界 upload |
| document_upload_receipts / document_upload_closures | projects.html | 原 key 確認、batch pause、明示停止 |
| interpretation_requests | projects.html | 解釈の原 UUID、応答喪失/SSE 切断、只読確認と刷新後の結果復元（`--output` 必須） |
| task_flow / schedule_management | projects.html | 読取専用 Flow、調度編集/未知/在途投影 |

各 runner の遅延・切替・防重・三語/keyboard/狭幅の範囲は test を参照する。
mock browser は実 DB の CAS/rollback/撤権、blob/PUT/清理、model/外部 write、複数 Worker や HTTPS の証明ではない。

初回導入は download を伴う。外置依存は PYTHONPATH、browser は PLAYWRIGHT_BROWSERS_PATH を使う。
document_sources は Backend src と依存も必要。`--output` は新しい外部 directory を指定する。使用中の port は奪わず変更し、終了時は自分の Vite だけを止める。

Task Flow の数値検証は合成 fixture を実 Backend projector/route/serializer に通した原 byte を使う。
`SKM/web/` で Backend 依存を利用可能にし、途中で JS の JSON parse/stringify を挟まない：

```bash
task_flow_wire=$(mktemp /tmp/skillmind-flow-wire.XXXXXXXX.json)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=../backend/src:../backend \
  python3 -c 'import sys; from tests.skills.task_flow_numeric_fixtures import numeric_flow_wire; sys.stdout.buffer.write(numeric_flow_wire())' > "$task_flow_wire"
python3 tests/browser/check_task_flow.py \
  --url http://127.0.0.1:5189/skillmind/tests/browser/projects.html \
  --numeric-wire "$task_flow_wire"
```

対象は両原契約の大整数/浮動小数/負零、再帰表示、型/重複/逆転範囲。`--numeric-wire` 省略時は当該 case を実行せず runner が報告する。

## 変更に応じた検証

まず変更箇所の test を選び、共有契約・認証・transaction・共通 UI に影響する場合は関連する消費側へ広げる。下表は選択肢であり、毎回全部を実行する checklist ではない。広範な変更やリリース候補では全体回帰を行い、実 DB・モデル・配備の証拠は別に取る。

| 対象 | 実行位置と選択基準 |
| --- | --- |
| Backend | SKM/backend/：対象 file の Ruff と pytest、型の変更は `python3 -m mypy src`。全体検査は `python3 -m ruff check .` と全 pytest（先に副作用を確認） |
| Web | SKM/web/：`node_modules/.bin/tsc -b --pretty false` と対象の Vitest。画面変更は該当 harness の PC・両テーマ、共通スタイル/ナビ変更は全画面。bundle/依存/配備変更は `node_modules/.bin/vite build` |
| 契約 | SKM/：`python3 scripts/validate_contracts.py`。[OpenAPI 一致性](contract-workflow.md#遇到未接齐的交付链)も確認 |
| Compose / 工具 | SKM/：変更した工具の unittest。Compose 変更は `python3 scripts/validate_compose.py`、配備工具を横断する変更は `python3 -m unittest discover -s scripts/tests -v` |
| SDK 接続/更新 | SKM/：`PYTHONPATH=backend/src python3 scripts/probe_claude_agent_sdk.py` と対象 Adapter test（offline） |
| CLI 計量/中断清理 | Linux の SKM/：`PYTHONPATH=backend/src python3 scripts/probe_claude_metering.py`。実随包 CLI + 合成 loopback API；実モデル不使用、`--output` は任意の観測 JSON 保存先 |
| 文書 | SKM/：[build/check と閲覧検証](documentation.md) |

CLI 計量 probe は親の資格情報を継承せず、一時 config/cwd と固定合成応答を使う。[非必須通信の無効化](https://code.claude.com/docs/en/env-vars)と loopback の拒否 proxy を設定するが、OS network 隔離の証明ではない。実 model 課金や停止の証拠と区別し、[観測範囲](../design/run-budgets.md#实-cli-合成-api-探针)を確認する。

配備工具 test は実 GNU make と合成 file/fake Docker を使い、四 file 配備の順序・失敗停止・Worker 起動を検査する。PowerShell 実行回帰は SKM_TEST_PWSH に実行 file を指定する（未指定なら skip）。archive export/Force の検査も Windows/Rancher・実 image の証拠ではない。
実環境の `make config` は Docker と確認済み対象が必要で、通常 config 出力には Secret が含まれ得る。
mock/実環境、成功/skip/失敗を分け、Docker・Make・PowerShell 等がなければ該当する実起動・復旧・Windows 操作は未検証と報告する。

## Skill Interpreter の検証

Backend 依存導入後、`SKM/` で model 不使用の fixture runner：

```bash
skillmind-skill-interpret-fixture \
  backend/tests/fixtures/skills/repository-review \
  contracts/examples/skill-interpreter-response.v1.json
```

再利用と構造化出力の条件は[Interpreter 設計](../design/skill-interpretation.md)が正本。prompt JSON fallback でも Schema/identity/修復検査を省略しない。

次は実 model API と課金を伴うため、承認済み環境・入力・設定を確認してから実行する：

```bash
python3 scripts/measure_skill_interpretations.py \
  /absolute/path/to/your-skill --repeats 3 --timeout-seconds 900
```

構造有効率・意味 signature に加え、人手で規則保真・証拠・修正量を評価する。fixture 成功を model 品質と扱わない。

## 作業後の一時物

今回生成した cache、__pycache__、web/dist、node_modules/.tmp、egg-info だけを対象確認後に片付ける。既存成果物と node_modules 本体は残す。
