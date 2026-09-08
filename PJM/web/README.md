# ProjectMind Web

React + TypeScript の application shell。ページの責務と現在の hash route は [Workspace 設計](../../docs/design/workspace.md)、次期の流れ表示は [Task Flow 設計](../../docs/design/task-flow.md)を参照する。

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
| Skill の Preview・公開・Project 有効化 | [SkillsPage](src/pages/SkillsPage.tsx) → [skills API / validator](src/api/skills.ts)。組合の表示設定は [ProjectsPage](src/pages/ProjectsPage.tsx)と分ける |
| タスク選択・即時実行・時刻起動 | [TasksPage](src/pages/TasksPage.tsx)、[TaskLaunchFields](src/components/TaskLaunchFields.tsx)、[taskDraft](src/lib/taskDraft.ts)、[ScheduleDialog](src/components/ScheduleDialog.tsx) |
| 文書の選択と凍結範囲の表示 | [DocumentSourceField](src/components/DocumentSourceField.tsx) / [documentSelection](src/lib/documentSelection.ts) → [runs API](src/api/runs.ts) / [runResources validator](src/api/runResources.ts) → [RunDocumentSnapshots](src/components/RunDocumentSnapshots.tsx) |
| 一つの Run の観察・再接続 | [WorkspacePage](src/pages/WorkspacePage.tsx)、[agentStream](src/lib/agentStream.ts)、[runReplay](src/lib/runReplay.ts)、[events API](src/api/events.ts) |
| 作成応答が失われた後の原要求確認 | [runSubmission](src/lib/runSubmission.ts) → [useRunSubmission](src/hooks/useRunSubmission.ts) → [RunSubmissionPanel](src/components/RunSubmissionPanel.tsx) → 確認後は Workspace の既存 lifecycle |
| 待処理 Run の発見と回答・承認 | [PendingActionsPanel](src/components/PendingActionsPanel.tsx) は発見、[RunResultPanel](src/components/RunResultPanel.tsx) は回答/承認と結果表示 → [runs API](src/api/runs.ts) / [effects API](src/api/effects.ts) |
| 画面横断の用語・三語表示 | [messages catalog](src/lib/i18n/messages.ts)、[zh](src/lib/i18n/zh.ts) / [ja](src/lib/i18n/ja.ts) / [en](src/lib/i18n/en.ts) |

表示は server の事実を投影する。送信開始を回答受理、APPROVED を外部変更完了、子分析への予算分配を実消費と読み替えない。判定の正本は [Workspace](../../docs/design/workspace.md)、[Effect](../../docs/design/repository-effects.md)、[予算](../../docs/design/run-budgets.md)を参照する。

