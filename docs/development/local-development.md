# ローカル開発と検証

全体の入口は [PJM README](../../PJM/README.md)。以下はコード root `PJM/` 起点で、各節に実行 directory を明示する。

## 前提と設定

Python 3.12、Node.js 26 / pnpm 11.7.0 を用意する。venv は作らず Backend 依存を user site に置き、lockfile を手編集しない。導入だけでは DB/Redis/object storage は起動しない。

`.env.example` は production 雛形。HTTP localhost は `PROJECTMIND_ENVIRONMENT=development`、許可 Origin、専用接続先、書込可能な Run workspace を設定する。通常 Backend の `.env` は実行 directory 基準なので、`backend/` から親の file が自動で読まれると仮定せず、設定 file または process environment を明示する。Compose は [compose.py の共通設定入口](../operations/deployment.md#环境文件与配置边界)で ENV_FILE を同一 source に固定する。

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
python3 tests/browser/check_accounts.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/accounts.html
python3 tests/browser/check_projects.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html
python3 tests/browser/check_project_members.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html
python3 tests/browser/check_project_management.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html
python3 tests/browser/check_run_submission.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/run-submission.html
python3 tests/browser/check_interaction_responses.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/run-submission.html
python3 tests/browser/check_result_references.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-result-reference-browser
python3 tests/browser/check_artifacts.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-artifact-browser
python3 tests/browser/check_evaluation_submissions.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-evaluation-browser
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=../backend/src \
  python3 tests/browser/check_document_sources.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/run-submission.html
python3 tests/browser/check_schedule_times.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/run-submission.html
python3 tests/browser/check_task_flow.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-task-flow-browser
python3 tests/browser/check_schedule_management.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html
python3 tests/browser/check_document_management.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-document-browser
python3 tests/browser/check_document_preview.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-document-preview-browser
python3 tests/browser/check_document_upload.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-document-upload-browser
python3 tests/browser/check_document_upload_receipts.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-document-upload-receipts-browser
python3 tests/browser/check_document_upload_closures.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --output /tmp/projectmind-document-upload-closures-browser
```

| runner | 検証する範囲と限界 |
| --- | --- |
| login | 実 LoginPage の二重 submit、拒否・abort・遅延応答、三語/keyboard/窄屏。実 session、HTTPS、多 tab は別 |
| accounts | 実 AccountsPage と App の本人安全、ADMIN 管理、検索/ページング、原版比較・未知・遅延応答・会話切替。実 DB の競争、Redis、HTTPS は別 |
| projects | 実 App の失効/空/重複 Project link、精確詳細 gate、原対象の再読取・遅延応答・帰還、帰档履歴、三語/狭幅導航。API は全 mock、実 membership/削除競争・HTTPS は別 |
| project_members | 実 App の ADMIN 成員管理、関係/アカウント状態の区別、候補検索/page、原対象確認・未知結果の照合・遅延・切替。全 API は mock、実加入/禁用競争・監査 transaction は別 |
| project_management | 実 App の作成/編集/帰档/復元/削除、原版比較・未知照合・初回選択・対象/会話切替、三語/keyboard/狭幅。API は全 mock、実 DB の CAS/rollback、0034 と HTTPS は別 |
| run_submission | 実 Workspace の応答喪失、同 key/body 確認、明示的新規、actor/Project 切替・refresh。実 transaction/唯一制約は別 |
| interaction_responses | 実 Workspace の普通答復、原要求確認、競合/期限、同 tick/30 秒/旧応答、会話/対象切替、詳細/SSE/表示 tab 更新と三語。API は全 mock、実答復/過期 transaction・撤権競争と model 停止は別 |
| result_references | 実 Workspace の保存時検証範囲、旧欠損/壊れた新 field、flag 矛盾、モデル効果と platform 記録の区別、三語/狭幅。全 API は mock；実 DB 参照照合、Artifact 保存や遠端 write の証明ではない |
| artifacts | 実 App の公開索引/結果採用、v1/v2/旧欠損、原 size/hash/UTF-8 と実 stream 上限、拒否/同 tick/期限/旧応答/対象切替、実 download と三語。全 API は mock；実 DB の同時保存/0040/権限競争と備份恢复は別 |
| evaluation_submissions | 実 App の複数修正案/原値、原要求の保存/読取専用確認/明示再送、cursor ページ、期限/取消/旧応答/対象切替、帰档と三語。全 API は mock；実 DB の同キー競争、0041/commit/撤権と復元は別 |
| document_sources | 即時/調度入力、清単、CSRF と凍結表示。Backend 依存と純 parser は使うが、実 blob、物化、調度編集/時区/認領 crash は別 |
| document_management | 実 App の削除防重、参照拒否、原 ID の未知照合/人工解除、読取拒否・絶対期限後の失効応答、切替、帰档と三語/狭幅。全面 mock API で、実 DB 参照競争・blob 清理は別 |
| document_preview | 実 App の静的 HTML/CSP/sandbox、実 stream byte 上限、拒否/期限/旧応答/同 tick 切替、三語/狭幅。HTTP は mock、byte は合成 stream；実 S3・Proxy・他 browser engine は別 |
| document_upload | 実 App の multipart/CSRF、サイズ等の確定拒否、未知の成功応答、資格拒否と三語。全 HTTP は mock；実 DB/PUT/清理は別 |
| document_upload_receipts | 実 App の原 key、部分拒否/未知の batch pause、原 key GET と明示継続、独立した手動照会の終了/取消/別 key、File 保持、防重/期限/切替と三語。全 HTTP は mock；実 DB/PUT/再起動・清理と完全な跨刷新 batch 回復は別 |
| document_upload_closures | 実 App の原 key の明示停止、独立回执/未知核対、元 batch の明示継続、手入力の独立回復、三語/狭幅。全 HTTP は mock；実 DB 競争/0042/旧 PUT 停止・清理・quota 結算は別 |
| schedule_times | 実 TasksPage/ScheduleDialog の時区/offset、DST、現在の preview 確認、防重・遅延・期限と対象切替、三語/狭幅。全 API は mock、実 cron 発火・認領/Run transaction・DB/複数 Worker は別 |
| task_flow | 実 App/TasksPage の精確 Task 読取、Task/Skill 共有範囲、原参照・未評価・欠落/損傷、期限と旧応答隔離、三語/keyboard/狭幅。数値は下記の実 serializer wire を渡して別途検証する。全 API は mock；実 DB 授権競争、元 source/blob、モデルの意味保真と Run Flow は別 |
| schedule_management | 実 App の独立調度一覧/検索/全ページ、原配置編集/版衝突/未知と対象切替、在途の旧形式/空/期限/認領上限・独立読取拒否、三語/狭幅。全 API は mock、実 DB の CAS/撤権/多 Worker と発火は別 |

初回導入は download を伴う。外部依存は PYTHONPATH、browser は PLAYWRIGHT_BROWSERS_PATH で指定でき、document_sources では Backend src と両方を含める。`--output` は明示した新しい工作区外 directory に screenshot を保存する。port が使用中なら奪わず別 port と URL を使い、終了時は自分の Vite だけを停止する。

Task Flow の数値検証は Backend 依存も使用する。`PJM/web/` で合成 fixture を実 projector/route/serializer に通し、その原 byte を渡す。実 DB/model は使わず、途中で JavaScript の JSON parse/stringify を挟まない：

```bash
task_flow_wire=$(mktemp /tmp/projectmind-flow-wire.XXXXXXXX.json)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=../backend/src:../backend \
  python3 -c 'import sys; from tests.skills.task_flow_numeric_fixtures import numeric_flow_wire; sys.stdout.buffer.write(numeric_flow_wire())' > "$task_flow_wire"
python3 tests/browser/check_task_flow.py \
  --url http://127.0.0.1:5189/projectmind/tests/browser/projects.html \
  --numeric-wire "$task_flow_wire" --output /tmp/projectmind-task-flow-numeric-browser
```

両原契約の大整数・浮動小数・負零と再帰表示、型・重複・逆転範囲の拒否を検証する。`--numeric-wire` 省略時は当該 case を実行せず、その事実を runner が報告する。

## 変更に応じた検証

| 対象 | 実行位置とコマンド |
| --- | --- |
| Backend | `PJM/backend/`：`python3 -m ruff check .`、`python3 -m mypy src`、上記副作用確認後の pytest |
| Web | `PJM/web/`：`node_modules/.bin/tsc -b --pretty false`、`node_modules/.bin/vitest run`、`node_modules/.bin/vite build` |
| 契約 | `PJM/`：`python3 scripts/validate_contracts.py`。快照一致性は[契約 workflow](contract-workflow.md#遇到未接齐的交付链) |
| Compose / 配備 | `PJM/`：`python3 scripts/validate_compose.py`、`python3 -m unittest discover -s scripts/tests -v`。Docker 利用可・対象確認済みなら `make config` |
| SDK offline | `PJM/`：`PYTHONPATH=backend/src python3 scripts/probe_claude_agent_sdk.py` |
| 文書 | `PJM/`：[文書維持](documentation.md)の build/check・回帰 |

通常の config 出力には Secret が含まれ得る。配備工具 test は合成環境 file、tar と fake 子 process で source・引数・拒否・失敗後に進まないことを検証する。実 DB skip、mock と実環境、静的検査と配備を区別する。Docker / Make / PowerShell がない場合、それぞれ実注入・段階起動/復旧、Make 展開、Windows export の未検証を報告する。

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
