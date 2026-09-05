# ProjectMind 基础版本运维与恢复 Runbook

本书用于按同一套手册执行基础版本的配备、migration、backup、故障恢复和 version rollback。产品与 Runtime 规格以 `docs/01`、`docs/06` 为准，认证边界以 `docs/09` 为准。

## 1. 運用原則

- PostgreSQL を Run、Attempt、Event、Evidence、Result、Evaluation、認証情報の正本とする。
- Redis は Queue、通知、短期 lock のみであり、Redis backup だけでは復旧できない。
- `.env`、password、session/CSRF token、provider credential、Ticket 本文を backup manifest や log に記録しない。
- migration 前と application image 更新前に PostgreSQL backup と使用 image ID を取得する。
- 既存 migration を `stamp` で飛ばさない。失敗理由を修正し、同じ head まで正常に適用する。
- rollback は「互換な旧 image へ戻す」か「database backup を復元する」のどちらかとし、監査 row を手作業で削除しない。

以下の例では正式コードの `PJM/` を起点に実行し、別設定を使う場合は各 `make` command に `ENV_FILE=.env.production` を付ける。

## 2. 配備前 backup

まず処理中の外部 Effect が完了したことを確認し、保守時間を設けて API/Worker の新規書込みを止める。PostgreSQL の transaction snapshot だけでは blob/workspace と整合した復元点を作れない。同じ STAMP を付けることも整合性の証明にはならない。