批准要求の応答喪失は[専用の状態設計](../../docs/design/workspace.md#审批请求与执行结果)を確認する。現行 ChangeProposalCard はクリックごとに新 key を生成するため、原決定の安全な再確認は未完成。Run 作成の再送実装があることを、このカードにも実装済みという根拠にしない。[controlledEffects API test](tests/api/controlledEffects.test.ts)と[RunResultPanel test](tests/components/RunResultPanel.test.tsx)から接続し、三語・actor/Project 切替と browser 上の応答喪失まで検証する。

Skill の compatibility、gate_passed、version status、Project enablement、task readiness は[別の判断](../../docs/design/skill-contract.md#发布与就绪的判断顺序)として表示する。公開しても全 Project へ自動で有効化せず、組合の編集を版の公開と見なさない。現行の同版再有効化は拒否されるため、成功する toggle として案内しない。[回退設計](../../docs/design/skill-contract.md#112-可审计的重新启用与回滚)を実装する際は精確版の確認・競合・監査と三語を同期する。

後続の Flow はこの既存投影へ接続する。[計画 identity と layout](../../docs/design/task-flow.md#计划身份与显示布局)を分け、SDK step_id や同じ表示名から計画ノードの完了を推測しない。公開 Flow 契約はまだ無く、先行する読取 preview のために API response や ViewSpec へ仮 field/component を追加しない。

`RUNNING` も入力準備完了やモデル開始を保証しない。`document_snapshots` の `FROZEN` は作成時の選択の検証状態、内部入力回执の `READY` は別の事実であり、現行 API に公開されていない状態を Web が合成しない。[実行開始の境界](../../docs/design/agent-runtime.md#74-从领取到模型启动的边界)に従い、Session/Event と選択一覧を区別して表示する。準備 timeout の追加を理由に UI 側で秒数から完了率を作らず、取消応答を実行停止の証拠にしない。

公開 field の追加は TypeScript の型だけで終えず、実行時 validator、mock/fixture、画面の正常・歴史・異常状態と三語を同期する。[契約変更ガイド](../../docs/development/contract-workflow.md)に互換性と検証順を集約する。document_snapshots の欠落は空集合へ変換せず契約エラーとする。以前の Web 回帰を現行ファイルの成功証拠にせず、検証範囲は[計画](../../docs/planning/roadmap.md#13-当前执行状态)で確認する。

原要求の確認 UI は [Run 作成と幂等](../../docs/design/run-creation.md#提交结果未知时的界面责任)を正本とする。現行 Workspace は編集可能な草稿と送信済み payload/key を分け、結果不明時に原要求を再送する。新規実行は明示確認後に別 key を発行する。状態はページ内の memory-only であり、refresh・離頁・actor/Project 切替後の自動復元はしない。

HTTP 待機 timeout と Run 取消は別操作である。受信できなかったことだけを理由に、サーバー側の作成や実行が取り消されたと表示しない。

## 調度の保存と管理を引き継ぐ

[規則・発火・Run の具体例](../../docs/design/task-scheduling.md#一个例子规则触发与执行分别看)を表示の基準にする。run_count は成功数ではなく、last_run_at と last_run_id も同一 occurrence を指すとは限らない。停止操作は在途 Run の取消ではない。

接続は [ScheduleDialog](src/components/ScheduleDialog.tsx) → [API client](src/api/schedules.ts) → [TasksPage.buildRows](src/pages/TasksPage.tsx) の順で読む。現在は先頭 100 件を取得して TaskCatalog の card に結合しているため、件数超過や精確 Task の失効で Schedule が見えなくなり得る。管理要件は[保存後の入口](../../docs/design/task-scheduling.md#保存后的管理入口)を正本とし、「見つからないので新規作成」と案内しない。

編集は API/client のみで画面入口が無い。後続は Project 単位の paging、失効した task の表示、原設定を保持する編集/競合 UI、[時間入力と表示](../../docs/design/task-scheduling.md#时间输入与展示的边界)を接続する。[TasksPage test](tests/pages/TasksPage.test.tsx) の純粋な結合/静的描画と [API test](tests/api/schedules.test.ts) の成功を、100 件超・時区差・実保存競争のブラウザ受入とは扱わない。

## 開発と検証

Node.js 26 と pnpm 11.7.0 を使用する。install、API proxy、三語確認と検証コマンドは[ローカル開発](../../docs/development/local-development.md#web)に集約する。

変更時は typecheck、Vitest、build と対象 UI のブラウザ確認を行う。generated iframe/Host API と Task Flow は未実装なので、API client の型や設計だけで利用可能と判断しない。[AGENTS.md](../AGENTS.md) の strict type、JSDoc、Effect cleanup 規約に従う。

[原要求確認のブラウザ回帰](../../docs/development/local-development.md#原要求確認のブラウザ回帰)は、実 Workspace と mock API で応答喪失・二重クリック・context 切替・三語/キーボードを検証する。通常の Vitest とは別に実行し、実 DB の幂等性や全画面の受入完了とは区別する。

[文書選択のブラウザ回帰](../../docs/development/local-development.md#文書範囲と調度入力のブラウザ回帰)は、即時/調度で送信した input/sources と Run の凍結表示を確認する。両 script は同じローカル harness を使うが、検証している責任は異なる。
