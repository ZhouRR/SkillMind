# ProjectMind

Skill を Project のタスクとして実行し、根拠・結果・人工評価・外部変更の承認を記録する AI Agent プラットフォーム。
この README はコードと操作の入口。設計本文は `../docs/`、変更規約は [AGENTS.md](AGENTS.md) に集約する。

[文書ブラウザ](../docs/index.html) · [文書ガイド](../docs/README.md) · [現在の計画](../docs/planning/roadmap.md#当前执行状态)

## はじめに

| 目的 | 読む場所 |
| --- | --- |
| 全体を理解する | [製品概要](../docs/overview/product.md) / [システム構成](../docs/overview/architecture.md) |
| 開発を始める | [環境構築・検証](../docs/development/local-development.md) / [変更ガイド](../docs/development/change-guide.md) |
| 起動・更新・復旧する | [初回起動](../docs/operations/quickstart.md) / [公開・移行](../docs/operations/deployment.md) / [復元](../docs/operations/backup-recovery.md) |
| API を扱う | [利用ガイド](../docs/development/api-usage.md) / [契約変更](../docs/development/contract-workflow.md) |
| 設計・障害を調べる | [設計の責任分担](../docs/design/README.md) / [Runbook](../docs/operations/runbook.md#按问题找入口) |

コードは [Backend](#backend)・[Web](#web)・[Contracts](#contracts)・[Scripts](#scripts)・[Skills](#skills)・[Images](#images) に分ける。
実装の不足と検証範囲は上記の計画を正本とする。

## 実行前の注意

- Python 3.12、Node.js 26 / pnpm 11.7.0 を使う。DB・Redis・object storage と接続設定は別途必要。
- Compose は既存の共有 Traefik を使い、host port を公開しない。`make run` は配備検査後に API/Web だけを起動する。
- dispatch=false は既存 job・cron を止めない。[保守時の停止範囲](../docs/operations/deployment.md#一个例子关闭-dispatch-后仍有工作)を確認する。
- 通常の初期 ADMIN 作成は `python -m projectmind.ops.bootstrap_admin`。`make bootstrap-admin` は全 volume を消す。
- [共通設定入口](../docs/operations/deployment.md#环境文件与配置边界)で ENV_FILE と Compose project を明示する。配備は load/migrate/api/worker の独立段階で、宿主にも Python 3.12 が必要。

## Backend

[backend/src/projectmind](backend/src/projectmind/) は API と Worker の共有 package。
route は認証・入出力、service は業務調整、repository は永続化、AgentEngine/Provider は実行境界を担当する。

| コード入口 | 対応する設計 |
| --- | --- |
| [api](backend/src/projectmind/api/)・[auth](backend/src/projectmind/auth/)・[users](backend/src/projectmind/users/) | [認証](../docs/design/authentication.md) / [ログイン防護](../docs/design/login-protection.md) / [ユーザー](../docs/design/user-lifecycle.md) |
| [projects](backend/src/projectmind/projects/)・[documents](backend/src/projectmind/documents/)・[storage](backend/src/projectmind/storage/) | [Project](../docs/design/project-lifecycle.md) / [文書資産](../docs/design/document-lifecycle.md) |
| [skills](backend/src/projectmind/skills/)・[compositions](backend/src/projectmind/compositions/) | [Skill 契約](../docs/design/skill-contract.md) / [解釈](../docs/design/skill-interpretation.md) |
| [runs](backend/src/projectmind/runs/)・[worker](backend/src/projectmind/worker/) | [Run 作成](../docs/design/run-creation.md) / [Runtime](../docs/design/agent-runtime.md) / [停止](../docs/design/run-supervision.md) |
| [agent](backend/src/projectmind/agent/)・[integrations](backend/src/projectmind/integrations/) | [資源快照](../docs/design/resource-snapshots.md) / [Secret](../docs/design/secret-storage.md) / [予算](../docs/design/run-budgets.md) / [子分析](../docs/design/subagents.md) |
| [evaluations](backend/src/projectmind/evaluations/)・[effects](backend/src/projectmind/effects/)・[schedules](backend/src/projectmind/schedules/) | [回答](../docs/design/user-interactions.md) / [結果と評価](../docs/design/results-evaluation.md) / [外部変更](../docs/design/repository-effects.md) / [調度](../docs/design/task-scheduling.md) |
| [modules](backend/src/projectmind/modules/)・[db](backend/src/projectmind/db/)・[migrations](backend/migrations/) | [生成表示](../docs/design/generated-modules.md) / [データモデル](../docs/design/domain-model.md) |

[環境構築](../docs/development/local-development.md#backend)後、`PJM/backend/` でそれぞれ別 process として起動する。

```bash
python3 -m uvicorn projectmind.api.main:app --reload --port 8000
arq projectmind.worker.settings.WorkerSettings
```

検証は Ruff・Mypy・[tests](backend/tests/) の Pytest。[実 PostgreSQL の前提](../docs/development/local-development.md#実-postgresql-の前提)を先に読む。
全件実行には専用 DB の作成・強制削除を伴う test があり、skip は成功ではない。
[ops](backend/src/projectmind/ops/) の preflight は DB head/Redis の確認のみ。smoke は Run/Evaluation を作り、rotation は暗号文を更新する。

## Web

[web/src](web/src/) は React + TypeScript の画面。Task Center は実行対象の選択、Workspace は一つの Run の観察を担当する。

| コード入口 | 責務・設計 |
| --- | --- |
| [pages](web/src/pages/)・[components](web/src/components/) | [Workspace](../docs/design/workspace.md) / [Project](../docs/design/project-lifecycle.md) / [文書管理](../docs/design/document-lifecycle.md) / [調度管理](../docs/design/task-scheduling.md#保存后的管理入口) |
| [api](web/src/api/) | 資源別 client と validator。画面は index.ts、HTTP は http.ts を経由する |
| [lib](web/src/lib/)・[hooks](web/src/hooks/) | routing・SSE・入力草稿 / 非同期処理と cleanup。即時実行と調度は taskDraft / TaskLaunchFields を共有する |
| [i18n](web/src/lib/i18n/)・[styles](web/src/styles/)・[assets](web/src/assets/) | zh/ja/en、responsive layout、自己保持 font |

起動は `PJM/web/` で `npx -y pnpm@11.7.0 dev`。依存導入・proxy と typecheck/Vitest/build は[開発手順](../docs/development/local-development.md#web)へ。
実 component の応答喪失・切替・三語・keyboard・狭幅は同手順の browser 回帰で確認し、mock と実 Server の検証を分ける。
[アカウントと安全](../docs/design/user-lifecycle.md)は平台の `#/accounts`。Project 未所属でも本人安全を操作でき、ADMIN は組織ユーザーを管理する。
[Project の成員管理](../docs/design/project-lifecycle.md#成员管理页面)は `#/projects` の ADMIN 用 tab。会話・対象の隔離と未知結果の門禁はアカウント画面と共通 request hook を使う。
Task Center の[読取専用 Flow](../docs/design/task-flow.md#只读任务预览)は、Task 宣言・Skill 共通事項・現在 readiness を分けて表示する。Run Flow と[生成 Host](../docs/design/generated-modules.md)の未接続部分は計画を参照する。
[変更別の入口](../docs/development/change-guide.md)から対象の設計・API・回帰へ進む。

## Contracts

[contracts](contracts/) は versioned JSON Schema・example・OpenAPI の置場。権限・凍結・再送の意味は設計に置く。

| 配置 | 内容 |
| --- | --- |
| [runs](contracts/runs/) / [users](contracts/users/v1/) / [projects](contracts/projects/) / [tasks](contracts/tasks/) / [task-schedule](contracts/task-schedule/) / [events](contracts/events/) / [errors](contracts/errors/) | 公開 API・RunEvent・Problem の Schema |
| [tools](contracts/tools/) | versioned Tool capability の request / response / error |
| agent-task-brief / capability-blueprint / runtime-manifest / outcomes / view-spec | 業務固有 field を持たない共有契約 |
| [examples](contracts/examples/)・[fixtures](contracts/fixtures/) | 代表値 / offline 回帰入力。実 account や自動 seed ではない |
| [OpenAPI snapshot](contracts/openapi/projectmind-api.v1.json) | FastAPI から生成する公開面。手編集しない |

変更時は[契約変更手順](../docs/development/contract-workflow.md)に従い、DTO・Web validator・テスト・互換性を同期する。
example は scripts/validate_contracts.py と backend/tests/contracts/test_contracts.py の両表へ登録する。
Schema 通過は Runtime の実装・公開済み・実環境の受入を保証しない。未接続の公開面も計画で確認する。
[変更別の入口](../docs/development/change-guide.md)で、今回影響する消費側を選ぶ。

## Scripts

以下は `PJM/` で実行する。文書の依存・生成元・browser 検証は[文書管理](../docs/development/documentation.md)へ。
公開面を調べる場合は[契約変更手順](../docs/development/contract-workflow.md)から読取確認と生成操作を区別する。

| コマンド / 入口 | 用途と副作用 |
| --- | --- |
| `python3 scripts/validate_contracts.py` | Schema / example の読取検査 |
| `python3 scripts/validate_compose.py` | Compose 構造の読取検査。実起動ではない |
| [compose.py](scripts/compose.py) / [deploy.py](scripts/deploy.py) | 同一設定源 / identity を固定した分段配備。対象と承認は[配備手順](../docs/operations/deployment.md)を参照 |
| `PYTHONPATH=backend/src python3 scripts/probe_claude_agent_sdk.py` | offline SDK 契約確認。実モデルを呼ばない |
| `python3 scripts/build_docs.py --check` | 文書リンクと生成物の一致を検査 |
| `python3 scripts/build_docs.py` | Markdown と docs-viewer.html から docs/index.html を上書き生成 |
| `python3 -m unittest discover -s scripts/tests -v` | 文書・配備工具の一時 fixture / fake command 回帰。実 Docker は使わない |
| `python3 scripts/check_docs_browser.py` | offline Chromium 検査。--output 指定時だけ screenshot を上書き保存 |
| `python3 scripts/export_openapi.py` | 公開 API 変更時に snapshot を上書き。読むだけなら実行しない |
| [measure_skill_interpretations.py](scripts/measure_skill_interpretations.py) | 実モデル呼出し・課金を伴う品質測定 |
| [export-images.ps1](scripts/export-images.ps1) | 既存 local image の export。[Images](#images) の注意を参照 |

## Skills

[skills](skills/) は実行に影響する source package。各 package の SKILL.md を入口とし、文書整理では移動・書換えない。

| Package | 用途 |
| --- | --- |
| pjm-project-dev | 本プロジェクトの開発 workflow |
| projectmind-skill-interpreter | versioned system Skill。入力 source を実行せず候補契約を生成 |
| examples | repository、情報不足、危険 Tool/credential の回帰入力 |

package の配置は import・公開・Project 有効化ではない。source script の存在も実行許可を与えない。
[公開規則](../docs/design/skill-contract.md)に従い、実 credential や評価用の正解データを package に含めない。

## Images

`images/projectmind-images.tar` は配備 image の生成物であり、DB・blob・workspace・KEK の backup ではない。
操作は[image 移送](../docs/operations/quickstart.md#image-移送と更新)、復旧は[同一復元点](../docs/operations/backup-recovery.md#一致恢复点包含什么)を確認する。

- export は build/pull しない。application image 不足は失敗、第三者 image 不足は警告して除外される。
- 実 image ID と必要 image を確認し、送受信の checksum を比較する。checksum だけでは配布元の真正性は証明できない。
- 既存 tar は既定で上書きしない。ArchiveName で版を分け、-Force で唯一の回退用 archive を失わない。
- export は一時 tar が成功してから公開し、失敗時は旧成品を保持する。deploy-load も旧 image を削除せず、途中失敗時に自動復旧・後続起動しない。
- DB の復元を外部 write の取消と扱わない。Secret や archive を文書ブラウザへ埋め込まない。