初期管理者を作るだけなら[通常 bootstrap CLI](quickstart.md#最初の-admin-を作成する)を使う。`make bootstrap-admin` は全 volume 消去であり backup や更新手順に含めない。

Backup directory は owner 以外が読めないように作成する。

```bash
umask 077
mkdir -p backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose --env-file .env exec -T postgres sh -ceu \
  'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
  > "backups/projectmind-${STAMP}.dump"
sha256sum "backups/projectmind-${STAMP}.dump" \
  > "backups/projectmind-${STAMP}.dump.sha256"
docker image inspect projectmind/backend:0.1.0 projectmind/web:0.1.0 \
  --format '{{.RepoTags}} {{.Id}}' > "backups/images-${STAMP}.txt"
```

Dump が空でないこと、checksum 検証が成功することを確認する。

```bash
test -s "backups/projectmind-${STAMP}.dump"
sha256sum --check "backups/projectmind-${STAMP}.dump.sha256"
```

Object storage には SkillSource と Project 文書もあるため、Artifact の利用有無によらず DB が参照する blob を同じ復元点で保管する。停止した書込みがない状態で object-storage と必要な Run workspace の snapshot を配備基盤で取得し、DB dump と対応付ける。MANAGED の KEK は別系統で退避する。復旧手順には blob の読戻しと実 Run の再表示も含め、DB 接続成功だけで完了としない。

## 3. Migration と配備

現在の適用 revision と image が要求する head を確認する。

```bash
docker compose --env-file .env run --rm migrate alembic current
docker compose --env-file .env run --rm migrate alembic heads
```

新 image を load した後、application を起動する前に migration を単独実行する。

```bash
docker compose --env-file .env run --rm migrate
docker compose --env-file .env up -d --no-build
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
make                                            # service 状態
```

Migration が失敗した場合は `alembic_version` を手動更新しない。PostgreSQL の transactional DDL で rollback されたことを `alembic current` で確認し、修正版 Backend image で再実行する。非 transactional 操作や revision 不一致が残った場合は第 5 節の database restore を使う。

`0018_skill_library_scope` は Skill library の contract migration である。適用前に第 2 節の dump を
必ず取得する。同一 Organization/content の重複 source、複数 Organization に跨る Skill identity、
未公開または cross-Organization の Project enablement を検出した場合は fail closed で停止するため、
行を削除・統合して通過させず、参照元を含む整合方針を決めてから再実行する。適用後に一つの source を
複数 Project が共有した場合、旧 `skill_sources.project_id` への downgrade は一意に戻せない。
その場合は Alembic downgrade を試行せず、配備前 dump と旧 image を第 5/6 節の手順で復元する。

`0019_outcome_envelope` は既存 `run_results.data_json` を変更せず、歴史行を
`STRUCTURED_OUTPUT` として分類し、Evidence/Artifact 引用と任意 Schema identity の追加列を空値で
補完する additive migration である。新 image は以後 `OUTCOME_ENVELOPE` を書くため、旧 image へ
戻す場合は新形式 Result が一件でも作成された時点で Alembic downgrade を行わず、第 5/6 節の
backup/image 一式を復元する。

`0020_interactive_run` は新 Run に RunSegment、AgentTaskBrief snapshot、UserInteraction と Session
lineage を追加する。歴史 Run を推測で backfill せず read model の implicit Segment として扱うため、
配備後に新 Run が一件でも作成された場合は旧 image へ単独 rollback しない。

`0021_controlled_effects` は SecretReference、Integration、ResourceBinding、ChangeProposal、Approval、
EffectPreauthorization、EffectExecution と Result の `change_proposal_refs_json` を追加する。適用前に
必ず dump を取得し、適用後は API/Web/Worker を同じ image 世代へ揃える。旧 image は新しい Run binding
や effect Outbox を理解できないため、downgrade ではなく配備前 dump + 旧 image を復元する。

## 4. Generic task acceptance と障害接管

### 4.1 通常 smoke

Worker dispatch と model credential を有効化し、専用テスト Project の UUID を `SMOKE_PROJECT_ID` に設定して次を実行する。smoke は Run/Evaluation を作り取消も行うため、業務監査データへの書込みを伴う。`PROJECTMIND_SMOKE_PROJECT_ID`
には、smoke 対象の published task を持つ Project を明示する(0024 で seed Project を廃止したため
既定値は無く、未設定なら実行を拒否する)。

```bash
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
docker compose --env-file .env exec \
  -e PROJECTMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" \
  api python -m projectmind.ops.smoke
```

smoke test（`docker compose --env-file .env exec -e PROJECTMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" api python -m projectmind.ops.smoke`）は password を TTY から読み、次を検証する。

1. login CSRF、session cookie、session CSRF。
2. 同じ idempotency key の Run replay。
3. TaskCatalog で選択した published task の SUCCEEDED、Result、Evidence、SkillVersion checksum。
4. Evaluation の追加と Result 不変性。
5. 永続 event sequence と terminal snapshot。
6. `Last-Event-ID` より後だけを返す SSE reconnect。
7. active Run の interrupt と CANCELLED audit。

対象 task は `PROJECTMIND_SMOKE_SKILL_VERSION_ID` と `PROJECTMIND_SMOKE_TASK_KEY` で固定できる。
Schema に適合する入力と source 選択は `PROJECTMIND_SMOKE_TASK_INPUT_JSON` と
`PROJECTMIND_SMOKE_TASK_SOURCES_JSON` に JSON object として設定する。

### 4.2 Worker 喪失と接管

検証用 Run が `RUNNING` になった時点で Worker を停止する。

```bash
LEASE_SECONDS="$(docker compose --env-file .env exec -T api python -c \
  'from projectmind.core.settings import get_settings; print(get_settings().run_lease_seconds)')"
docker compose --env-file .env stop worker
```

`PROJECTMIND_RUN_LEASE_SECONDS` と recovery cron の最大待ち時間 20 秒を加えた時間だけ待ってから Worker を戻す。

```bash
sleep "$((LEASE_SECONDS + 20))"
docker compose --env-file .env start worker
docker compose --env-file .env logs --no-log-prefix --tail=200 worker
```

成功条件は、旧 Attempt が `LEASE_EXPIRED`、Run が一度 `RETRY_PENDING` になり、新しい `run_attempt_id` と増加した `attempt_no` で再開することである。Run の input、task、permission、SkillVersion snapshot は変化してはならない。上限超過時は `retry_exhausted` で FAILED へ閉じる。

### 4.3 回帰 matrix

| Scenario | 自動 gate | 期待結果 |
| --- | --- | --- |
| Model output が Schema 不適合 | `test_invalid_result_is_finalized_as_failed` | FAILED / `result_schema_invalid`、不正値を error/log に出さない |
| Tool boundary 変更・未登録 Tool | `test_pre_tool_denial_is_audited_without_argument_values` | Provider 未実行、deny audit、Ticket 値を複製しない |
| Worker lease 失効と接管 | `test_worker_loss_recovery_creates_second_attempt_without_snapshot_drift` | Attempt 追加、snapshot 不変 |
| SSE 切断・再接続 | smoke test | `Last-Event-ID` より大きい永続 event だけを replay |
| Idempotent Run 再送 | smoke test | 同じ Run ID、`idempotent_replay=true` |

## 5. Database restore

この節は対象 DB を削除して置換する破壊的復旧である。復旧対象の環境、dump、対応する blob/workspace、image、必要な KEK を確認し、現在の状態も別途退避する。`RESTORE_STAMP` に復元対象の日時識別子を設定し、checksum を確認して書込み service を停止する。

```bash
sha256sum --check "backups/projectmind-${RESTORE_STAMP}.dump.sha256"
docker compose --env-file .env stop api worker migrate
```

Database を再作成し、owner を変更せず restore する。

```bash
docker compose --env-file .env exec -T postgres sh -ceu '
  dropdb --if-exists --force --username="$POSTGRES_USER" "$POSTGRES_DB"
  createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" "$POSTGRES_DB"
'
docker compose --env-file .env exec -T postgres sh -ceu '
  pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-owner
' < "backups/projectmind-${RESTORE_STAMP}.dump"
```

DB と対応する object-storage/workspace snapshot を配備基盤の手順で戻し、必要な旧 KEK を復号可能にする。実 blob を取得できることを確認してから、対応 image または互換な新 image で migration を forward 適用し、検証して起動する。

```bash
docker compose --env-file .env run --rm migrate
docker compose --env-file .env up -d --no-build
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
docker compose --env-file .env exec -e PROJECTMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" api python -m projectmind.ops.smoke
```

## 6. Application version rollback

旧 Backend が現在の DB schema と backward compatible である場合だけ、旧 archive を指定して application image を戻す。

```bash
make deploy IMAGE_ARCHIVE=images/projectmind-previous.tar
docker compose --env-file .env exec -T api python -m projectmind.ops.preflight
docker compose --env-file .env exec -e PROJECTMIND_SMOKE_PROJECT_ID="$SMOKE_PROJECT_ID" api python -m projectmind.ops.smoke
```

旧 Backend が新 schema を解釈できない、または migration 後に新形式の data が書かれた場合は Alembic downgrade を即興で実行しない。第 5 節で配備前 dump を復元し、その時点の Backend/Web image archive を load する。

## 7. Log 確認と incident 記録

ProjectMind log は一行 JSON であり、API は `trace_id/request_id`、Worker は `run_id/run_attempt_id`、Agent lifecycle は `agent_session_id` を出力する。

```bash
docker compose --env-file .env logs --no-log-prefix api worker \
  | jq -c 'select(.run_id == "<RUN_ID>" or .trace_id == "<TRACE_ID>")'
```

Incident 記録には UTC 時刻、image ID、migration revision、Run/Attempt/Session ID、公開 error code だけを残す。Password、token、credential、Ticket 本文、Tool raw argument/result、Evidence excerpt は転記しない。

## 8. 対話型 Run と外部 Effect の運用契約

本節は 0020/0021 以降に適用する。外部 write は、登録済み capability、固定 ResourceBinding、Proposal、
承認/明示的な事前許可、原子 Provider、idempotency と read-back がすべて揃う場合だけ許可する。現在の唯一の
apply できるのは Redmine `issue.update/v1` CAS adapter と git `repository.write/v1` (§20) であり、stock endpoint、svn commit と任意 write は
引き続き禁止する。

### 8.1 WAITING 状態

- `WAITING_FOR_INPUT` と `WAITING_FOR_APPROVAL` は非終端状態である。Worker lease と課金対象 process を保持したまま待機してはならない。
- 待機へ入る前に AgentTaskBrief checksum、checkpoint、Interaction、Session transcript、RunEvent と Outbox を同じ監査境界で永続化する。
- active execution の wall timeout は待機中に消費しない。Interaction は別の `expires_at` と retention policy を持つ。
- 応答を受理すると新しい RunSegment を作成する。Worker 障害時の RunAttempt retry と混同しない。
- 通常の応答期限切れは `INTERACTION_EXPIRED` を保存し、推奨 option を採用せず
  `INTERACTION_TIMEOUT` Segment へ進める。Effect approval の期限切れは Proposal を `STALE` にして
  Provider を呼ばない。取消、Project membership 失効、対象 resource の scope 変更でも旧応答/批准を
  再利用せず、安定した公開 error code を残す。

運用確認では Run ID に加えて `run_segment_id`、`run_attempt_id`、`agent_session_id`、`interaction_id` を追跡する。同一 Run で複数 Session が存在すること自体は異常ではないが、初期実装では同時に複数の ACTIVE Session が存在してはならない。

### 8.2 Session resume / fork / replace

- transcript と workspace が完全で、engine/model が互換な場合だけ `resume` する。
- user が比較分岐を選択した場合は `fork` とし、parent session と checkpoint checksum を記録する。
- transcript 破損や engine 非互換時は `replace` を使用し、監査済み checkpoint から新 Session を開始する。無言で最初から再実行しない。
- Session の変更で SkillVersion、Project、permission、resource scope、budget 上限を変更してはならない。変更が必要な場合は新 Run を作成する。

### 8.3 ChangeProposal と承認

- 外部 write は `observe → propose → apply` の順で実行し、Proposal 作成時点では対象 system を変更しない。
- 既定は user approval 必須とする。ADMIN の事前許可は capability、Integration、operation、risk、scope と期限を明示し、scope 外へ一般化しない。
- Approval 受理時に actor、Project、Proposal checksum、対象 revision/updated_at、期限を再検証する。
- User approval は初版では Run 起動者または system ADMIN に限る。他の Project member は 403 となる。
- Provider call は idempotency key を必須とし、retry で comment、Ticket 更新、commit を重複させない。
- 実行後は read-back し、before/after Evidence と verification result を保存する。write 成功・verification 失敗は通常成功として閉じない。
- stale、partial success、rejected、expired はそれぞれ別の公開 error/status とし、運用者が SQL row を手作業で書き換えない。

### 8.4 Redmine CAS adapter と Secret

Integration の `base_url` は接続先 origin だけを保持し、credential は Project の SecretReference が
`ENVIRONMENT` または `/run/secrets` file locator を指す。API/Web は locator、config 本文、Secret 値を
返さない。rotation は新 locator/key version の SecretReference と新 Integration revision/binding を
作成し、既存 Run snapshot を書き換えない。

Effect Worker は書き込み前に `${base_url}/.well-known/projectmind-effect-provider.json` を読み、次を
すべて確認する。

- `protocol=projectmind.redmine-effect/v1`、`provider=redmine`。
- capabilities に `issue.update/v1` が存在する。
- `atomic_precondition=revision`、`idempotency=key`。

不一致、非 JSON、redirect 後の不正 endpoint、stock Redmine のみの構成では write 前に fail closed と
する。適合時だけ pre-read 後、`/projectmind/effects/issue.update/v1/issues/{id}.json` へ exact revision
と idempotency key を送り、APPLIED/REPLAYED 応答後に read-back する。Adapter の retry/保守時も同じ
idempotency key を保持し、手作業で新しい EffectExecution を作らない。

### 8.5 Repository Integration と資源快照物化

`repository` kind の Integration は接続先 URI と既定 revision だけを config に持ち、credential は
Project の SecretReference が指す。運用時の要点は次のとおり。

- **URI scheme**：git は `http` / `https` / `file`、svn は加えて `svn` のみ受理する。ssh 系は鍵管理を
  platform が持たないため登録段階で拒否する。URI に `user:password` を書くことはできない。
- **credential の形式**：Secret 本文は `username:secret` として解釈する。colon が無い場合は token と
  みなし、user 名に `x-access-token` を補う(token 認証は user 名を無視するため)。password 側に colon を
  含めてよい(最初の colon だけで分割する)。
- **凭据の渡し方**：git は環境変数から Authorization header を注入し、svn は stdin へ password を渡す。
  command 行には現れないため `ps` や `/proc/<pid>/cmdline` から読めない。工作 copy は Run workspace の
  外の一時 directory に作り、session 終了時に破棄する。
- **物化の scope**：binding の `paths` が硬境界で、`revisions` は任意の allowlist。空なら Integration の
  `default_revision` を使う。複数指定で既定値を含まない場合は「どの revision を凍結すべきか不定」として
  Run を失敗させるため、運用では 1 件または既定値を含む形にする。
- **前提 command**：backend image は `git` と `subversion` を同梱する。独自 image を使う場合は両方を
  導入する。command 単位の上限は `PROJECTMIND_REPOSITORY_COMMAND_TIMEOUT_SECONDS`(既定 120 秒)。
- **失敗の読み方**：Run の失敗理由は `not_found`(path/revision)、凭据拒否、timeout を安定 code で
  区別する。Provider の stderr 本文は Agent と Evidence に載せないため、詳細は対象 system 側の log で
  確認する。物化された tree は `input/<requirement_key>/.projectmind/manifest.json` に provider、解決済み
  revision、binding checksum、scope、統計、`skipped` を残す。読めなかった file は必ず `skipped` に
  現れるため、「Agent が見落とした」のか「platform が読めなかった」のかはここで判別する。

### 8.6 Repository への書き込みと PR

`repository.write/v1` を有効にする Integration の運用要点。

- **capability の宣言**：Integration が `repository.write/v1` を宣言したときだけ、その Project で
  代码変更提案を apply できる。git と svn の双方に対応する。
- **凭据**：push は SecretReference 必須。読取が匿名 clone で足りる構成でも、書き込みには設定する。
- **落とし方 (`write_mode`)**：既定は **`direct`**——承認済み変更を既定 branch へ直接 commit する
  (git は fast-forward のみで force 不使用、svn は out-of-date で拒否)。闸門は ChangeProposal の
  人手承認であり、この capability は事前許可の対象外。
- **`write_mode=branch`**：分支/PR 評審をもう一段挟みたい場合に選ぶ。`projectmind/` 予約 namespace の
  **新規** branch のみへ書き、`write_branch_prefix` で内側へさらに狭められる (外へは広げられない)。
  同名 branch が別内容で存在すれば `target_branch_conflict` で失敗し、上書きしない。svn の branch
  置き場は平台固定の約定 `<仓库根>/branches/projectmind/`、仓库根は `svn info` から取る。
- **設定時の注意**：① git で write を宣言するなら `default_revision` に**具体的な branch 名**を書く
  (`HEAD` のままだと登録を拒否する。direct は「既定 branch」へ書くため)。② **保護 branch を持つ仓库は
  `write_mode=branch` を明示する**——direct のままだと push が server に拒否される (想定どおりの
  fail closed だが、配置時に決めておく方がよい)。③ 既存 Integration は config に `write_mode` を
  持たないため、既定値 `direct` を継承する。
- **PR/MR**：`forge_kind`(github/gitlab) / `forge_api_base_url` / `forge_project` を**三つとも**設定した
  ときだけ自動で開く。部分設定は登録時に拒否する。未設定なら branch と commit だけが残り、評審は
  forge 上で人が開始する。PR は承認の代替ではない——この capability は事前許可の対象外であり、
  apply 前に必ず人手承認がある。
- **失敗の読み方**：`target_stale` は提案が凍結した base revision が動いたことを表し、再提案が必要。
  `target_branch_conflict` は同名 branch の内容不一致。PR 開設の失敗は commit を巻き戻さない
  (commit は read-back 済み)。

### 8.7 Incident と recovery

外部 Effect の incident では、次を Secret を含めず記録する。

- Run/Segment/Attempt/Session/Proposal/Effect/ToolCall ID。
- image ID、migration revision、Provider version と capability version。
- Proposal checksum、request fingerprint、idempotency key の hash、対象 revision の非機密識別子。
- approval actor ID、policy ID、decision time、apply/read-back status。

対象 system の状態が不明な場合は即時 retry せず、同じ idempotency key で Provider の照会または read-back を行う。適用済みなら保存済み結果を回復し、未適用と確認できた場合だけ再実行する。手動で外部状態を修正した場合も、ProjectMind 側には Evaluation/incident note を追加し、既存 EffectExecution や Result を上書きしない。

### 8.8 MANAGED Secret の KEK 運用

`MANAGED` resolver の SecretReference は、明文を API へ一度だけ渡し、`PROJECTMIND_MANAGED_SECRET_KEK`
の主鍵(KEK)で AES-256-GCM 封入した密文だけを `managed_secret_material` に保存する(`docs/09` §7.2)。
運用上の必須事項は次のとおり。

- **KEK の保管**:KEK は環境変数だけに置き、DB backup・log・Agent 環境へ複製しない。KEK と DB backup を
  同じ保管先へ置かない。**KEK を失うと全 MANAGED 凭据は復元不能**であり、§2 の backup とは別系統で退避する。
- **KEK 形式**:`version:base64key` を `,` で連ねた keyring。先頭が新規封入用の active 鍵、残りは復号専用の旧鍵。
  各鍵は 32 byte 乱数を base64 化する
  (`python -c "import base64,os;print('v1:'+base64.b64encode(os.urandom(32)).decode())"`)。
- **KEK rotation**:新 active 鍵を先頭に、旧鍵を後続へ置いた keyring で `PROJECTMIND_MANAGED_SECRET_KEK` を
  更新し、`python -m projectmind.ops.rotate_secrets` を実行する。全密文が新 active version へ再封入され、
  件数だけが出力される(凭据や KEK は表示しない)。現用 DB の rotation 完了だけで旧鍵を廃棄しない。旧 backup に旧鍵で暗号化した Secret が残る間は、復元用の旧鍵を別の保護された保管先に保持する。現用 keyring から外す場合も、その backup の保持期間と復元手順を確認する。rotation は
  SecretReference の identity を変えないため、既存 Integration/binding/Run snapshot は書き換えない。
- **脅威境界**:MANAGED は DB/backup の単独流出を防ぐ(密文のみでは復号不可)。host/process 全体の攻陷は
  防がない(KEK と密文を同時に取得され得る)。より強い保証が要る場合は外部 KMS/HSM を検討する(現行スコープ外)。
- **fail closed**:KEK 未設定・version 不明・改竄・AAD 不一致では復号せず、既存の credential-unavailable 経路で
  Effect を FAILED に閉じる。KEK 未設定の環境では MANAGED の新規作成も拒否される。

## 10. Schedule と Recovery の監視

Schedule tick (`schedule.tick.completed`) は毎分一度走り、`created` / `skipped` / `failed` を**別々に**
集計する。三つを一つの数字へ潰さないのは、schedule が動いていない理由が「前回がまだ終わっていない
(skipped)」なのか「凍結した版・資源・作成者権限が失効した (failed)」なのかを log だけで判別できるように
するため。`failed` が出た schedule は status が `ERROR` になり発火を止めるので、設定を直して手動で再開する。
`missed_count` が増えているのは停止中に過ぎた回数で、追いかけ実行は行わない (計画 §22 D6)。

Recovery cron は RunAttempt lease、EffectExecution lease、通常 Interaction expiry、effect approval expiry
を独立集計する。`recovered_runs`、`recovered_effects`、`recovered_interactions`、
`recovered_proposals` のいずれかが連続して増える場合は Worker/adapter/通知経路を調査する。技術的な
Effect retry は同じ Proposal、EffectExecution、idempotency key のまま `REQUESTED` へ戻り、上限超過は
fail closed で終了する。
