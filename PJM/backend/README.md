# ProjectMind Backend

FastAPI API と ARQ Worker が同じ `projectmind` package を共有するモジュラーモノリス。API は認証/入出力、service は業務調整と transaction の境界、repository は query/lock/永続化、AgentEngine/Provider は実行境界を担当する。

[変更から選ぶ](#一つの変更を追う) · [認証](#認証と-secret-の境界を追う) · [入力準備](#入力準備の接続を引き継ぐ) · [停止](#実行の取消と停止を追う) · [予算](#予算と子分析の接続を追う) · [起動と検証](#起動と検証) · [運用 CLI](#運用-cli-と停止境界を確認する)

[ユーザー管理](#ユーザー管理の接続を引き継ぐ) · [Skill 公開](#skill-の公開と-project-可視性を追う) · [外部変更](#承認から外部変更まで追う) · [調度](#schedule-の認領と回写を追う) · [生成 module](#生成-module-の前置実装を読む)

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
| [auth](src/projectmind/auth/) / [users](src/projectmind/users/) / [projects](src/projectmind/projects/) | 会話・初期化 / アカウント管理 / Project とメンバー。users の API と未接続の消費側は下記参照 |
| [db](src/projectmind/db/) / [migrations](migrations/) | 実 table・migration |
| [tests](tests/) | src の module 構成に対応する回帰 |

`modules/` は[生成 FrontendModule の前置実装](#生成-module-の前置実装を読む)であり、公開 API の module(SkillComposition)とは別物。構築/配信 service は未実装。Blueprint は JSON 内嵌、ExecutableTask/RunStep は投影概念であり、同名 table の存在を仮定しない。

## 一つの変更を追う

| 調べたい動作 | 設計から実装へ読む順序 |
| --- | --- |
| ログイン・Project 認可・Secret | [認証](../../docs/design/authentication.md) / [Secret](../../docs/design/secret-storage.md) → [接続案内](#認証と-secret-の境界を追う)。現在の利用者、凍結権限、外部 credential を分ける |
| アカウントの作成・変更・会話失効 | [ユーザー lifecycle](../../docs/design/user-lifecycle.md) → [管理の接続案内](#ユーザー管理の接続を引き継ぐ)。ProjectMember 管理とは別の用例 |
| Run の作成と要求再送 | [作成と幂等](../../docs/design/run-creation.md) → [意図](src/projectmind/runs/creation_request.py) / [再確認](src/projectmind/runs/creation_replay.py) → [route](src/projectmind/api/routes/runs.py) / [service](src/projectmind/runs/service.py) / [repository](src/projectmind/runs/repository.py) |
| Worker の復旧・ユーザー応答 | [状態速查](../../docs/design/domain-model.md#run-状态速查) / [Runtime](../../docs/design/agent-runtime.md) → [Executor](src/projectmind/worker/executor.py) / [runs](src/projectmind/runs/)。Attempt と Segment を分ける |
| 取消・Worker 終了・後処理 | [監督と停止](../../docs/design/run-supervision.md) → [接続案内](#実行の取消と停止を追う)。Run 終態と process 停止を分ける |
| 時刻起動・停止後の挙動 | [調度](../../docs/design/task-scheduling.md) → [認領と回写](#schedule-の認領と回写を追う)。認領・作成・回写は別 transaction |
| Agent が何を読めるか | [資源快照](../../docs/design/resource-snapshots.md) → [context_builder](src/projectmind/agent/context_builder.py) / [Gateway](src/projectmind/agent/tool_gateway.py) → 対象 Provider |
| Run 資源の公開範囲 | [公開投影](../../docs/design/resource-snapshots.md#公开选择与读取投影的实施契约) → [resource_projection](src/projectmind/runs/resource_projection.py) → [response model](src/projectmind/api/routes/runs.py) / Schema / Web。内部 JSON を直接返さない |
| 上限・timeout・子分析 | [Run 予算](../../docs/design/run-budgets.md) / [子結果](../../docs/design/subagents.md#当前返回值的可信边界) → [接続案内](#予算と子分析の接続を追う) |
| migration・復旧・運用 CLI | [移行審査](../../docs/operations/deployment.md#迁移与回退审查) / [復旧確認](../../docs/operations/backup-recovery.md#恢复后验证) → [migration](migrations/versions/) / [ops](src/projectmind/ops/) / [実 DB 回帰](tests/db/)。preflight の通過は復旧証明ではない |

これは読み順であり、route に業務判断を追加する指示ではない。domain/validator の単一実装と対応する `tests/<module>/` を一緒に確認する。新 Segment の Brief は Worker 準備時に凍結され、ユーザー応答 transaction で完成済みとは仮定しない。

`documents/__init__.py` は domain 型だけを公開する。service/repository/source は対象 module から明示 import し、DB model → Run domain → 文書選択規則という依存から repository を先行 load しない。入口の便利な再輸出が循環 import を作るためであり、[独立 process の import 回帰](tests/runs/test_creation_replay.py)も確認する。

新旧の作成 hash は同一ではない。互換処理を省いて過去 Run の snapshot/hash を書き換えたり、API/調度ごとに別の比較実装を追加したりしない。現在の未完了箇所と検証範囲は[計画](../../docs/planning/roadmap.md#13-当前执行状态)を参照する。

資源投影の変更では[文書契約の対応表](../contracts/README.md#run-文書契約を読む)から、example、実 API 応答のテストと Web validator まで辿る。[契約変更の手順](../../docs/development/contract-workflow.md)で OpenAPI、歴史データと版互換も確認する。投影のために保存済み snapshot を更新しない。

## 認証と Secret の境界を追う

[多ページでの認証の例](../../docs/design/authentication.md#一个例子同一账号打开两个页面)と[Secret 保存の例](../../docs/design/secret-storage.md#一个例子保存成功不等于资源可用)から、どの失敗を調べるか決める。公開 field が正しくても、期限、現在の権限、停止や外部到達は別の検証である。

| 変更する境界 | 実装と回帰の入口 |
| --- | --- |
| Login の入口防護 | [専用の接続案内](#ログイン入口の防護を追う) → middleware / Redis / Problem。来源配額と account 検査、challenge、Session の失効を分ける |
| Session / CSRF | [auth route](src/projectmind/api/routes/auth.py) → [AuthService](src/projectmind/auth/service.py) / [domain](src/projectmind/auth/domain.py) → [auth tests](tests/auth/) / [API test](tests/api/test_auth_api.py)。安定 CSRF、role/期限と失効は fake transaction、challenge は fake Redis の検査を含む |
| 会話の切替と保持 | [migration 0031](migrations/versions/0031_auth_session_credentials.py) / [model](src/projectmind/db/models.py) → [構造回帰](tests/db/test_auth_session_migration.py)。[実 DB test](tests/db/test_real_auth_sessions.py)は別の検証で、専用 DB の作成/削除を伴う。存在を実施済み証拠と扱わない |
| token 応答の cache | [API middleware](src/projectmind/api/main.py) → login 入口 path と auth tag の route / API test。route 確定前の login 拒否も対象だが、未処理例外・proxy 自身の応答やサイト全体へ保証を拡張しない |
| 現在の Project / role | [actor dependency](src/projectmind/api/auth_dependencies.py) → [Project service](src/projectmind/projects/service.py) / [repository](src/projectmind/projects/repository.py) → [Project API test](tests/api/test_project_api.py)。Read/Write alias と資源所有確認を分ける |
| Secret 作成と公開範囲 | [integration route](src/projectmind/api/routes/integrations.py) → [service](src/projectmind/integrations/service.py) / [repository](src/projectmind/integrations/repository.py) → [integration tests](tests/integrations/) / [API test](tests/api/test_integration_api.py) |
| 復号・実行時の使用・鍵更新 | [SecretCipher](src/projectmind/core/secret_crypto.py) → [resolver](src/projectmind/integrations/secrets.py) / [Run binding](src/projectmind/agent/run_binding.py) / [rotation CLI](src/projectmind/ops/rotate_secrets.py) → [crypto test](tests/core/test_secret_crypto.py)。CLI は全材料への書込操作であり、診断用に起動しない |

現在の v2 は GET session で同じ CSRF を返し、User → AuthSession の lock 後に期限・role snapshot・失効を確認する。[会話 protocol](../../docs/design/authentication.md#会话凭据-v2-与切换要求)と[切替条件](../../docs/operations/deployment.md#会话协议切换检查)を先に読み、DB の version default 1 を不用意に変更しない。旧 cookie の自動移行や旧 API との混在は扱わない。

ユーザー管理は API まで接続された工作副本があり、OpenAPI/Web と全体回帰は未完了。[専用の接続案内](#ユーザー管理の接続を引き継ぐ)から継続し、ProjectMember 管理で代用しない。role snapshot は撤権履歴の代わりにならず、[認証 transaction の範囲](../../docs/design/authentication.md#认证与业务提交不是同一个事务)を全業務の提交まで拡張した保証と扱わない。

既存の KEK 名は互換上保持するが、実装はマスター鍵による直接 AES-256-GCM であり DEK/KEK 二層ではない。[鍵更新と復元条件](../../docs/design/secret-storage.md#切换与恢复的顺序)を読み、同じ version label の key 差替えや skipped 件数を復号の検証として扱わない。公開契約は[認証と Secret の索引](../contracts/README.md#認証と-secret-の契約を読む)、画面側は[Web の案内](../web/README.md#ログインと書込失敗を切り分ける)へ進む。

### ログイン入口の防護を追う

まず[一回のログインと二回の入口 request](../../docs/design/login-protection.md#一个例子一次登录两次入口请求)を読む。来源の HTTP 計数と account/組合の試行計数は別段階であり、Session の有効性を Redis で判定する設計ではない。

| 変更する境界 | 実装と検証の入口 |
| --- | --- |
| Body より前の来源 gate | [API middleware](src/projectmind/api/main.py) → AuthService.begin_login。login/context は [API fake test](tests/api/test_login_protection_api.py) / [ASGI + Redis test](tests/auth/test_real_login_http.py)、改密は [users API test](tests/api/test_users_api.py)。各 test の替身範囲を別々に読む |
| 共有配額と有限退避 | [LoginProtection](src/projectmind/auth/login_protection.py) → [純粋な境界 test](tests/auth/test_login_protection.py) / [隔離 Redis test](tests/auth/test_real_login_protection.py)。実 Lua と fake 応答の証拠を混同しない |
| challenge と password への入口 | [AuthService](src/projectmind/auth/service.py) → [service test](tests/auth/test_auth_service.py)。SET 成功未確認、GETDEL 異常 marker、断連/timeout は password の前で拒否する |
| 成功時の共有配額 | [Session service test](tests/auth/test_session_service.py)。実 Redis と password 検証を使う case でも Session 保存は fake であり、実 PostgreSQL transaction の証拠ではない |
| 429/503 の公開情報 | [Problem factory](src/projectmind/api/login_protection.py) / [auth route](src/projectmind/api/routes/auth.py) → [契約の同期先](../contracts/README.md#認証と-secret-の契約を読む) / [Web HTTP client](../web/src/api/http.ts)。header を JSON field と混同しない |

設定は [Settings](src/projectmind/core/settings.py) → [.env.example](../.env.example) → API lifespan を一緒に確認する。詳しい[計数/退避](../../docs/design/login-protection.md#计数与退避如何恢复)と[来源/切替](../../docs/design/login-protection.md#来源识别与上线边界)は設計だけに保持する。実 Redis test は専用 process を起動するため、実行前に[隔離検証の前提](../../docs/development/local-development.md#隔离-redis-登录防护验证)を読む。

実 Redis の共通 fixture は [tests/auth/conftest.py](tests/auth/conftest.py) にある。Problem の宣言は[共有 schema mirror](src/projectmind/api/problems.py)と[契約 test](tests/contracts/test_contracts.py)から追う。route/OpenAPI、実応答、Web 消費の証拠を分け、現在の残項目は[計画 R05](../../docs/planning/roadmap.md#r05-领域与身份安全)で確認する。Lua、ASGI と fake Session の成功だけで HTTPS proxy、Redis 切替や実 DB session 作成まで完了したと扱わない。

## ユーザー管理の接続を引き継ぐ

まず[停止と再有効化の例](../../docs/design/user-lifecycle.md#一个例子停用再启用不恢复旧登录)と[内部実装と公開入口](../../docs/design/user-lifecycle.md#工作副本与公开入口)を読む。以下はコードへの入口であり、稼働済み機能の一覧ではない。

| 確認する境界 | 実装と接続先 |
| --- | --- |
| 管理 transaction | [domain](src/projectmind/users/domain.py) → [service](src/projectmind/users/service.py) → [repository](src/projectmind/users/repository.py) → [service test](tests/users/test_user_service.py)。認証は [sessions validator](src/projectmind/auth/sessions.py)を共有し、平行実装を作らない |
| 保存と初期化 | [models](src/projectmind/db/models.py) / [0032](migrations/versions/0032_user_lifecycle.py) → [bootstrap](src/projectmind/auth/bootstrap.py)。初 ADMIN と CREATED の追加呼出しは接続済み。実 DB の親子 INSERT・rollback・並行 bootstrap は別の検証 |
| 公開入口と防護 | [users route](src/projectmind/api/routes/users.py) / [API startup・middleware](src/projectmind/api/main.py) → [users API test](tests/api/test_users_api.py)。本人改密は来源 gate と AuthService.admit_password_change、各 mutation は actor alias を通る |
| 消費側への引渡し | [公開同期の案内](../contracts/README.md#ユーザー管理の公開面を準備する) → [Web 接続](../web/README.md#アカウント管理を接続する)。Schema/example は存在するが、OpenAPI snapshot と専用 Web client/page は未完了 |

管理 lock の順序、授権の判定点、最後の活動 ADMIN、持久失効と監査の正本は[管理 transaction](../../docs/design/user-lifecycle.md#事务与并发)。middleware が生成した[相関 ID](../../docs/design/user-lifecycle.md#审计与请求关联)を route から渡すが、ID を幂等 key として再利用しない。UserSecurityEvent の存在を全 DB 書込への UPDATE/DELETE 防止機構と読まない。

専用回帰は二層ある。users test は実 service と mock repository/transaction、API test は実 middleware/route と [fake services](tests/api/fakes.py)を使う。同時実行では tests/users の裸 conftest import が API 側と衝突するため、まず収集を修復する。別々の通過を全体成功として報告せず、失敗命令と現在の実施範囲は[計画 R05](../../docs/planning/roadmap.md#r05-领域与身份安全)から確認する。

次に [OpenAPI 一致性 test](tests/contracts/test_contracts.py)、Web と[管理の受入表](../../docs/design/user-lifecycle.md#开发接续与验收)へ進む。今回の文書整理では既存の実装・test・公開契約を変更していない。

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
| 回执の所有者・完成確認 | [repository inputs](tests/runs/test_repository_inputs.py)。同世代の一度だけの commit 確認、古い lease/取消の拒否を mock DB/transaction で確認する。実 driver の応答喪失とは別 |
| 全根の完成・再訪・中断 | [input preparation protocol](tests/agent/test_input_preparation_protocol.py)。実 file と独立 memory store で孤立 namespace、逐根/総量、共同改竄、接管/取消、READY 再訪を検査する。実 DB 復元は別 |
| 回执 migration と保持 | [input snapshot migration](tests/db/test_input_snapshot_migration.py)。model/DDL の型・制約と downgrade の呼出し順を比較する。実 PostgreSQL で SQL を実行した証拠ではない |
| 実 byte・探索・安全な書き込み | [workspace Provider](tests/agent/test_workspace_provider.py)、[storage](tests/agent/test_materialization_storage.py)。一時 file の検証であり、回执 commit の crash/recovery は含まない |

`materialize` 呼出しでは claim、Project/Run と workspace の identity を揃え、凍結來源を明示する。戻り値の `PreparedInput.workspace / resources` を両方引き継ぐ。旧 fixture の引数不足や旧戻り値の参照を、production 側の optional 化で吸収しない。

`input_files=None` は未検証、空 tuple は合法な空入力になり得るため、互換 default で同一視しない。`input/` は Tool の論理 path、`.projectmind-inputs/<snapshot_id>/` は平台の物理配置であり、model や公開 API に任意の世代選択を開放しない。

初回は UUID 子 directory だけでなく namespace 全体を独占作成する。既存の空 namespace も中断の痕跡として拒否し、別 UUID で再取得しない。READY の再訪は新規作成を通さない。[準備中断の規則](../../docs/design/resource-snapshots.md#准备中断与再次使用)に従い、孤立現場を消して再実行する手順を追加しない。
[文書物化 test](tests/agent/test_document_materialization.py)、[全物化 test](tests/agent/test_workspace_materializer.py)、[Tool test](tests/agent/test_workspace_provider.py)、[Runtime context test](tests/agent/test_runtime_context.py)を消費側として確認する。旧 fixture の不足を理由に本番回执を optional に戻さず、明示的な test store と実 DB 検証を分ける。migration head、構造検査や以前の回帰だけで機能が有効とは判断しない。

## 実行の取消と停止を追う

まず[取消後の四つの事実](../../docs/design/run-supervision.md#一个例子点击取消之后)を読む。準備から model 開始までの順序は Runtime、取消/lease/Worker 終了と後処理の詳細は[監督設計](../../docs/design/run-supervision.md#不同阶段如何收束)が担当する。

| 接続点 | コードと検証の入口 |
| --- | --- |
| 取消意図から待機の中断 | [Executor](src/projectmind/worker/executor.py) の _monitor_cancellation → _build_until_cancelled / _first_engine_event。首 event 前は session_ref を待たず、所有する await を取消する |
| client と全分支の寿命 | [Engine](src/projectmind/agent/engine.py) / [stream_lifecycle](src/projectmind/agent/stream_lifecycle.py) / [SubagentDispatchProvider](src/projectmind/agent/subagent_provider.py) の TaskGroup。主/子の終態保存順序は同一ではない |
| 原因分類と書込権 | Executor の _terminal_mapping → [Run repository](src/projectmind/runs/repository.py)。[原因の規則](../../docs/design/run-supervision.md#原因与执行权如何分类)と [outcomes test](tests/worker/test_execution_outcomes.py)を確認し、interrupted だけで user intent を推定しない |
| 取消と終態の commit 順序 | 同 repository の request_cancellation / finalize_execution → [RunService](src/projectmind/runs/service.py)。[提交の判断点](../../docs/design/run-supervision.md#提交时谁决定最终状态)を正本とし、呼出し側は実際に保存された status を使う |
| 主 Session と終態の保存 | [repository_base](src/projectmind/runs/repository_base.py) の PRIMARY query / [execution_outcome](src/projectmind/runs/execution_outcome.py) → [finalization test](tests/runs/test_execution_finalization.py)。子 Session を含む Attempt の query 条件、観測用量、Result と Event/Outbox を確認する |
| 待機/通常 event の取消拒否 | [repository_base](src/projectmind/runs/repository_base.py) の _reject_cancelled_execution ← append_agent_event / [repository_interactions](src/projectmind/runs/repository_interactions.py) / [repository_effects](src/projectmind/runs/repository_effects.py)。[拒否と終態の二つの transaction](../../docs/design/run-supervision.md#等待提交仍是独立边界)を[書込拒否 test](tests/runs/test_cancelled_execution_writes.py)と outcomes test で追う |
| 停止点の制御 | [first event supervision](tests/worker/test_first_event_supervision.py)、[preparation supervision](tests/worker/test_preparation_supervision.py)、[subagent lifecycle](tests/agent/test_subagent_lifecycle.py)。清理を保留した間の保存有無まで確認する |

待機/通常 event の拒否は採番や新規書込より前に行う。service の rollback 後、Executor が別 transaction で lease と取消意図を再検証して終態を保存する。二つの transaction 間で実行権を失った旧 Worker は補書しない。用量は[Event と Session の保存口径](../../docs/design/run-budgets.md#用量现在流向哪里)も確認し、payload の保持だけで台帳更新済みと扱わない。

これらの test は fake service/client と mock SQL session を含む。query/lock の構築確認は、実 DB の並行 commit や複数行の読取、SDK process の退出を証明しない。[停止の受入行列](../../docs/design/run-supervision.md#验收矩阵)に従い、正常終態、待機、disconnect 失敗、重複取消と接管を分けて検証する。接続済み範囲と次の受入は[計画 R07](../../docs/planning/roadmap.md#r07-run-与审计)で確認し、active 登録の削除や空 cost を停止/返金の証拠として使わない。

## 予算と子分析の接続を追う

まず[予算の数値例](../../docs/design/run-budgets.md#一个例子已用占用与可用)で上限・占用・消費を分ける。タイマーは[適用範囲](../../docs/design/run-budgets.md#现有计时器的覆盖范围)、子結果は[現在の信頼境界](../../docs/design/subagents.md#当前返回值的可信边界)へ進む。下表は現在動く経路である。別途ある[内部台帳の実装](#台帳の実装を引き継ぐ)は、まだこの経路へ接続されていない。

| 境界 | 一緒に読む実装と検証 |
| --- | --- |
| 凍結限額と SDK | [RunService](src/projectmind/runs/service.py) → [RunLimits](src/projectmind/agent/domain.py) → [Claude options](src/projectmind/agent/claude.py)。snapshot は残額ではない |
| 用量 event と保存 | [mapper](src/projectmind/agent/engine.py) → [Executor._merge_usage](src/projectmind/worker/executor.py) / [repository_base](src/projectmind/runs/repository_base.py)。[engine test](tests/agent/test_claude_engine.py) と Session/Result を比較し、最新値の保存を累積台帳と扱わない |
| 子 context と分配 | [split_budget](src/projectmind/agent/subagent.py) → [dispatch](src/projectmind/agent/subagent_provider.py) の _derive_child_context。prompt/tools を狭めても父 Schema/Brief は継承されるため、[子指令と出力の責任](../../docs/design/subagents.md#子任务指令与结果的边界)を先に確認する |
| 終端と出力検証 | [SubagentTranscript](src/projectmind/agent/subagent_result.py) → [ResultValidator.validate_context](src/projectmind/agent/result_validation.py)。[Worker startup](src/projectmind/worker/settings.py) は主/子へ同じ validator を注入する。[Executor test](tests/worker/test_agent_run_executor.py) も実 validator で成功・不正結果・Review/fork を検証する |
| stream の所有者と清理 | [stream_lifecycle](src/projectmind/agent/stream_lifecycle.py) → [Engine](src/projectmind/agent/engine.py) の execute/resume/fork。[engine test](tests/agent/test_claude_engine.py) の fake client 解放と、実 process の停止は別に検証する |
| Session と Tool の監査 | [Session recorder](src/projectmind/agent/subagent_sessions.py) → Provider → Gateway。v1 の recorder は必須で、保存と Tool 成功記録は[別の提交段階](../../docs/design/subagents.md#提交顺序与版本兼容)。[Provider test](tests/agent/test_subagent_provider.py) と[実 Gateway 回帰](tests/agent/test_subagent_lifecycle.py)を一緒に読む |
| 主へ返す境界 | [registry](src/projectmind/agent/context_builder.py) → [Gateway](src/projectmind/agent/tool_gateway.py) → [Tool 契約](../contracts/README.md#子分析と用量の契約を読む)。Provider 単体の結果が返却前 Schema を通るかまで確認する |

後続は[子分析の接続順](../../docs/design/subagents.md#开发接续顺序)と[計量・起動・結算の接続順](../../docs/design/run-budgets.md#上线门禁与接线顺序)に従う。v1 は保存済み Session ID を要求する。将来の監査降級と必須の予算台帳を混ぜず、旧 Worker、再 dispatch、Segment/Attempt、用量の遅着を含めて検証する。凍結 Brief に残額を書くだけで共通予算を実装済みとしない。

### 台帳の実装を引き継ぐ

[台帳の現在地](../../docs/design/run-budgets.md#持久账本的当前载体)で保存対象を確認し、[状態と証拠の違い](../../docs/design/run-budgets.md#内部状态不能当作运行证明)を読んでから実装へ進む。進捗と残る受入は[計画 R02](../../docs/planning/roadmap.md#r02-run-统一预算)へ集約する。

| 境界 | コードと検証の入口 |
| --- | --- |
| 内部契約と保存形式 | [budget DTO](src/projectmind/runs/budget.py) → [models](src/projectmind/db/models.py) / [0030](migrations/versions/0030_run_budget_ledger.py)。[単位/型](tests/runs/test_budget.py)と[DDL 対応](tests/db/test_budget_migration.py)を確認する。公開 Schema の追加ではない |
| 勘定・預留・核対 | [repository_budgets](src/projectmind/runs/repository_budgets.py) → [有状態 fake 回帰](tests/runs/test_repository_budgets.py)。親子残額、lock 後の lease、重複報告、未知量と超過を確認する。SDK 計量の正しさは証明しない |
| transaction の所有者 | [budget_store](src/projectmind/runs/budget_store.py) → [提交の回帰](tests/runs/test_budget_store.py)。A の原鍵確認、B の許可を返す時点、C の重複/衝突保存を分ける。repository の flush だけを成功として返さない |
| 実 PostgreSQL | [実 DB テスト](tests/db/test_real_budget_ledger.py)：競合・rollback・終態後結算。専用 DB の作成/削除を伴う fixture であり、通常の文書確認では実行しない。ファイルの存在や skip を成功と数えない |

`new_budget_account` は新 Run 用の構築関数で、既存の RunService はまだ呼び出さない。Worker startup、主 Executor、子 Provider、信頼した計量/停止核対も未接続である。`claim_reconciliation` は内部 lease、`verified_evidence` は根拠の参照鍵にすぎず、サービス認証や process 停止を自動検証する API ではない。実装を重複作成せず、[公開前の門禁](../../docs/design/run-budgets.md#上线门禁与接线顺序)を満たして接続する。

## 承認から外部変更まで追う

まず[批准と外部結果の違い](../../docs/design/repository-effects.md#先分清四种事实)を読む。DB の transaction と外部 I/O を一つの成功にまとめず、[段階回执の修正要求](../../docs/design/repository-effects.md#阶段回执与不确定结果)へ接続する。

| 変更する境界 | コードと回帰 |
| --- | --- |
| 提案の内容と許可される操作 | [catalog](src/projectmind/effects/catalog.py) → [proposal](src/projectmind/effects/proposal.py) / [repository validator](src/projectmind/effects/repository_write.py) → [tests/effects](tests/effects/)。登録と事前許可の判断を複製しない |
| 精確批准と保存 | [effects route](src/projectmind/api/routes/effects.py) → [RunService](src/projectmind/runs/service.py) → [repository_effects](src/projectmind/runs/repository_effects.py) の decide → [decision test](tests/runs/test_effect_decision_service.py) |
| claim・実行・finalize・再派発 | [EffectService](src/projectmind/effects/service.py) → 同 repository の claim/finalize/recover → [Effect Executor](src/projectmind/worker/effects.py) → [executor test](tests/worker/test_effect_executor.py)。Run の Executor とは別の監督境界 |
| 遠端の競合・replay・PR | [repository Provider](src/projectmind/effects/repository_effect.py) → [command client](src/projectmind/agent/repository_client.py) / [forge](src/projectmind/effects/forge.py)、[Redmine](src/projectmind/effects/redmine.py) → Provider 回帰。file 内容の一致と原実行の証明を分ける |

現在の Provider test はローカル Git/SVN と fake transport、repository test は mock の範囲を含む。実 DB の lease 競合、遠端応答喪失、部分成功回执を証明したとは扱わない。Provider の旧「branch のみ」注釈と repository.write request Schema は現行 direct/SVN と未同期であり、後続変更では[契約の接続先](../contracts/README.md#外部変更の契約を読む)も確認する。

## Schedule の認領と回写を追う

まず[規則・発火・Run の具体例](../../docs/design/task-scheduling.md#一个例子规则触发与执行分别看)で last_* と Run 状態を分ける。同 Task 全体を走査するコードや、公開 row_version による原子的 CAS が実装済みとは仮定しない。

| 変更する境界 | コードと検証の入口 |
| --- | --- |
| 候補時刻と認領 | [Worker tick](src/projectmind/worker/settings.py) の trigger_due_schedules → [service](src/projectmind/schedules/service.py) の plan_occurrence / _claim → [repository](src/projectmind/schedules/repository.py) の claim。認領後 crash の回復は別に確認する |
| 原 Run の確認と重複 | 同 service の _create_scheduled_run / _overlapping_run → [RunService](src/projectmind/runs/service.py) → [replay test](tests/schedules/test_schedule_replay.py)。現行の重複確認は本 Schedule の last_run_id のみ |
| 定義・状態・結果の更新 | repository の update_definition / set_status / record_outcome → [モデル](src/projectmind/db/models.py)。[API test](tests/api/test_schedule_api.py) の fake conflict と実 DB の競争を分ける |

後続は[認領記録と実行権](../../docs/design/task-scheduling.md#认领记录与恢复权限)、[原子的更新](../../docs/design/task-scheduling.md#配置并发与暂停)、[一度だけの計数](../../docs/design/task-scheduling.md#结算计数与未知结果)を満たす。[cron test](tests/schedules/test_cron.py)、[planning test](tests/schedules/test_schedule_planning.py)、[tick test](tests/worker/test_schedule_tick_job.py) の成功は、DB transaction 間の中断・lease 接管・履歴移行の証拠にはしない。公開形状は[Schedule 契約](../contracts/README.md#schedule-の公開契約を読む)へ進む。

## 生成 module の前置実装を読む

最初に[表示故障の例](../../docs/design/generated-modules.md#一个例子图表坏了任务没有失败)を読み、業務版の変更と表示だけの回退を分ける。既存資産を再利用しても、純関数の成功を配信許可として扱わない。

| 担当する境界 | 既存の入口と回帰 |
| --- | --- |
| 業務 module | [compositions route](src/projectmind/api/routes/compositions.py) / [service](src/projectmind/compositions/service.py)。精確 SkillVersion の組合設定であり、bundle を配信しない |
| 生成版と公開条件 | [domain](src/projectmind/modules/domain.py) / [model](src/projectmind/db/models.py) / [migration 0026](migrations/versions/0026_frontend_module_versions.py) → [version test](tests/modules/test_module_versions.py)。CSP の文字列 assertion はブラウザの証拠ではない |
| ソース・依存・取得先 | [static_analysis](src/projectmind/modules/static_analysis.py) / [build_plan](src/projectmind/modules/build_plan.py) → [静的 test](tests/modules/test_module_static_analysis.py) / [build plan test](tests/modules/test_build_plan.py)。実 installer、builder、egress 制御は未接続 |
| 公開契約への入口 | [Manifest Schema](../contracts/runtime-manifest/v1alpha1.schema.json) / [runtime_defaults](src/projectmind/skills/runtime_defaults.py)。frontend_module は null のみで、内部の Module API 定数を公開済み Host protocol と扱わない |

後続は[現在のコードと契約](../../docs/design/generated-modules.md#现有代码与公开契约)から、[構築の提交](../../docs/design/generated-modules.md#构建输入与结果的提交)と[内容 identity](../../docs/design/generated-modules.md#内容身份与历史兼容)へ進む。同じ source の依存更新が現行一意制約に衝突する点、CSP の暫定値、独立した表示選択の欠落を先に解決する。状態は[計画 R04](../../docs/planning/roadmap.md#r04-生成模块)、公開面は[生成 module の契約入口](../contracts/README.md#生成-module-と既存-module-api-を分ける)へ集約する。

## 運用 CLI と停止境界を確認する

先に[公開・移行](../../docs/operations/deployment.md)または[復元](../../docs/operations/backup-recovery.md)から操作の目的を選ぶ。下表は実装を探すための案内であり、実行許可や復元結果ではない。

| 入口 | 責務と副作用 |
| --- | --- |
| [preflight.py](src/projectmind/ops/preflight.py) / [test](tests/ops/test_preflight.py) | DB head と Redis PING を確認する。blob・KEK・認証・Worker・外部結果を検証しない |
| [bootstrap_admin.py](src/projectmind/ops/bootstrap_admin.py) / [bootstrap](src/projectmind/auth/bootstrap.py) | 初期 ADMIN を作成する書込 CLI。既存 ADMIN を追加・修復する CLI ではない。Make の同名 target と別物 |
| [smoke.py](src/projectmind/ops/smoke.py) / [client test](tests/ops/test_smoke_client.py) | 専用 Project に Run/Evaluation を作り、model と取消を検証する。通信 client の mock 回帰は実 smoke ではない |
| [rotate_secrets.py](src/projectmind/ops/rotate_secrets.py) | MANAGED 材料を再暗号化する書込 CLI。解読の只読診断ではなく、旧 backup の KEK 保持も別途必要 |
| [WorkerSettings](src/projectmind/worker/settings.py) / [dispatch test](tests/worker/test_worker_dispatch.py) | relay と job/cron の登録。dispatch=false が制限するのは新しい Run/Effect Outbox 配送だけ |
| [schedule tick test](tests/worker/test_schedule_tick_job.py) / [recovery test](tests/worker/test_effect_recovery_job.py) | cron の service 呼出しと結果集計の fake 回帰。全 Worker の停止や実 DB の静止を証明しない |

復元中に Worker を起動すると、古い queue job、Schedule、recovery、Skill 解釈も対象になり得る。[dispatch の具体例](../../docs/operations/deployment.md#一个例子关闭-dispatch-后仍有工作)を先に確認し、機能設定を全局停止の保証へ読み替えない。運用の改善時は job 入口と既存仕事を含めた[受入条件](../../docs/operations/deployment.md#后续开发约束与验收)まで追う。

## 起動と検証

環境構築・DB 接続設定は[ローカル開発](../../docs/development/local-development.md)を参照する。依存導入後、`backend/` で API を起動する。

```bash
python3 -m uvicorn projectmind.api.main:app --reload --port 8000
```

同じ設定で Worker を別 process として起動する場合：

```bash
arq projectmind.worker.settings.WorkerSettings
```

Run/Effect の新しい Outbox dispatch は明示有効化が必要だが、false でも既存 job や cron は停止しない。DB/Redis/object storage が別途必要であり、production 相当の構成は[Compose 起動](../../docs/operations/quickstart.md)に従う。

Backend の変更は Ruff/Mypy/Pytest と関連する契約/SDK probe を確認する。実 DB test の skip は未検証として報告する。
