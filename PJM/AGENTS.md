# ProjectMind エージェント開発ガイド

本ファイルの規約はリポジトリ全体に適用する。

## README との役割分担

- `README.md` は開発者・運用者向けの案内であり、プロジェクト概要、前提環境、起動方法、検証コマンド、運用上の注意を記載する。
- `AGENTS.md` は Coding Agent 向けの拘束規約であり、変更時に守る設計境界、実装規約、コメント規約、テスト要件、禁止事項を記載する。
- 利用手順やコマンドの詳細は `README.md` に集約し、本ファイルへ重複記載しない。
- 製品仕様や設計判断は `../docs/` に集約し、README や AGENTS に設計本文を複製しない。

## 正式な仕様

- `../docs/`：製品仕様と設計判断の正本。本 repository（`PJM/`）はコードだけを保持し、文書一式は親ディレクトリ側に置く。文書の分類・読み順・旧番号と新 path の対応は [文書ガイド](../docs/README.md) に集約する。本ファイルへ一覧を複製しない。
- `contracts/`：実行可能なインターフェース契約。Schema、example、テストを常に同期する。

本ファイルとコード注釈に現れる `docs/01`・`docs/06` 等は文書番号を指す従来どおりの略記であり、実体は `../docs/` 配下にある。注釈側の略記は書き換えない（数百箇所へ `../` を撒くと、参照している章番号より path の方が目立ち、かえって読みにくくなる）。

仕様とコードが矛盾する場合は、推測でどちらかを変更せず、影響範囲を明示して整合させる。

## 現在のスコープ

