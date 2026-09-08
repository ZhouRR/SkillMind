# ProjectMind Backend

FastAPI API と ARQ Worker が同じ `projectmind` package を共有するモジュラーモノリス。API は認証/入出力、service は業務調整と transaction の境界、repository は query/lock/永続化、AgentEngine/Provider は実行境界を担当する。

[システム構成](../../docs/overview/architecture.md) · [ローカル開発](../../docs/development/local-development.md) · [変更規約](../AGENTS.md)

## 実装の入口

| Directory | 責務 |
| --- | --- |
| [api](src/projectmind/api/) | actor dependency、資源別 route、Problem |
| [skills](src/projectmind/skills/) / [compositions](src/projectmind/compositions/) | 導入・解釈・公開・組織資産・Project 有効化 |
| [runs](src/projectmind/runs/) / [worker](src/projectmind/worker/) | Segment/Attempt、Outbox、lease、待機・復旧・終態 |
| [agent](src/projectmind/agent/) | SDK adapter、Tool Gateway、workspace、物化、Evidence、子 Agent |
| [integrations](src/projectmind/integrations/) / [documents](src/projectmind/documents/) | 資源・Secret・binding・Project 文書 |
| [effects](src/projectmind/effects/) / [schedules](src/projectmind/schedules/) | 受控 write・時刻起動 |
| [auth](src/projectmind/auth/) / [projects](src/projectmind/projects/) | Session、ユーザー、Project と成员 |
| [db](src/projectmind/db/) / [migrations](migrations/) | 実 table・migration |
| [tests](tests/) | src の module 構成に対応する回帰 |

`modules/` は生成 FrontendModule の前置検査のみ。構築/配信 service は未実装。Blueprint は JSON 内嵌、ExecutableTask/RunStep は投影概念であり、同名 table の存在を仮定しない。

## 一つの変更を追う

