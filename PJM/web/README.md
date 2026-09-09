# ProjectMind Web

React + TypeScript の application shell。ページの責務と現在の hash route は [Workspace 設計](../../docs/design/workspace.md)、次期の流れ表示は [Task Flow 設計](../../docs/design/task-flow.md)を参照する。

[画面から選ぶ](#画面から実装へ進む) · [ログイン](#ログインと書込失敗を切り分ける) · [アカウント管理の準備](#アカウント管理を接続する) · [結果と評価](#結果と人工評価を接続する) · [生成表示](#生成表示と業務-module-を分ける) · [用量](#子分析と用量を読む) · [調度](#調度の保存と管理を引き継ぐ) · [開発と検証](#開発と検証)

## 構成と境界

| Directory | 責務 |
| --- | --- |
| [src/pages](src/pages/) | 概览、Skills、Project、Workspace、History、Task Center、Documents、Resources |
| [src/components](src/components/) | 入力・実行・結果・待機・承認の共有 UI |
| [src/api](src/api/) | 資源別 client と response validator；画面は index.ts barrel から import |
| [src/lib](src/lib/) | routing、SSE projection、task draft など画面非依存 logic |
| [src/hooks](src/hooks/) | React の非同期調整と cleanup；純粋な判定規則は lib へ分離 |
| [src/lib/i18n](src/lib/i18n/) | zh/ja/en catalog；React 側は useMessages 経由 |
| [src/styles](src/styles/) / [src/assets](src/assets/) | theme・responsive layout・自己保持 font |

Task Center は選択と調度、Workspace は一つの Run の lifecycle を担当する。即時実行と Schedule の作成は TaskLaunchFields/taskDraft を共有し、実入力・文書範囲を確認する。HTTP/Problem は api/http.ts、mutation は csrfToken と X-CSRF-Token を使用する。

Session token を Web storage に置かない。Project/Run/Task ID はサーバーが返した値を使い、UUID や過去 Run の関連を画面で再導出しない。

## 画面から実装へ進む

| 変更したい体験 | 入口と共有 logic |
| --- | --- |
| ログイン・現在の Session・登出 | [App](src/App.tsx) → [LoginPage](src/pages/LoginPage.tsx) / [auth client](src/api/auth.ts)。Server の token を memory に保持し、Project 選択や Run の retry と分ける |
| Project の選択・編集・アーカイブ・削除 | [Project 管理の接続案内](#project-の切替と管理を追う) → ProjectsPage / projectContext。メンバー API の存在を画面入口と読み替えない |
| Skill の Preview・公開・Project 有効化 | [SkillsPage](src/pages/SkillsPage.tsx) → [skills API / validator](src/api/skills.ts)。組合の表示設定は [ProjectsPage](src/pages/ProjectsPage.tsx)と分ける |
| タスク選択・即時実行・時刻起動 | [TasksPage](src/pages/TasksPage.tsx)、[TaskLaunchFields](src/components/TaskLaunchFields.tsx)、[taskDraft](src/lib/taskDraft.ts)、[ScheduleDialog](src/components/ScheduleDialog.tsx) |
| 文書の upload・tree・preview・削除 | [文書管理の接続案内](#project-文書の管理を追う) → DocumentManagerPanel / documents client。現在の資産一覧と Run の凍結一覧を分ける |
| 文書の選択と凍結範囲の表示 | [DocumentSourceField](src/components/DocumentSourceField.tsx) / [documentSelection](src/lib/documentSelection.ts) → [runs API](src/api/runs.ts) / [runResources validator](src/api/runResources.ts) → [RunDocumentSnapshots](src/components/RunDocumentSnapshots.tsx) |
| 一つの Run の観察・再接続 | [WorkspacePage](src/pages/WorkspacePage.tsx)、[agentStream](src/lib/agentStream.ts)、[runReplay](src/lib/runReplay.ts)、[events API](src/api/events.ts) |
| 作成応答が失われた後の原要求確認 | [runSubmission](src/lib/runSubmission.ts) → [useRunSubmission](src/hooks/useRunSubmission.ts) → [RunSubmissionPanel](src/components/RunSubmissionPanel.tsx) → 確認後は Workspace の既存 lifecycle |
| 待処理 Run の発見と回答・承認 | [PendingActionsPanel](src/components/PendingActionsPanel.tsx) は発見、[RunResultPanel](src/components/RunResultPanel.tsx) は回答/承認と結果表示 → [runs API](src/api/runs.ts) / [effects API](src/api/effects.ts) |
| 原結果と人工修訂 | [結果・評価の接続案内](#結果と人工評価を接続する) → RunResultPanel / evaluations client。通常回答や批准とは別の提出 protocol |
| 画面横断の用語・三語表示 | [messages catalog](src/lib/i18n/messages.ts)、[zh](src/lib/i18n/zh.ts) / [ja](src/lib/i18n/ja.ts) / [en](src/lib/i18n/en.ts) |

表示は server の事実を投影する。送信開始を回答受理、APPROVED を外部変更完了、子分析への予算分配を実消費と読み替えない。判定の正本は [Workspace](../../docs/design/workspace.md)、[Effect](../../docs/design/repository-effects.md)、[予算](../../docs/design/run-budgets.md)を参照する。

### Project の切替と管理を追う

[ProjectsPage](src/pages/ProjectsPage.tsx) → [projects client](src/api/projects.ts) / [App](src/App.tsx) / [projectContext](src/lib/projectContext.ts)が入口。[失効リンクの設計](../../docs/design/project-lifecycle.md#项目选择与失效链接)と現行 fallback を照合する。明示された無効 Project を別の Project に置き換えても安全とは限らず、旧草稿・Run・遅延応答を新 context へ引き継がない。

現行ページは作成/編集、アーカイブ/復元/削除と業務 module を扱う。メンバーの list/add/remove は client まで存在するが、画面から未使用。[アーカイブと削除](../../docs/design/project-lifecycle.md#归档的实际边界)の結果を区別し、404 を別 Project への自動移動、削除 timeout を自動再送に変換しない。

[projectContext test](tests/lib/projectContext.test.ts)は現行 fallback、[ProjectsPage test](tests/pages/ProjectsPage.test.tsx)は純粋 logic と静的描画、[API test](tests/api/authProjects.test.ts)は mock 応答の検査である。実 component の二重 submit、actor/Project 切替、StrictMode と遅延応答は別途検証する。後続の metadata 競合 UI は[版と並行性](../../docs/design/project-lifecycle.md#并发修改不能只看有无行锁)から契約と同時に接続し、User の row_version を流用しない。

### Project 文書の管理を追う

[削除と再アップロードの例](../../docs/design/document-lifecycle.md#一个例子列表消失不等于清理完成)を読み、[DocumentsPage](src/pages/DocumentsPage.tsx) → [DocumentManagerPanel](src/components/DocumentManagerPanel.tsx) → [documents client](src/api/documents.ts)へ進む。現在の API は全件 documents 配列であり、directory は folder の投影である。Run の選択/履歴を現在の tree から再構築しない。

目録 upload は一件ずつ実行し、失敗後も次へ進む。進捗は処理件数で成功数ではない。[未知結果の設計](../../docs/design/document-lifecycle.md#页面与结果未知)に従い、原 file と context、二重 submit、確認待ちの切替、古い一覧/preview/成功応答を検証する。既存の controller abort は server rollback や現在 request の一致を保証しない。

[読取と preview](../../docs/design/document-lifecycle.md#读取下载与预览)の上限・MIME・外部資源の境界を守る。HTML の空 sandbox を生成 Host やネットワーク隔離として流用しない。API validator は基礎型の確認に留まるため、[公開契約](../contracts/README.md#project-文書の保存と読取を読む)と対象 Project/ID、数値/日時/hash の検査を接続する。

[API test](tests/api/documents.test.ts)と [component test](tests/components/DocumentManagerPanel.test.tsx)は mock/純粋 logic/静的描画を確認する。実 component の部分成功・未知・actor/Project 切替・三語/keyboard/狭い画面は別途検証し、input 選択の browser 回帰で文書管理全体を検証済みと扱わない。

### ログインと書込失敗を切り分ける

[二つのページの例](../../docs/design/authentication.md#一个例子同一账号打开两个页面)を先に確認する。現在の v2 は同じ会話の読取で CSRF を変えず、[auth client](src/api/auth.ts)は token を opaque string として受け取り、各 auth request に no-store を指定する。別ログインによる cookie 変更、失効、actor/Project 切替は別の問題であり、他 tab への身份同期や共通 retry 制御は無い。403 を一律「未ログイン」と解釈せず、[認証の分診](../../docs/operations/runbook.md#认证故障的只读分诊)へ進む。

後続の会話再確認でも原要求の identity を保ち、token 再取得を業務 mutation の自動再送へ結び付けない。[auth / Project API test](tests/api/authProjects.test.ts)と[mutation CSRF test](tests/api/mutationCsrf.test.ts)は送信/解析・no-store・安定値の受取を確認するもので、実 cookie・二つの tab・proxy・Session 失効を検証していない。Web で prefix や HKDF を再実装せず、[認証の契約入口](../contracts/README.md#認証と-secret-の契約を読む)から Server と同時に変更する。

ログイン前の 429/503 は[入口防護の client 責任](../../docs/design/login-protection.md#公开响应与客户端责任)で判断する。実装を追う順序は次のとおりで、待機秒数の構文と Login 固有の範囲判定を分ける。

| 入口 | 責務と対応 test |
| --- | --- |
| [HTTP client](src/api/http.ts) | status/code と合法な Retry-After 秒数を ApiProblemError に保持 → [HTTP test](tests/api/http.test.ts) |
| [auth client](src/api/auth.ts) | challenge の成功後だけ password POST、await の前後で abort を確認 → [auth / Project test](tests/api/authProjects.test.ts) |
| [loginFeedback](src/lib/loginFeedback.ts) | Login 範囲内の待機、503、401/403 を catalog へ投影 → [三語 test](tests/lib/loginFeedback.test.ts) |
| [LoginPage](src/pages/LoginPage.tsx) | activeRequest ref で二重 submit を拒否、離頁で abort、現在の request だけを App へ通知 → [browser runner](tests/browser/check_login.py) は実 component と mock API を使用 |

待機は静的な案内であり、429 後の button を秒数ぶん無効にする timer は無い。password 自動再送や challenge polling も行わない。503 は汎用の利用不可表示で、proxy の非 JSON 応答を Redis 障害と断定しない。提出時の例外は catalog へ変換するが、App の初回 Session 読取から渡る initialError は別の文字列経路である。

[提出・離頁・結果不明](../../docs/design/login-protection.md#提交离页与结果未知)の区別を守る。[画面の browser 回帰](../../docs/development/local-development.md#ログイン画面のブラウザ回帰)は同じ DOM tick の submit、StrictMode、unmount 後の callback、三語・キーボード・窄屏を確認する。fixture の cookie と mock 応答は実 DB の会話や HTTPS proxy の証拠ではない。password 送信後の abort は Server の Session を巻き戻さず、cleanup で自動 logout しない。限流を理由に既存会話や元の業務要求を勝手に破棄しない。

Resources の MANAGED 入力は ADMIN が新しい原値を一時的に扱う画面であり、保存済み Secret を読み返す画面ではない。[明文の境界](../../docs/design/secret-storage.md#明文与威胁边界)に従い、保存成功を外部資源への接続成功と表示しない。

### 待処理と外部結果を表示する

まず[普通回答・批准・結果評価](../../docs/design/user-interactions.md#先分清三种人工参与)を区別する。InteractionCard は [runs client](src/api/runs.ts)へ question version と回答を送り、Evaluation は別の [evaluations client](src/api/evaluations.ts)を使う。REVIEW の回答と Result の修訂を同じ保存操作にしない。

現在の InteractionCard は submit ごとに新 key を作り、state による button 無効化と unmount abort のみを持つ。[結果不明と現在要求の確認](../../docs/design/user-interactions.md#答复界面与结果未知)は未完成であり、同じ DOM tick の二重 submit、abort を無視して届く成功、actor/Run 切替後の callback を別途防ぐ。原 payload/key の確認を Run 作成の retry と混同しない。

410 は回答を採用しなかったことを示すが、過期と次 Segment が既に commit されている場合がある。重放 response の Run status も QUEUED 固定ではない。[普通回答の状態表示](../../docs/design/workspace.md#普通答复与续行状态)から対応し、[RunResultPanel test](tests/components/RunResultPanel.test.tsx)の静的描画だけで実時序や多画面を検証済みと扱わない。

批准要求の応答喪失は[専用の状態設計](../../docs/design/workspace.md#审批请求与执行结果)を確認する。現行 ChangeProposalCard はクリックごとに新 key を生成するため、原決定の安全な再確認は未完成。Run 作成の再送実装があることを、このカードにも実装済みという根拠にしない。[controlledEffects API test](tests/api/controlledEffects.test.ts)と[RunResultPanel test](tests/components/RunResultPanel.test.tsx)から接続し、三語・actor/Project 切替と browser 上の応答喪失まで検証する。

### 結果と人工評価を接続する

[結果の四つの事実](../../docs/design/results-evaluation.md#先分清四种事实)を [RunResultPanel](src/components/RunResultPanel.tsx) と [evaluations client](src/api/evaluations.ts)へ投影する。AI の原値・人工提案・理由は並列表示し、confidence を正解率へ変換しない。現行はモデルの効果摘要と平台の Proposal/Effect を別々に表示しており、項目ごとの照合は無い。後続では出所を明示して平台の記録を優先し、未検証 Artifact を download link にしない。

[原値の指針](../../docs/design/results-evaluation.md#修订指向哪份原值)は detail.result.data が根であり、API 全体ではない。現行 form は一つの修訂、API は複数を受理する。JSON として読める提案入力は型付き値、読めない入力は文字列になるが、保存しても元 Result の Schema へ自動適用されない。

[評価の結果不明と切替](../../docs/design/results-evaluation.md#提交未知与界面责任)に従って、同時 submit、一覧の遅延上書き、旧 actor/Project/Run/Result の応答を守る。現在の state 無効化と unmount abort は十分な保証ではなく、POST に幂等契約も無い。履歴に同じ文字があっても原要求の成功確認と断定しない。

回帰は [controlledEffects](tests/api/controlledEffects.test.ts)の評価 API case と [RunResultPanel](tests/components/RunResultPanel.test.tsx)の静的描画から始める。unknown/401/403/409 の表示、正常・歴史・異常形状、三語と実ブラウザの操作時序は別途確認し、静的 markup の通過だけで完了としない。

### Skill と計画を表示する

Skill の compatibility、gate_passed、version status、Project enablement、task readiness は[別の判断](../../docs/design/skill-contract.md#发布与就绪的判断顺序)として表示する。公開しても全 Project へ自動で有効化せず、組合の編集を版の公開と見なさない。現行の同版再有効化は拒否されるため、成功する toggle として案内しない。[回退設計](../../docs/design/skill-contract.md#112-可审计的重新启用与回滚)を実装する際は精確版の確認・競合・監査と三語を同期する。

後続の Flow はこの既存投影へ接続する。[計画 identity と layout](../../docs/design/task-flow.md#计划身份与显示布局)を分け、SDK step_id や同じ表示名から計画ノードの完了を推測しない。公開 Flow 契約はまだ無く、先行する読取 preview のために API response や ViewSpec へ仮 field/component を追加しない。

### 入力準備と公開データを表示する

`RUNNING` も入力準備完了やモデル開始を保証しない。`document_snapshots` の `FROZEN` は作成時の選択の検証状態、内部入力回执の `READY` は別の事実であり、現行 API に公開されていない状態を Web が合成しない。[実行開始の境界](../../docs/design/agent-runtime.md#74-从领取到模型启动的边界)に従い、Session/Event と選択一覧を区別して表示する。準備 timeout の追加を理由に UI 側で秒数から完了率を作らず、取消応答を実行停止の証拠にしない。

公開 field の追加は TypeScript の型だけで終えず、実行時 validator、mock/fixture、画面の正常・歴史・異常状態と三語を同期する。[契約変更ガイド](../../docs/development/contract-workflow.md)に互換性と検証順を集約する。document_snapshots の欠落は空集合へ変換せず契約エラーとする。以前の Web 回帰を現行ファイルの成功証拠にせず、検証範囲は[計画](../../docs/planning/roadmap.md#13-当前执行状态)で確認する。

### 作成要求を再確認する

原要求の確認 UI は [Run 作成と幂等](../../docs/design/run-creation.md#提交结果未知时的界面责任)を正本とする。現行 Workspace は編集可能な草稿と送信済み payload/key を分け、結果不明時に原要求を再送する。新規実行は明示確認後に別 key を発行する。状態はページ内の memory-only であり、refresh・離頁・actor/Project 切替後の自動復元はしない。

HTTP 待機 timeout と Run 取消は別操作である。受信できなかったことだけを理由に、サーバー側の作成や実行が取り消されたと表示しない。

### 取消と最終状態を表示する

取消操作の意味は[取消後の事実](../../docs/design/run-supervision.md#一个例子点击取消之后)を確認する。cancel 応答は要求の処理、terminal RUN_SNAPSHOT は業務状態であり、process 退出や予算結算を保証しない。[停止理由の互換性](../../docs/design/run-supervision.md#兼容与开发接续)に従い、SESSION_INTERRUPTED の名前から user 操作を推測せず、未公開の停止確認 badge を作らない。

`runReplay.applicableRunSnapshot` は RUN_SNAPSHOT の status / row_version を投影する。個別の interrupted event から Run を CANCELLED へ変えない。取消より先に終態が確定した場合は元の結果を維持し、`409 run_not_cancellable` を「取消成功」に置き換えない。[取消と終態の契約](../contracts/README.md#run-の取消と終態を読む)から response、event、API error の同期先へ進む。

## アカウント管理を接続する

Backend の管理 route、Schema/example と [users client](src/api/users.ts)は存在するが、アカウント画面・hash route は未接続。工作副本には [UserAccountPanel](src/components/UserAccountPanel.tsx)、[UserSecurityEvents](src/components/UserSecurityEvents.tsx)と [useUserRequest](src/hooks/useUserRequest.ts)もあるが、App から使用されておらず本輪は検証していない。既存部品を無いものとして再作成せず、利用可能な画面とも扱わない。[アカウント画面の責務](../../docs/design/user-lifecycle.md#生效与界面)は Project に依存しない本人操作と ADMIN の組織管理を分ける。UI language / Project preference の画面や ProjectMember 管理を改密・失効の入口に見立てない。

[管理契約の対応表](../contracts/README.md#ユーザー管理の公開面を準備する)から既存 Schema と route を照合し、未同期の OpenAPI を解消する。専用 client は [HTTP 共通処理](src/api/http.ts)、response validator、[API barrel](src/api/index.ts)へ接続済みで、[client test](tests/api/users.test.ts)もある。再実装せず、[App](src/App.tsx) / [routing](src/lib/routing.ts)、[messages](src/lib/i18n/messages.ts)と三語 catalog へ進む。client の abort/対象 ID 検査を、画面の現在 request/actor 判定や本人 response の照合まで完了した証拠と扱わない。

検索結果は server の items/total/limit/offset を保持し、先頭 page の client filter で全件管理を代用しない。offset paging は一覧全体の固定 snapshot ではなく、更新中の件数変化も表示上考慮する。詳しい意味は[版と問い合わせ](../../docs/design/user-lifecycle.md#版本查询与公开数据)へ集約する。

[成功・拒否・未知](../../docs/design/user-lifecycle.md#生效与界面)をそれぞれ表示し、password の誤りを全体 logout に、abort を rollback に置き換えない。再認証後の自動再送はせず、actor 変更時の旧応答破棄・server paging・三語/keyboard/窄屏を実 component で検証する。実 Server の持久失効や多 tab は別の[受入条件](../../docs/design/user-lifecycle.md#开发接续与验收)である。

## 生成表示と業務 module を分ける

[modules client](src/api/modules.ts)と[既存 API test](tests/api/modules.test.ts)は SkillComposition の管理用であり、生成 bundle の client ではない。DocumentManagerPanel の HTML preview も script 無効の sandbox で、生成画面の Host へ転用しない。[三つの概念](../../docs/design/generated-modules.md#先分清三种模块与预览)から責任を確認する。

生成 iframe/SDK/Host は未実装。後続は[掛載と古いメッセージ](../../docs/design/generated-modules.md#挂载切换与晚到消息)の規則に従い、actor/Project/Task/Run 切替、重載、unmount、fallback で channel と購読を破棄する。現在の CSRF を子へ渡さず、Host を任意 API proxy にしない。[契約の同期先](../contracts/README.md#生成-module-と既存-module-api-を分ける)を確認してから実装する。

[表示選択と全体停止](../../docs/design/generated-modules.md#展示选择与全局停用)は別操作。表示失敗時は同じ Result/Evidence を standard で示し、入力草稿と focus を保つ。Run の retry や SkillComposition の版変更を自動実行しない。正常描画だけでなく、旧 iframe の遅延応答、禁止メッセージ、三語・キーボード・窄屏をブラウザで確認する。

## 子分析と用量を読む

[RunResultPanel.collectSubagentDispatches](src/components/RunResultPanel.tsx) は subagent-dispatch Evidence の outcome と分配 metadata を表示し、モデルや DB を直接再判定しない。[一組の結果の読み方](../../docs/design/subagents.md#一个例子完成的是哪一层)に従い、Tool の success を全 branch の成功や主 Run の完了として表示しない。

現行 v1 は保存済み Session ID が必須で、Session 保存を確認できない場合に成功 Evidence があるとは仮定しない。[現在の実装境界](../../docs/design/subagents.md#当前返回值的可信边界)を踏まえ、Web だけで text の有無から outcome を修正したり、未実装の監査欠落状態を合成したりしない。将来の降級表示は[版互換](../../docs/design/subagents.md#提交顺序与版本兼容)と実 Gateway の出力を同期する。

予算の「1 面あたり上限」は消費でも残額でもない。[用量の読み分け](../../docs/design/run-budgets.md#用量现在流向哪里)と[数値例](../../docs/design/run-budgets.md#一个例子已用占用与可用)を参照し、未確定を 0 に変換しない。後続の公開投影は [契約入口](../contracts/README.md#子分析と用量の契約を読む)から接続する。結果の完成、Session 追跡の欠落、予算の未結算を一つの badge に合成しない。

Backend の[内部台帳](../../docs/design/run-budgets.md#持久账本的当前载体)には保存 DTO があるが、Run detail へ残額が公開されたわけではない。START_INTENT や SETTLED は内部預留の状態であり、Run の開始/完了 badge に転用しない。公開投影と履歴互換が揃うまで、Web が Session の usage から代用残額を計算しない。

[RunResultPanel test](tests/components/RunResultPanel.test.tsx) は固定 Evidence の表示検証であり、実 engine の失敗判定や予算残額を証明しない。変更時は正常/部分失敗/欠落/未知・三語を同期し、既存の精確 ID と server の事実を投影する責任を保つ。

## 調度の保存と管理を引き継ぐ

[規則・発火・Run の具体例](../../docs/design/task-scheduling.md#一个例子规则触发与执行分别看)を表示の基準にする。run_count は成功数ではなく、last_run_at と last_run_id も同一 occurrence を指すとは限らない。停止操作は在途 Run の取消ではない。

接続は [ScheduleDialog](src/components/ScheduleDialog.tsx) → [API client](src/api/schedules.ts) → [TasksPage.buildRows](src/pages/TasksPage.tsx) の順で読む。現在は先頭 100 件を取得して TaskCatalog の card に結合しているため、件数超過や精確 Task の失効で Schedule が見えなくなり得る。管理要件は[保存後の入口](../../docs/design/task-scheduling.md#保存后的管理入口)を正本とし、「見つからないので新規作成」と案内しない。

編集は API/client のみで画面入口が無い。後続は Project 単位の paging、失効した task の表示、原設定を保持する編集/競合 UI、[時間入力と表示](../../docs/design/task-scheduling.md#时间输入与展示的边界)を接続する。[TasksPage test](tests/pages/TasksPage.test.tsx) の純粋な結合/静的描画と [API test](tests/api/schedules.test.ts) の成功を、100 件超・時区差・実保存競争のブラウザ受入とは扱わない。

## 開発と検証

Node.js 26 と pnpm 11.7.0 を使用する。install、API proxy、三語確認と検証コマンドは[ローカル開発](../../docs/development/local-development.md#web)に集約する。

変更時は typecheck、Vitest、build と対象 UI のブラウザ確認を行う。generated iframe/Host API と Task Flow は未実装なので、API client の型や設計だけで利用可能と判断しない。[AGENTS.md](../AGENTS.md) の strict type、JSDoc、Effect cleanup 規約に従う。

[ログイン client の局部回帰](../../docs/development/local-development.md#ログイン-client-の局部回帰)は実 LoginPage を描画しない。[専用 browser 回帰](../../docs/development/local-development.md#ログイン画面のブラウザ回帰)は実 component と fixture cookie を使う。この二段階と、実 Server の会話/多 tab/HTTPS の受入を区別し、現在の証拠は[計画](../../docs/planning/roadmap.md#当前证据怎么用)で確認する。

[原要求確認のブラウザ回帰](../../docs/development/local-development.md#原要求確認のブラウザ回帰)は、実 Workspace と mock API で応答喪失・二重クリック・context 切替・三語/キーボードを検証する。通常の Vitest とは別に実行し、実 DB の幂等性や全画面の受入完了とは区別する。

[文書選択のブラウザ回帰](../../docs/development/local-development.md#文書範囲と調度入力のブラウザ回帰)は、即時/調度で送信した input/sources と Run の凍結表示を確認する。両 script は同じローカル harness を使うが、検証している責任は異なる。