現在の scope・状態・着手順は [実施計画 §13](../docs/planning/roadmap.md#13-当前执行状态) を唯一の正本とする。docs/01 の番号付き各節は対象設計への参照入口として残す。本ファイルへ節番号や進捗状況を複製しない（複製した瞬間から腐り始め、agent を誤った前提へ誘導する）。

以下は変更時に守る規約であり、全項目が実装済みという保証ではない。既知の document 物化範囲・Run 共通予算の差距は実施計画と対応設計に明記し、規約を弱めずに収束させる。

完成済み機能の不変条件は、どの slice を実装していても継続して適用する：Run / RunAttempt / RunSegment、Evidence、Result、Evaluation、session 認証、Project 授権、CapabilityBlueprint、Integration と三層 ResourceBinding、AgentTaskBrief、UserInteraction、順次 multi-session、ChangeProposal と controlled effect、Skill library の作用域（Organization 資産 + Project 明示有効化）、資源快照の物化（凍結 binding の scope 内だけを `input/` へ只読で落とし、上限超過は截断せず fail closed、読めなかった file は必ず manifest の `skipped` に残す）、TaskSchedule（発火は必ず `RunService.create_task_run` を通す。調度専用の作成経路を作らない——闸门が調度でだけ緩む穴になる。保存時に凍結した SkillVersion／入力／資源選択が失効しても黙って別の来源へ切り替えず ERROR で止める。停止中に過ぎた発火は追いかけない）。

CapabilityBlueprint は Interpreter だけが生成する。RuntimeManifest から blueprint を逆算する互換
投影は Release I で撤去済みであり、再導入しない（`docs/11` §5.3）。blueprint を宣言しない manifest
は発行できない。

計画を先に更新しない限り、以下を追加しない。

- 生成 FrontendModule（方向は `docs/01` §24.2 で決定済みだが、実装は §24.3 の切片としてのみ追加する）。
  とくに二点を緩めない：①bundle を返す経路は必ず `Content-Security-Policy: sandbox allow-scripts` を
  付ける（D2 は独立 Origin の代わりにこの header で opaque origin を強制する設計であり、header が
  落ちた瞬間に隔離が消える）。②iframe に `allow-same-origin` を付けない。両方ともテストで固定する。
  M3 以降は脅威 model の完了が着手条件（D3）。
- 並行 multi-agent の拡張（実装済み範囲は `docs/01` §23.3 を正本とし、本ファイルへ複製しない。拡張を足すときも次を緩めない）：
  子 Agent の能力集は `agent/subagent.py` の `resolve_subagent_capabilities` が唯一の決定点で、
  禁止集合と「名前の形による write 判定」の**二重**で落とす（列挙だけにすると新しい write 能力を
  足したとき、忘れた分がそのまま子へ漏れる）。禁止能力の要求は拒否する——黙って削ると Agent は
  渡った前提で分岐を書く。予算は `split_budget` の整除で切分し、branch ごとに与え直さない
  （合計が Run 上限を超える）。現行 Provider は各 dispatch に凍結上限を再投入しており、Run 共通の
  消費/予約/残額管理は未実装である。[子 Agent 設計](../docs/design/subagents.md)の修正要件を先に満たす。子 Session は RunEvent を書かず、主 Session 上の一つの
  ToolCall + Evidence へ収斂させる（`Run 内 sequence は厳密単調増` を階層番号へ作り変えない）。
- JAF の残り 5 タスク群。
- 任意の host Shell、無制限 network、source script の直接実行。
- ChangeProposal、承認/事前許可、登録済み write Provider、idempotency、read-back が揃っていない外部 system write。
- 代码变更の自動 apply：`repository.write/v1` は事前許可の対象にしない（`effects/catalog.py` の `preauthorizable=False`）。落とし方は Integration の `write_mode` が決める——既定 `direct` は既定 branch のみ（fast-forward、force 不使用）、`branch` は `projectmind/` 予約 namespace の新規 branch のみ。どちらでも既存 branch の上書きと任意 branch への直接書き込みは行わない。

JAF/repository-review 固有の業務 Schema、active seed、専用 renderer は新規実行の rule source として復活させない。実 Run・Proposal・composition から参照されている歴史的な SkillVersion/Run snapshot は read-only audit asset として保持する（参照の無い bootstrap seed は migration `0024_remove_bootstrap_seed` が条件付きで削除済み）。generic service、公開 API の既定 request、通常 Workspace component に業務分岐を埋め込まない。

## アーキテクチャ規約

- Backend はモジュラーモノリスとし、API と Worker は同じ `projectmind` パッケージを共有する。
- Business 層は `AgentEngine` 抽象へ依存し、Claude Agent SDK の型へ直接依存しない。
- Run snapshot と権限上限は不変とする。user の回答・Review・承認による継続は新しい RunSegment、同一 Segment の再試行・復旧・lease 接管は新しい RunAttempt を追加する。両者を混同しない。
- Domain capability と Tool capability を分離する。未知の業務 capability は有効だが、実 Tool call は登録済み versioned capability に限る。
- Skill の guidance、effect intent、model の提案は権限を付与しない。Worker には AgentTaskBrief で guidance を完全に渡し、permission は platform/Project/Run/Tool policy で独立して強制する。
- Tool capability ID は `issue.read/v1` のようにバージョンを含める。
- 自動実行できるのは登録済み Tool handler と、公開済み checksum に一致する Script のみとする。
- `cwd` をセキュリティ境界として扱わない。SDK 組み込み Tool（Read/Glob/Grep/Bash/Write/Edit/WebFetch/WebSearch）は `agent/tool_policy.py` の `DENIED_BUILTIN_TOOLS` で fail closed を維持する。同等機能は登録済み platform capability としてのみ開放する。開放済みは `workspace.read/v1`・`workspace.search/v1`・`workspace.write/v1`（いずれも Run 隔離 workspace 限定。write の対象は `workspace/` と `output/` だけで、物化済み `input/` は冻结证据として常に読み取り専用）と、束縛済み資源を読む `repository.read/v1`・`document.read/v1`・`issue.read/v1` である。sandbox command と network egress は未開放であり、個別 capability・mount/sandbox・resource limit・network policy・test が揃った slice でのみ追加する。境界の詳細は `docs/06` §6.2 と §6.4。
- 外部 effect は `observe → propose → apply` とし、apply は既定で user approval を要求する。明示的な低 risk 事前許可を導入する場合も capability、Integration、operation、risk、scope を固定し、optimistic concurrency、idempotency、read-back Evidence を必須とする。
- Result は不変とし、人工修正は Evaluation として追加する。
- PostgreSQL を Run と監査情報の正本とする。Redis は Queue、短期 Lock、通知用途に限定する。
- 業務 API は `api/auth_dependencies.py` の actor dependency を唯一の認証・授権境界とし、資源の不存在と越権は同じ 404 へ畳む。unsafe request は Origin と CSRF を必ず検証する。
- 公開 route は `api/routes/` の資源別 module に一層で実装する。未認証の並行実装や委譲 wrapper を再導入しない。

## 共通実装の利用規約

同じ意味の処理を複数箇所へ手書きせず、以下の単一実装を経由する。語彙や形式の変更もこの一箇所で行う。

- 正規化 JSON と SHA-256：`core/hashing.py` の `canonical_json` / `sha256_hex`。`json.dumps` + `hashlib` の組み合わせを新設しない。
  - 例外：`skills/importer.py` と `agent/fixture_providers.py` の既存 hash は DB の一意制約・監査値として永続化済みのため、出力形式を変更しない。
- Credential らしい field 名の検出：`core/redaction.py` の `find_sensitive_key`。Evidence と Tool response の遮断は両方ここへ依存しており、検出語彙の追加漏れはセキュリティ欠陥になる。
- Lease token の hash 化：`runs/domain.py` の `lease_token_hash`。
- `RunEvent`（RUN_SNAPSHOT）と Outbox の生成：`RunRepository` の `_snapshot_event` / `_event_outbox` / `_dispatch_outbox`。event sequence の採番は `_next_sequence` を使う。定義は `runs/repository_base.py` の共有基底にあり、行 lock は同基底の `_lock_run_row` / `_lock_segment_row` で Run → Segment の順を固定する。
- 認証・授権の Problem 変換：`api/auth_dependencies.py` の `authentication_required_problem` / `csrf_rejected_problem` / `project_not_found_problem` / `administrator_required_problem`、Run の 404 は `api/routes/runs.py` の `run_not_found_problem` / `authorized_run` を使う。同義の ProblemException を route に複製しない。
- Run 凍結 ResourceBinding の実行直前再検証（checksum、Integration status、provider、revision、capability）と Secret 解決：`agent/run_binding.py` の `load_bound_run_resource` / `resolve_binding_secret`。Provider ごとに書き写すと片方だけ緩み、Run 作成時点の権限上限を超える経路になる。
- 外部 repository への到達：`agent/repository_client.py`（git/svn command 境界。承認済み書き込みの `open_writable` も同居）と `agent/repository_source.py`（binding 解決・scope/revision 強制）。物化器、`repository.read/v1` Provider、`repository.write/v1` の effect Provider は必ずこの経路を通し、subprocess や凭据解決を各所へ書かない。
- Apply 可能な effect capability の判断（Provider/version、事前許可の可否、提案検証器、scope 投影）：`effects/catalog.py` の `EFFECT_CAPABILITIES`。proposal 作成・検証・EffectExecution 作成・承認時再検証の 4 箇所が同じ表を引く。capability ごとに `if` を書き分けると、追加時に片方だけ緩む。
- 読めない binary の text 化：`agent/binary_text.py` の `render_text`（現在 xlsx/xlsm/docx）。対応形式を増やすときもここへ足し、物化経路や Provider 側で個別に変換しない（変換の有無と skip 理由の語彙が分裂する）。

## Run 実行の信頼性不変条件

Run lifecycle に触れる変更は、以下を壊していないか必ず確認する。

- 状態遷移は `plan_run_transition` を唯一の検証点とし、`ALLOWED_RUN_TRANSITIONS` を迂回しない。
- Run と RunAttempt の row lock は常に Run → Attempt の順で取得する（`_lock_claimed_execution`）。
- RunSegment 導入後の row lock は Run → Segment → Attempt の順に拡張する。既存 migration 前の path は Run → Attempt を維持する。
- user response/approval は新 Segment、worker retry/recovery は同 Segment の新 Attempt とする。待機中は worker lease と active wall timeout を保持しない。
- 終態化（SUCCEEDED / FAILED / CANCELLED）では terminal RUN_SNAPSHOT を当該 Run の最後の event として書く。SSE stream はこれを配信終了の判定に使う。
- Run の再試行は `PROJECTMIND_RUN_MAX_ATTEMPTS` を上限とし、超過は `retry_exhausted` で FAILED へ閉じる。新しい再 dispatch 経路を追加する場合も必ず claim 側の上限判定を通す。
- `wall_timeout_seconds` は Worker Executor が deadline として強制する。engine 消費 loop へ await を追加する際は「timeout は event 待機だけに適用し、終態化 transaction を中断しない」構造を維持する。ARQ の `job_timeout` は wall timeout より長い最終防衛線として保つ。
- Worker が永続化する event の sequence は Run 内で厳密単調増加とする。TEXT_DELTA は sequence を消費するが永続化しない（欠番は正常）。
- 同一 Run の主（PRIMARY）AgentSession は当面順次実行とし、同時に複数の ACTIVE を許可しない。読取専用の SUBAGENT（`continuation_mode = BRANCH`）だけが `docs/01` §23 の設計の範囲で並行できる。resume/fork/replace の parent と checkpoint checksum を監査する。
- terminal Result は通用 OutcomeEnvelope を最低境界とし、task-specific output Schema は任意とする。Schema が宣言されていないことだけで `structured_output_missing` にしない。

## よくある変更の同期点

変更種別ごとに、漏れやすい同期先を列挙する。該当するものをすべて更新する。

- 設定値の追加：`core/settings.py`（Field で範囲を制約）→ `.env.example` → 利用箇所への注入（`api/main.py` lifespan または `worker/settings.py` startup）。
- AgentEventType の追加：`agent/domain.py` の enum → `agent/engine.py` の mapper → `web/src/api/events.ts` の `RUN_EVENT_NAMES` → `contracts/events/run-event/v1.schema.json`。
- CapabilityBlueprint / AgentTaskBrief / OutcomeEnvelope の変更：通用 contract → Backend DTO/validator/projector → frozen checksum → Worker consumer → Web validator/view → example/test を同期し、業務固有 field を meta contract に追加しない。
- RunSegment / UserInteraction / ChangeProposal の追加：domain transition → DB model/migration/repository → RunEvent/Outbox/SSE → authorization/idempotency → Web detail drawer/response UI → recovery/Compose test を同期する。
- Tool capability の追加：`contracts/tools/<capability>/` の request/response/error schema → Provider 実装 → `agent/context_builder.py` の registry 登録 → permission snapshot の `allowed_capabilities` → テスト。
- 公開 API endpoint の追加：`api/routes/` の該当資源 module に実装し、認証境界は `api/auth_dependencies.py` の actor alias（`ReadActor` / `WriteActor` / `ProjectReadActor` など）で宣言する → response model は field を明示列挙する（内部 field の意図しない公開を防ぐ許可リストとして機能する）→ error は `ProblemException` → `web/src/api/` の該当資源 module に validator 付き client 関数を追加し、`web/src/api/index.ts` から再輸出する。
- DB model の変更：`db/models.py` → Alembic migration → repository の projection/DTO → テスト。`relationship` を張らない FK 親子を同一 transaction で追加する場合は、親を追加した直後に `flush()` する。SQLAlchemy の unit of work は relationship の無い mapper 間の INSERT 順序を保証せず、子が先に挿入されて FK 違反になる（実 DB でのみ再現するため、単体テストでは順序自体を固定する）。
- Contract schema/example の追加：`contracts/` へ配置 → `scripts/validate_contracts.py` と `backend/tests/contracts/test_contracts.py` の**両方**の example 表へ登録する。片方だけだと example は誰にも検証されないまま通る。
- 構造化 log field の追加：`core/logging.py` の `_CONTEXT_FIELDS` allowlist へ登録する。未登録の field は例外ではなく**黙って捨てられる**ため、監査点を足したつもりで何も出ていない状態になる。
- system Skill（`skills/projectmind-skill-interpreter/`）の変更：SKILL.md か `references/` を触ると package の content_hash が変わり identity/prompt checksum も変わる。version（SemVer）→ `contracts/examples/skill-interpreter-request.v1.json` と `skill-interpreter-response.v1.json` の identity → 該当テストの assertion を同期する。prompt が model 実行に委ねる規則は確定性検査で守れないため、規則が prompt に載っていること自体をテストで固定する。

## Ingress と Compose の規約

- 配備環境には共有 Traefik が既に存在する。
- Traefik service、静的設定、証明書 resolver、dashboard、Docker socket mount を追加しない。
- ProjectMind container は host port を公開しない。
- 外部 edge network に参加できるのは `web` と `api` のみとする。
- 外部 URL は `PROJECTMIND_CONTEXT_PATH` 配下に限定し、`{contextPath}/api` は API、`{contextPath}` は Web が所有する。
- Traefik は外部 context path を除去し、FastAPI の内部 route は `/api`、Web の静的 route は `/` を維持する。
- PostgreSQL、Redis、MinIO、Worker、Sandbox は内部 network に限定する。

## 言語・コメント規約

- `AGENTS.md`、`README.md`、サブディレクトリの README は日本語で記述する。
- Class、関数、method、fixture、migration entry point には、責務を説明する日本語の docstring または JSDoc を付ける。
- 複雑な分岐、セキュリティ境界、transaction、復旧処理、互換性維持のための code block には、理由を説明する日本語コメントを付ける。
- コメントはコードを日本語へ置き換えるだけにせず、「なぜ必要か」「何を守るか」を記述する。
- 自明な代入や単純な return に逐語的コメントを付けない。処理変更時に古くなったコメントは同時に更新する。
- 識別子、公開 API field、Schema key、protocol 名、外部製品名は既存の英語表記を維持する。
- User-facing 文言の言語は画面仕様に従い、コメント言語とは分離する。

## Python 実装規約

- 全 module の先頭で `from __future__ import annotations` を維持する（lint では強制していない規約）。
- Public/内部を問わず Class と関数の責務を日本語 docstring で説明する。
- Domain rule と infrastructure access を分離し、FastAPI route や ARQ job に業務判断を埋め込まない。
- 広い `Exception` 捕捉は health check や境界層など必要な箇所に限定し、理由をコメントする。
- DB schema 変更には必ず Alembic migration を追加する。

## TypeScript / React 実装規約

- TypeScript strict mode を維持し、`any` で契約を回避しない。
- Component、公開関数、型の業務上の役割を日本語 JSDoc で説明する。
- API response は将来 Schema から生成する型へ移行できる境界に閉じ込める。
- Effect は cleanup を実装し、非同期結果が unmount 後に state を更新しないようにする。
- 生成 FrontendModule の権限を通常の Web application へ混在させない。
- API client は `web/src/api/` の資源別 module に置き、画面は `index.ts` barrel からだけ import する。HTTP 実行と Problem 変換は `api/http.ts` の `requestApiJson` / `requestApiEmpty` を経由し、fetch を直接呼ばない。画面非依存の純 logic は `src/lib/` に置く。
- 画面の役割分担を混ぜない：**任务中心(`TasksPage`)は「何を走らせるか」を選ぶ場所、工作空间
  (`WorkspacePage`)は「今走っている一つ」を観る場所**。任务中心に二つ目の Run lifecycle(SSE 購読・
  取消・終態化)を作らない。task の実行設定は `lib/taskDraft.ts` と `components/TaskLaunchFields.tsx`
  の単一実装を即時実行と時刻起動の双方が使う。
- server が返す決定的 ID を frontend で再導出しない（例: Run と task を突き合わせる `task_id` は
  `runs/domain.py` の `derive_task_id` が唯一の導出元）。再実装すると導出方式が変わったときに
  「実行はできるのに履歴が紐づかない」静かな不整合になる。
- 一覧の絞り込みは server 側で行う。先頭 page を client で filter する実装は、対象が古い page に
  あるときに取りこぼす（「待你处理」の漏報は、表示しないことより悪い）。
- User-facing 文言は `src/lib/i18n/messages.ts` の言語別 catalog（`Record<UiLanguage, ShellMessages>`）へ置き、画面は `src/i18n.tsx` の `useMessages()` で取得する。key の追加は zh/ja/en 三言語を同時に補い、画面へ文字列を直接 hard code しない。

## テストと検証

- 動作変更と同時にテストを追加または更新する。
- テストは `tests/` 配下で `src` の module 構成を反映した directory に置く。API contract test は `tests/api/` に資源別 file で分割し、fake service は `tests/api/fakes.py`、既定の認証済み client 設定は `tests/api/conftest.py` を共有する。
- テスト名は検証する振る舞いを表し、日本語 docstring で目的または守る不変条件を補足する。
- 契約変更時は Schema、example、OpenAPI、Backend test、Web test を同期する。
- 変更範囲に応じて [開発ガイド](../docs/development/local-development.md) の検証を実行する。文書のみの場合は [文書 build/check](../docs/development/documentation.md) と関連例の契約確認を行う。
- 実行できない検証がある場合は、未実行理由と残るリスクを報告する。

## 変更規律

- 既存の設計境界を変更する場合は、先に対応する `docs/` と契約への影響を確認する。
- 生成 lockfile を手作業で編集しない。
- Credential、内部 URL、Ticket 本文、Gold data、Secret を fixture やログへ入れない。
- 公開 API error は `contracts/errors/problem/v1.schema.json` と互換に保つ。
- SSE event は `contracts/events/run-event/v1.schema.json` と互換に保ち、Run 内で sequence を一意かつ単調増加にする。
- ユーザーが明示的に依頼しない限り、Git の初期化、commit、push、履歴変更を行わない。

## 完了条件

- 要求された変更が `docs/` の仕様と現行 slice の scope に整合している。
- 日本語コメントと文書が規約に従っている。
- 関連する Schema、migration、テスト、文書が同期している。
- 必要な検証が成功し、未解決リスクが明示されている。