| 調べたい動作 | 読む順序 | 設計の正本 |
| --- | --- | --- |
| Run の作成と要求再送 | [意図の正規化](src/projectmind/runs/creation_request.py) / [保存記録の検証](src/projectmind/runs/creation_replay.py) → [route](src/projectmind/api/routes/runs.py) → [service](src/projectmind/runs/service.py) → [repository](src/projectmind/runs/repository.py) | [作成と幂等](../../docs/design/run-creation.md)。初回 snapshot と要求の同一性を分ける |
| Worker の復旧・ユーザー応答 | [Executor](src/projectmind/worker/executor.py) → [runs](src/projectmind/runs/) → 対応する repository | [状態速查](../../docs/design/domain-model.md#run-状态速查)、[Runtime](../../docs/design/agent-runtime.md)。Attempt と Segment を混同しない |
| 時刻起動・停止後の挙動 | [Schedule service](src/projectmind/schedules/service.py) → [repository](src/projectmind/schedules/repository.py) → RunService | [調度設計](../../docs/design/task-scheduling.md)。認領・作成・回写は現在別 transaction |
| Agent が何を読めるか | [context_builder](src/projectmind/agent/context_builder.py) → [Gateway](src/projectmind/agent/tool_gateway.py) → 対象 Provider | [資源快照](../../docs/design/resource-snapshots.md)、[認証](../../docs/design/authentication.md) |
| Run 資源を何まで公開するか | [resource_projection](src/projectmind/runs/resource_projection.py) → [response model](src/projectmind/api/routes/runs.py) → Schema / Web validator | [公開投影の設計](../../docs/design/resource-snapshots.md#公开选择与读取投影的实施契约)。内部 JSON は応答へ直接流さない |
| 上限・timeout・子分析 | [SDK options](src/projectmind/agent/claude.py) → [Executor](src/projectmind/worker/executor.py) / [dispatch](src/projectmind/agent/subagent_provider.py) | [Run 予算](../../docs/design/run-budgets.md) |
| migration・復旧・運用 CLI | [migration](migrations/versions/) → [ops](src/projectmind/ops/) → [実 DB 回帰](tests/db/) | [移行と回退の審査](../../docs/operations/runbook.md#迁移与回退审查)、[復旧確認](../../docs/operations/runbook.md#恢复后验证)。preflight は DB head/Redis の基線だけを確認する |

これは読み順であり、route に業務判断を追加する指示ではない。domain/validator の単一実装と対応する `tests/<module>/` を一緒に確認する。新 Segment の Brief は Worker 準備時に凍結され、ユーザー応答 transaction で完成済みとは仮定しない。

`documents/__init__.py` は domain 型だけを公開する。service/repository/source は対象 module から明示 import し、DB model → Run domain → 文書選択規則という依存から repository を先行 load しない。入口の便利な再輸出が循環 import を作るためであり、[独立 process の import 回帰](tests/runs/test_creation_replay.py)も確認する。

新旧の作成 hash は同一ではない。互換処理を省いて過去 Run の snapshot/hash を書き換えたり、API/調度ごとに別の比較実装を追加したりしない。現在の未完了箇所と検証範囲は[計画](../../docs/planning/roadmap.md#13-当前执行状态)を参照する。

資源投影の変更では[文書契約の対応表](../contracts/README.md#run-文書契約を読む)から、example、実 API 応答のテストと Web validator まで辿る。[契約変更の手順](../../docs/development/contract-workflow.md)で OpenAPI、歴史データと版互換も確認する。投影のために保存済み snapshot を更新しない。

## Skill の公開と Project 可視性を追う

規則の正本は[公開と就緒の判断順](../../docs/design/skill-contract.md#发布与就绪的判断顺序)、呼出し順は[候補から Task への接続](../../docs/design/skill-interpretation.md#从候选到项目任务的接线)である。README は同じ状態表を複製せず、変更する境界を示す。

| 変更する境界 | コードと回帰 |
| --- | --- |
| 取込と解釈入力 | [importer](src/projectmind/skills/importer.py) / [interpreter](src/projectmind/skills/interpreter.py) → [importer test](tests/skills/test_skill_importer.py)。Adapter は正規化だけを担い、blueprint を生成しない |
| DRAFT と公開 gate | [service](src/projectmind/skills/service.py) の create_version_draft → [manifest_gate](src/projectmind/skills/manifest_gate.py) → [repository](src/projectmind/skills/repository.py) の publish_skill_version → [gate test](tests/skills/test_manifest_gate.py) |
| Project の有効化/無効化・廃止・削除 | 同 repository の lifecycle → [repository test](tests/skills/test_skill_repository.py)。組織境界、精確版、参照保護を確認し、再有効化を既存 toggle と仮定しない |
| Task の発見と就緒度 | [task_catalog](src/projectmind/skills/task_catalog.py) / repository query → [resource_binding](src/projectmind/skills/resource_binding.py) → [readiness test](tests/skills/test_resource_binding.py)。候補の存在と Run の凍結を分ける |

公開 DTO/Schema は [Skill 契約の接続先](../contracts/README.md#skill-の公開と-project-有効化を読む)、画面は [Web の案内](../web/README.md#画面から実装へ進む)へ進む。Manifest の不可変性を理由に版の状態更新まで禁止せず、反対に metadata 更新で本文を書き換えない。再有効化は[後続の監査設計](../../docs/design/skill-contract.md#112-可审计的重新启用与回滚)を満たす変更として扱い、単に disabled_at を消さない。

## 入力準備の接続を引き継ぐ

入力準備は [Run 全体の回执プロトコル](../../docs/design/resource-snapshots.md#输入准备与可信缓存)を正本とする。下表は接続の読み順であり、検証済み機能の一覧ではない。Worker/Tool に呼出しが存在することと、実 DB・準備中断を含む実行連鎖の完成を分け、現在の不足と回帰結果は[計画](../../docs/planning/roadmap.md#13-当前执行状态)で確認する。

| 接続点 | 一緒に確認する実装 |
| --- | --- |
| 永続回执と transaction | [DTO/port](src/projectmind/runs/input_snapshot.py) → [Postgres store](src/projectmind/runs/repository_inputs.py) → [Worker startup](src/projectmind/worker/settings.py)。ファイル I/O の前後だけ短い transaction を使う |
| 準備結果の受け渡し | [WorkspaceMaterializer](src/projectmind/agent/workspace_materializer.py) の `PreparedInput` → [ContextBuilder](src/projectmind/agent/context_builder.py)。旧 resource tuple だけの戻り値ではない |
| 論理 input と実 byte | [RunWorkspace](src/projectmind/agent/domain.py) → [input_workspace](src/projectmind/agent/input_workspace.py) → [workspace Provider](src/projectmind/agent/workspace_provider.py)。世代と回执を引き継ぎ、検証した byte から応答/Evidence を作る |
| repository の再検証 | [RepositorySnapshotSource](src/projectmind/agent/repository_source.py) の `inspect` と `open`。scope/checksum の確認と外部取得を区別し、fake も契約へ同期する |
| 準備から開始までの監督 | [Executor](src/projectmind/worker/executor.py) の準備前 TaskGroup → ContextBuilder → Brief 凍結 → [RunRepository](src/projectmind/runs/repository.py) の `verify_execution_start`。lease/取消を再検証してから model を呼ぶ |
| 限額の設定と注入 | [Settings](src/projectmind/core/settings.py) → [.env.example](../.env.example) → [Worker startup / job 登録](src/projectmind/worker/settings.py)。入力総量と準備 timeout を渡し、Run job だけ準備時間分の余裕を持たせる。値と適用範囲は[計時器の正本](../../docs/design/run-budgets.md#现有计时器的覆盖范围) |

Worker startup は必須 store と総量設定を物化器へ渡し、Tool は `input_workspace` の読取と安全 I/O を使う。検証は責務ごとに分ける。

| 確認する境界 | テスト入口と証明の限度 |
| --- | --- |
| 準備中の heartbeat・取消・timeout・開始前拒否 | [preparation supervision](tests/worker/test_preparation_supervision.py)、[Executor](tests/worker/test_agent_run_executor.py)。fake service/engine の制御順序であり、実モデル停止の保証ではない |
| DB gate と設定の消費 | [execution gates](tests/runs/test_execution_gates.py)、[Settings](tests/core/test_settings.py)、[startup](tests/worker/test_worker_startup.py)。SQL 構築/lock 順序は mock、constructor は実装を通す；実 PostgreSQL の競合は別検証 |
| 実 byte・探索・安全な書き込み | [workspace Provider](tests/agent/test_workspace_provider.py)、[storage](tests/agent/test_materialization_storage.py)。一時 file の検証であり、回执 commit の crash/recovery は含まない |

`materialize` 呼出しでは claim、Project/Run と workspace の identity を揃え、凍結來源を明示する。戻り値の `PreparedInput.workspace / resources` を両方引き継ぐ。旧 fixture の引数不足や旧戻り値の参照を、production 側の optional 化で吸収しない。

`input_files=None` は未検証、空 tuple は合法な空入力になり得るため、互換 default で同一視しない。`input/` は Tool の論理 path、`.projectmind-inputs/<snapshot_id>/` は平台の物理配置であり、model や公開 API に任意の世代選択を開放しない。

[文書物化 test](tests/agent/test_document_materialization.py)、[全物化 test](tests/agent/test_workspace_materializer.py)、[Tool test](tests/agent/test_workspace_provider.py)、[Runtime context test](tests/agent/test_runtime_context.py)を消費側として確認する。旧 fixture の不足を理由に本番回执を optional に戻さず、明示的な test store と実 DB 検証を分ける。migration head、構造検査や以前の回帰だけで機能が有効とは判断しない。

## 起動と検証

環境構築・DB 接続設定は[ローカル開発](../../docs/development/local-development.md)を参照する。依存導入後、`backend/` で API を起動する。

```bash
python3 -m uvicorn projectmind.api.main:app --reload --port 8000
```

同じ設定で Worker を別 process として起動する場合：

```bash
arq projectmind.worker.settings.WorkerSettings
```

Worker dispatch は明示有効化が必要。DB/Redis/object storage が別途必要であり、production 相当の構成は[Compose 起動](../../docs/operations/quickstart.md)に従う。

Backend の変更は Ruff/Mypy/Pytest と関連する契約/SDK probe を確認する。実 DB test の skip は未検証として報告する。
