# 実行・データ保守ガイド

本ページは既存経路を保守する上で守るべき境界を定義する。詳細な項目・状態・実装はコードと [contracts](../../SKM/contracts/) を正とし、能力の進捗は [Roadmap](../planning/roadmap.md) を参照。

## 認証と認可

- 入口は [actor dependency](../../SKM/backend/src/skillmind/api/auth_dependencies.py) を共用する。Cookie による変更要求は Origin/CSRF も検証し、明示 API Key header は原 Key を検証して重複・混用・無効時は即拒否する（Cookie へ fallback しない）。越権と不存在は同じ 404 を返し、アーカイブ済み Project は認可された読取のみを許可する。
- API Key は作成者が属する Organization の ADMIN 資源権限を付与し（Key 単位の scope なし）、作成者が有効な ADMIN であり続ける必要がある。Key によって Project 状態・凍結権限・配備スイッチ・承認を迂回することはできない。
- 認証と業務コミットは別トランザクションとする。永続書込は [UserAccess](../../SKM/backend/src/skillmind/users/access.py) を通し、ロック待機後およびコミット前に現在の資格・メンバー・Project 状態を再検証する。バックグラウンド実行は永続化された起票者・承認・原 claim を再検証し、ブラウザセッションを偽装しない。
- Run は権限上限を凍結し、後続の権限追加を旧 Run へ遡及させない一方、権限剥奪は即座に効力を持つ。資格停止・業務取消・ローカル接続切断は個別に扱い、リモートプロセスの終了を決めつけない。
- Secret は [resolver](../../SKM/backend/src/skillmind/integrations/secrets.py) 経由でのみ制御下 Provider に渡し、Brief・ログ・Evidence・ワークスペースへ入れない。`ENVIRONMENT`/`FILE` は参照を保存し、`MANAGED` は Project と参照に束縛した[暗号化実装](../../SKM/backend/src/skillmind/core/secret_crypto.py)を使う（鍵欠落・改ざん・解決失敗はすべて拒否）。ローテーションと復元は [Runbook](../operations/runbook.md) と[一致復元点](../operations/backup-recovery.md#一致復元点の内容)に従う。

## Skill と凍結原文

- Skill は Organization 資産であり、公開後に Project が特定版を明示有効化する。[SkillService](../../SKM/backend/src/skillmind/skills/service.py) に従い取込・公開・有効化・参照検査を行い、公開済み内容の上書きや既存参照がある版の削除を行わない。
- 取込ディレクトリに複数の `SKILL.md` がある場合は画面（または API `/skill-imports/upload` の `entry_path`、CLI `--entry`）で解析対象を選択する。アップロードルート内の共有 Schema・参照のみを辿り、選択された Skill と依存ファイルを元パス・バイト列・ハッシュのまま保存する（ルート外参照や他 Skill の暗黙解析は行わない）。
- 新規取込は `skill-candidate/v3` の入力と資源選択のみを解析し、`skill-execution/v2` の単一実行タスクとして固定する。凍結原文一式を Brief に渡して Agent が実行する（入口は [direct_candidate](../../SKM/backend/src/skillmind/skills/direct_candidate.py)、[execution](../../SKM/backend/src/skillmind/skills/execution.py)、[task_brief](../../SKM/backend/src/skillmind/agent/task_brief.py)）。`documents` はテスト入力を凍結し、`resource` は接続/文書ライブラリを選択する（それ自体は読み書き権限ではない）。汎用ファイル・画像・変換・Schema ツールは常備され、外部ツールと操作は選択資源の全 scope/capability と配備上限で Run に固定する（Task 層でのツール allowlist や資源範囲の再縮小は行わない）。旧 Blueprint は原値を保持し、逆算や自動昇格を行わない。
- 出所・スクリプト・モデル提案は権限ではない。ツールは [catalog](../../SKM/backend/src/skillmind/agent/tool_catalog.py) で契約と Provider を配線し、[ContextBuilder](../../SKM/backend/src/skillmind/agent/context_builder.py) と [ToolExecutionPolicy](../../SKM/backend/src/skillmind/agent/tool_policy.py) で凍結束縛・スイッチ・権限を検証する。SDK builtin は拒否し、Shell/ネットワーク/ファイルは Gateway を迂回せず、実行スクリプトは公開 checksum を照合する。
- 非同期解釈は元要求・ソース hash・モデル設定・呼出事実を保持し（タイムアウトや結果不明時に ID を変えて再実行しない）、変更時は [request_service](../../SKM/backend/src/skillmind/skills/request_service.py) を確認する。Codex モデル情報は固定 CLI で検出し、選択モデルや思考強度が未取得の場合は専用ログインとプロキシで公式検出を実行し、失敗時は推測や自動縮退を行わず明示エラーとする。

## 入力と文書

- **スナップショットとワークスペース**：Run は特定版・資源選択・文書 ID/hash・束縛を凍結する。実体化と再利用は [workspace_materializer](../../SKM/backend/src/skillmind/agent/workspace_materializer.py) と [input_snapshot](../../SKM/backend/src/skillmind/runs/input_snapshot.py) で原回執と入力ツリーを検証し、`input/` は読取専用、書込は `workspace/`・`output/` に限定する。能力の可用性は [Worker 配線](../../SKM/backend/src/skillmind/worker/settings.py) と凍結束縛に従う。
- **ツールエラー分類**：引数不正は `invalid_request`（項目と制約・未実行である旨を示し、修正後の再送を許可）、越権は `scope_denied`、戻り値違反や未分類失敗は `unavailable` を返し、結果不明の外部操作は自動再送しない。
- **アップロード・同名更新・削除**：アップロードは [DocumentService](../../SKM/backend/src/skillmind/documents/service.py) で原 actor/Project・冪等キー・内容回執を束縛する。画面でのファイル/フォルダ投入は同ディレクトリ同名ファイルを既定で上書き更新し（「ファイルを更新」メニューと同経路）、新版に新 ID を採番して旧版をごみ箱へ移し、原バイト・凍結参照・回執を保持する（クォータ対象も継続）。改名・移動は表示パスのみを変更し文書 ID を維持する。完全削除はごみ箱移動後に生成実行の終了と未決参照がないことを確認して行う（[参照クエリ](../../SKM/backend/src/skillmind/documents/reference_repository.py) は Run・Schedule・occurrence を網羅）。削除は同一トランザクションでオブジェクト清掃を登録した後に非同期で blob を消し、実行履歴の清掃は [history_purge](../../SKM/backend/src/skillmind/runs/history_purge.py) に従い共有成果物と最小削除監査を保持する。
- **Excel 変換（`excel-styles/v3`）**：`.xlsx` 変換は元の行列・静的塗潰し色・全体/部分取消線を保持する。割当済みセル書式（無色・取消線解除を含む）を優先し、未割当セルは行→列→既定の順でフォールバックする。同一書式は連続範囲に統合し、結合セルは左上書式を参照して本文を重複出力しない。RGB/theme/indexed/tint の原値と解決結果を保持し（index 64/65 は `SYSTEM_FOREGROUND`/`SYSTEM_BACKGROUND`、範囲外や未知色は `UNRESOLVED`）、実在する表/範囲にのみ未評価の条件付き書式や表スタイルを注記し、旧 `.xls` は値のみ・スタイル未検査と明記する。スタイル profile は Evidence に記録し、Markdown 本文には重複出力しない。
- **文書読み書きと分割変換**：`document.read/v1` は `response_mode=file` で本 Run ローカルファイルへの交付を優先する。新規 Run は現在 Project の文書ライブラリ全体の読取権限を持ち（都度現在ユーザーと Project 権限を再確認）、アップロード回執 ID/hash による直接読取に対応する。`document.convert_to_markdown/v1` も `response_mode=file` と `publish_artifact=true` を優先し、`workspace.read/v1` は `offset`・`max_chars`（既定 64,000、上限 200,000）・`expected_hash` による分割読取に対応し、ローカル欠落時は `artifact.materialize/v1` で検証済み原バイトから復元する。
- **ワークスペース編集とディレクトリ提案**：`workspace.edit/v1` は原ハッシュに基づくコピー・追記・一意文字列置換・出力公開に対応し、`document.files/v1` は認可文書ライブラリ内のディレクトリ列挙と保存・改名・移動・空ディレクトリ作成/削除・ごみ箱移動/復元提案を準備する（実書込は共有 Effect と元版競合検証を通す）。

## 資源ファイルと原生クライアント

ネットワークアクセスは Worker 内の共有クライアントが実行し、束縛・権限・回執を検証して資格情報をモデルに露出しない。要求は `request_file` + `expected_hash` に対応し、応答は `workspace/resources/` に保存する。`change.propose` は JSON ファイル提出とその内容ハッシュ束縛に対応する。8 KiB を超える確認済み Effect 回執はファイル参照として交付し（短い回執や保存失敗時は原値を返却）、同一回執は継続・復旧でも同一投影を保つ。

- **Git**：`repository.workspace/v1` で認可 UTF-8 ファイルを編集可能ディレクトリに配置し、`prepare_commit` でローカルファイルハッシュから提案を準備し、コミット時も branch・パス・原 revision・read-back を検証する。
- **MCP**：検出・凍結された原生パラメータ Schema を使い、読取はファイル入出力に対応し、書込は Effect・承認・原操作 ID を通す。
- **PostgreSQL**：`database.query/v1` と `database.execute/v1` を使い、テーブル/列権限は DB アカウントに委ねる。単一 SELECT と承認後の INSERT/UPDATE/DELETE・JOIN/CTE・`$1` パラメータに対応する（DDL・トランザクション制御・複文は禁止）。書込前に同一資源へ `read_back` と同一の SELECT（INSERT 対象行の不在確認を含む）を実行し、応答ファイルハッシュと Evidence を提案前提とし、書込・read-back・`skillmind_effects.execution_receipts` を同一トランザクションでコミットする。
- **HTTP API**：接続にはルート URL 一覧と認証 Secret のみを登録し、パス・メソッド・引数は登録された origin とルート配下で Skill/Agent が指定する。GET/HEAD 読取と承認後の POST/PUT/PATCH/DELETE に対応し、事前 GET や read-back は強制しない。原ステータスコード・許可ヘッダ・完全バイト列ファイルをそのまま Agent に渡す（自動リダイレクト禁止、タイムアウトや結果不明時の自動再送禁止）。
- **設定移行**：`0059_native_resource_clients` で旧書込接続を SELECT/INSERT/UPDATE/DELETE へ、Redmine を HTTP へ変換し、`0062_http_resource_roots` で基底 URL をルート一覧へ統合して旧既定束縛を無効化する（旧 Run スナップショットは書き換えない）。

## 画像と精密読取

- `mcp.download/v1` は同一 Run・同一束縛の成功した MCP 読取 Evidence と原応答 JSON Pointer からダウンロード URL を解決し、同一 origin へのみ資格情報付き GET を送り（リダイレクト・別接続参照は禁止）、権限検証後に完全バイト列とハッシュを保存する。
- `workspace.read/v1` の JSON モードは `pointers`（RFC 6901）と `expected_hash` で項目を精密読取する（欠落は `exists: false`、配列は既定 20 件/ページ・上限 200 件、元ファイル上限 16 MiB・結果上限 64 KiB、超過時は切り詰めず拒否）。
- `workspace.image/v1` は Run 内パス・原ハッシュ・PNG/JPEG/WebP 実体（上限 8 MiB・2,500 万画素・単一フレーム）を検証してエンジンへ画像を渡し、SDK builtin `view_image` は禁止する。スクリーンショットとウィンドウ状態は Skill が照合し、クリックは確認済み MCP セレクタを使用する。

## 実行と復旧

- [システム構成と用語](../overview/architecture.md)に従い Run・Segment・Attempt を区別する（入口は [worker/settings](../../SKM/backend/src/skillmind/worker/settings.py) と [executor](../../SKM/backend/src/skillmind/worker/executor.py)）。同一段階の復旧は新 Attempt を作成し、原スナップショット・権限上限・Result は変更しない。
- 送信前に主体・冪等キー・入力・資源選択・承認同意を凍結し、[creation_request](../../SKM/backend/src/skillmind/runs/creation_request.py)・[creation_replay](../../SKM/backend/src/skillmind/runs/creation_replay.py)・Web [runSubmission](../../SKM/web/src/lib/runSubmission.ts) を共用する。通信断やタイムアウト時はまず原回執を確認し、再送は原キーと原内容を維持して遅延応答を隔離する。
- 取消はまず意図を永続化し、[RunRepository](../../SKM/backend/src/skillmind/runs/repository.py) がロック内で lease・取消・終端状態を検証し、ツール呼出は [evidence](../../SKM/backend/src/skillmind/agent/evidence.py) で実行権を再確認する。モデルセッション復旧は [session_store](../../SKM/backend/src/skillmind/agent/session_store.py) の原記録を読み、根拠欠落時は停止する。
- 定期実行（[schedules/service](../../SKM/backend/src/skillmind/schedules/service.py)）は既定で自動承認を有効にし、汎用・Git・MCP の同意を発火時の原 occurrence 要求に固定した上で、実行時に作成者の現在の Project・資源権限を再検証する。

## 外部効果

- 外部書込は observe → propose → approve → apply に従う。モデル向け説明は各 Effect request Schema の `description` から供給し、文書と Git は提案準備ツールを優先利用する。
- 承認は特定 Proposal の version/checksum・Provider・操作・scope に束縛し、[EffectService](../../SKM/backend/src/skillmind/effects/service.py) が業務ロック外でリモート I/O を実行する。自動承認は原 Run 開始時に記録された同意または既存の事前認可に限定する（`repository.write/v1` は Project 事前認可と force を禁止）。
- 実効能力は [ExecutionFeatures](../../SKM/backend/src/skillmind/effects/release.py) と [Provider registry](../../SKM/backend/src/skillmind/effects/wiring.py) のスイッチ双方で制限する。MCP 提案は同一 Run/接続/束縛のツールカタログ証拠を自動関連付けして checksum に含める。
- 書込結果が不明な場合は主フローを停止して原 Effect を保持し、[reconciliation_service](../../SKM/backend/src/skillmind/effects/reconciliation_service.py) で読取照合を行う。MCP の read-back 再試行（[mcp_provider](../../SKM/backend/src/skillmind/effects/mcp_provider.py)）は変更操作を決して再送しない。

## 結果と評価

- 技術的終端状態・結果の完全性・業務 PASS/FAIL は分けて表現する。[ResultValidator](../../SKM/backend/src/skillmind/agent/result_validation.py) が凍結 Schema・参照帰属・添付バイト・保存済み Effect 事実を検証する。
- Result/Evidence/Artifact は不変とし、人手修正は [Evaluation](../../SKM/backend/src/skillmind/evaluations/service.py) を追記して原結果を上書きしない。
- `audit.export/v1`（[audit_export](../../SKM/backend/src/skillmind/agent/audit_export.py)）は保存済み事実のみを出力し、`artifact.append/v1` は原バイト検証後に短文を追記して新 Artifact 参照を公開する（元の監査参照は不変）。表示規則は [UI ガイド](../design/workspace.md#レポートとプレビュー)を参照。

## 後置能力

未接続または要否判断待ちの能力は日常タスクの前提としない。

| 範囲 | 現在の保守境界 |
| --- | --- |
| Run 共通予算 | [primary_budget](../../SKM/backend/src/skillmind/worker/primary_budget.py) は本番 coordinator 未接続。Codex 起動前にドル上限を拒否し、将来接続時は親子共有・課金前確保・費用不明時の返還なしを守る |
| 子 Agent | [Provider](../../SKM/backend/src/skillmind/agent/subagent_provider.py) は部分実装。権限は [resolve_subagent_capabilities](../../SKM/backend/src/skillmind/agent/subagent.py) で絞り込み、書込・対話・再帰 dispatch を禁止 |
| 生成 UI | [Manifest](../../SKM/contracts/runtime-manifest/v1alpha1.schema.json) は `frontend_module` を引き続き禁止。将来開放時は CSP `sandbox allow-scripts` と iframe の `allow-same-origin` 禁止を検証する |
| 外部書込シーケンス | [局所実験](../../SKM/backend/tests/worker/test_receipt_sequence_experiment.py) は本番未接続。同一 job の SDK 再利用と読取専用短列では一括外部書込を行わない |
| その他拡張 | Task Flow は読取専用プレビュー。完全オーケストレーション・SVN 書込・PR 自動作成・広範な Shell/network は実需要に応じて評価 |

## 限定ツール列とウォーム継続

- `agent/tool_sequence.py` は元の `ToolGateway` を通じて `sequence_safe` と明示されたローカル/読取専用サブツール 1〜5 個（原 Run の凍結集合内）を順次実行し、取消伝播に対応し外部書込と制御ツールを禁止する。
- `agent/warm_codex.py` は同一 Worker job の原 Run 内でのみ Codex プロセスを再利用し（Run 間で権限やセッションを共有しない）、原 Run ロック内で隣接 Segment・原 Effect・自動承認を検証して新 Attempt を作成する。

## 自動承認操作の直接回執

`SKILLMIND_INLINE_EFFECTS_ENABLED` は既定で無効（migration `0056_inline_effect_owner` に依存し、Codex かつ Attempt 残り時間が十分な自動承認操作にのみ適用。Claude や他条件は従来の延期経路を通る）。`change.propose/v1` は原 SDK 呼出 ID から冪等キーを生成して既定管理項目を補完し、有効時は原 Attempt を `RUNNING` のまま `ApprovedEffectExecutor` で claim・コミットして原回執を当該ツール結果として直接返す。失敗・取消・応答不明時は即座に当該 turn を停止して従来の Effect 待機/照合フローへ戻し、重複する書込操作を決して新規作成しない。

## 同一タスクの同種複数資源

Run は Skill の資源スロットに従い同種の複数接続を束縛できる。`database.*`・`mcp.*`・`repository.read`・`issue.read`・`tool.sequence` のサブ呼出および `change.propose` は最上位の `resource_key` で凍結済みスロットを選択する（複数束縛時は必須、単一束縛時は省略可。URL/ID の直接指定やテーブル名からの推測は不可）。SDK は能力ごとに 1 つのツール定義のみを公開して選択可能な key を列挙し、内部では `(capability, resource_key)` ごとに独立して認可・ルーティング・監査を行い、最上位 selector を除いた業務引数を Provider に渡す。

