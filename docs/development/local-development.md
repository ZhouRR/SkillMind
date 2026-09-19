# ローカル開発と検証

全体の入口は [SKM README](../../SKM/README.md)。実行 directory を各節に示す。検証は今回の変更に必要な範囲を選び、mock と実環境の証拠を区別する。

## 前提と設定

Python 3.12、Node.js 26 / pnpm 11.7.0 を使用する。Backend 依存は user site に導入し、lockfile は手編集しない。
`.env.example` は production 雛形。localhost では development、許可 Origin、専用接続先と書込可能な Run workspace を設定する。Backend の `.env` は実行 directory 基準。Compose は[設定入口](../operations/deployment.md#环境文件与配置边界)の ENV_FILE を使う。

## Backend

`SKM/backend/` で実行する。migration は対象 DB を変更するため開発用接続を確認する。API と各 Worker は別 terminal で起動する。

```bash
python3 -m pip install --user -e ".[dev]"
alembic upgrade head
python3 -m uvicorn skillmind.api.main:app --reload --port 8000
```

```bash
arq skillmind.worker.settings.WorkerSettings
```

```bash
arq skillmind.worker.settings.MaintenanceWorkerSettings
```

health は `http://localhost:8000/internal/health/live` と `/internal/health/ready`。後者は依存 service も確認する。Compose 起動は[Quickstart](../operations/quickstart.md)を使う。

### 実 PostgreSQL の前提

全 pytest は[実 DB fixture](../../SKM/backend/tests/db/test_real_database_invariants.py)を含む。SKILLMIND_DATABASE_URL の **server 上で scratch DB を作り、migration 後に DROP DATABASE WITH (FORCE)** を行う。URL の DB 名変更だけでは隔離にならない。
対象 server/account と作成・削除権限を確認する。実 DB を使わない場合は対象 test を指定するか、次で既知の実 DB module を除外する。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest --import-mode=importlib -p no:cacheprovider -o addopts= \
  --ignore=tests/db/test_real_database_invariants.py \
  --ignore=tests/db/test_real_auth_sessions.py \
  --ignore=tests/db/test_real_api_keys.py \
  --ignore=tests/db/test_real_budget_ledger.py
```

新たな fixture 利用先や Redis 等の process も確認する。接続不能による skip は未実施、認証/権限/migration エラーは失敗として報告する。

## Web

`SKM/web/` で実行する：

```bash
CI=true npx -y pnpm@11.7.0 install --frozen-lockfile
npx -y pnpm@11.7.0 dev
```

既定 URL は `http://localhost:5173/skillmind/`、API proxy は `http://127.0.0.1:8000`。context path と browser Origin を合わせる。

### ブラウザ回帰

専用 harness は実 Component + mock API を使う。実 credential/Backend/DB/model を使わず、runner を配備先 URL に向けない。`SKM/web/` で初回依存導入後、専用 terminal の Vite を起動する：

```bash
python3 -m pip install --user -r tests/browser/requirements.txt
python3 -m playwright install chromium
node_modules/.bin/vite --host 127.0.0.1 --port 5189 --strictPort
```

別 terminal で対象 runner の `--help` を確認して実行する。例：

```bash
python3 tests/browser/check_projects.py \
  --url http://127.0.0.1:5189/skillmind/tests/browser/projects.html
```

| 対象 | 主な runner（check_*.py） |
| --- | --- |
| 全体の見た目・読みやすさ | visual_style、reading |
| Project・アカウント | projects、project_members、project_management、login、accounts |
| 提出・待機・履歴 | run_submission、interaction_responses、run_history、evaluation_submissions |
| レポート・証拠 | report_reading、workspace_reports、result_references、artifacts |
| 文書・directory | document_organization、document_management、document_preview、document_upload、document_batches |
| 接続・Skill・調度 | resource_editing、resource_connections、interpretation_requests、task_schedules、schedule_times |

全 runner は [tests/browser](../../SKM/web/tests/browser/) にある。harness URL と `--output` 等の必須引数は各 runner を参照する。主に projects.html、提出系は run-submission.html を使う。
外置依存は PYTHONPATH、browser は PLAYWRIGHT_BROWSERS_PATH を指定する。使用中 port を奪わず、終了時は自分の Vite だけを止める。mock は実 DB の競合/撤権、blob、外部 write や複数 Worker の証明ではない。

## 変更に応じた検証

| 対象 | 実行位置と選択基準 |
| --- | --- |
| Backend | SKM/backend/：対象 Ruff/pytest。型変更は `python3 -m mypy src`、広範囲変更は関連 module に拡大 |
| Web | SKM/web/：`node_modules/.bin/tsc -b --pretty false` と対象 Vitest。共有 UI 変更は双テーマ・三語・PC/狭幅の browser 回帰。bundle/依存変更は `node_modules/.bin/vite build` |
| 契約 | SKM/：`python3 scripts/validate_contracts.py` と [OpenAPI 一致性](contract-workflow.md#遇到未接齐的交付链) |
| Compose / 工具 | SKM/：`python3 scripts/validate_compose.py`、対象 unittest。横断変更は `python3 -m unittest discover -s scripts/tests -v` |
| 文書 | [文書 build/check と閲覧検証](documentation.md) |

全体回帰は広範囲変更やリリース候補で行う。実 Docker/Make/PowerShell がない場合は該当する起動・更新が未検証と明記する。配備工具の fake Docker 成功は実 image の証拠にならない。

## 必要な場合の隔離 probe

[スクリプト](../../SKM/scripts/)の `--help` と先頭説明で前提・引数を確認してから実行する。既存の接続先や `.env` を試験環境の代用にしない。

| 対象 | 入口と検証範囲 |
| --- | --- |
| PostgreSQL read/write | probe_postgres_read.py / probe_postgres_write.py：外置 PGlite のメモリ DB。SQL/一行更新/原回执/rollback を確認、asyncpg 実接続や分散競争は別 |
| Worker Queue | probe_worker_queues.py：専用の空 Redis Unix socket。保守中の業務 dispatch/続行を確認、DB/model は合成 |
| ログイン防護 | tests/auth/test_real_login_protection.py と test_real_login_http.py：専用 Redis process。SKILLMIND_TEST_REDIS_SERVER で実行 file を指定、Session 保存は fake |
| SDK / CLI | probe_claude_agent_sdk.py / probe_claude_metering.py と Adapter test：offline または loopback 合成 API。実モデルの課金や停止は証明しない |
| 原 SDK 提案続行 | tests/agent/test_proposal_resume_native.py：SKILLMIND_NATIVE_CLI_TESTS=1 で固定 CLI と合成応答を検証 |

Skill 解釈の合成検証は `SKM/` で次を使う：

```bash
skillmind-skill-interpret-fixture \
  backend/tests/fixtures/skills/repository-review \
  contracts/examples/skill-interpreter-response.v1.json
```

実モデル品質は同じ Skill・設定・独立期待値で別に評価する。`scripts/measure_skill_interpretations.py` は実モデルと課金を伴うため、合意した環境と範囲で使う。構造検証成功を業務品質と扱わない。

## 作業後

今回作った cache、build、一時 probe の process と file だけを片付ける。既存成果物と依存 directory は保持する。実際に検証した範囲、skip/未実施、部署への反映有無を報告する。
