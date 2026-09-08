# 実行可能なインターフェース契約

本 directory は ProjectMind の公開契約を JSON Schema と代表 example で管理する。仕様の本文は
[文書ガイド](../../docs/README.md)から辿る `../../docs/design/` に置き、ここには機械検証可能な Schema、example、OpenAPI snapshot だけを置く。

## 配置

- 資源別 directory(`runs/`、`skills/`、`projects/`、`events/`、`errors/` など):公開 API・event・error の versioned Schema。
- `tools/<capability>/`:`issue.read/v1` のような versioned Tool capability の request/response/error Schema。
- `agent-task-brief/`、`capability-blueprint/`、`runtime-manifest/`、`outcomes/`、`view-spec/`:Interpreter と Runtime が共有する meta 契約。業務固有 field を追加しない。
- `examples/`:各 Schema の代表 example。
- `fixtures/`:offline 回帰用の固定入力。
- `openapi/projectmind-api.v1.json`:`python3 scripts/export_openapi.py` が現在の FastAPI application から再生成する snapshot。手編集しない。

## データ形状と設計の読み分け

Schema は field/type/required を、[設計](../../docs/README.md)は権限・凍結時点・幂等・失敗時の意味を担当する。両方を満たす必要があり、Schema validation だけで所有権や runtime の保証は証明できない。

内部 JSON 列や作業中 DTO の field をそのまま公開契約と見なさない。文書快照は[資源設計](../../docs/design/resource-snapshots.md)に従って公開投影を明示し、[共有予算](../../docs/design/run-budgets.md)はまだ後続設計として扱う。公開面を変える際は response allowlist、Web validator、version/checksum と歴史互換を確認し、説明用の仮 field を先に既成事実化しない。

[Run 作成](../../docs/design/run-creation.md)の versioned identity は、工作副本の内部 `task_snapshot_json.creation_request` に保存する。これは公開 request に fingerprint を追加する変更ではなく、既存の作成 request/response を使用する。[調度](../../docs/design/task-scheduling.md)の持続在途 occurrence はまだ目標設計であり、同名の公開 Schema や table が存在すると仮定しない。

[入力回执](../../docs/design/resource-snapshots.md#输入准备与可信缓存)には RunInputSnapshot model/migration、Worker/Tool と準備監督の接続があるが、公開 API の追加ではない。内部の snapshot_id、PREPARING/READY や物理世代 path を Run detail の document_snapshots へ混ぜない。作成時の文書選択が FROZEN であることと、実ファイルの準備完了は別の契約である。準備 timeout や開始前 gate も内部実行境界であり、説明のために RunEvent enum や応答へ未定義 field を追加しない。

既存の 201/200/409 の形が正しくても、資源変化や transaction 間の crash に対する保証は別途有状態テストで確認する。データの形、意味、実装状態の三つを混同しない。

[TaskFlowProjection](../../docs/design/task-flow.md#3-taskflowprojection-目标契约) は目標構造であり、この directory に同名 Schema はまだ無い。後続の導入では計画の意味を識別する checksum、純粋な表示 layout、実 event の関連を分け、旧版の未宣言と新版の破損を同じ空値へ畳まない。現行 Blueprint/Brief の追加禁止を仮 field で迂回しない。

公開 field の追加・削除・required 化は[契約変更と結合確認](../../docs/development/contract-workflow.md)に従い、既存 consumer と版互換を確認する。Schema、example、OpenAPI、Web が一致しても、混合版の配備や実 transaction まで検証済みとは扱わない。検証の範囲と残項目は[計画](../../docs/planning/roadmap.md#13-当前执行状态)に記録する。

## Skill の公開と Project 有効化を読む

| データの責任 | 契約と同期先 |
| --- | --- |
| 解釈の意味と frozen 本文 | [Blueprint](capability-blueprint/v1.schema.json) / [Manifest](runtime-manifest/v1alpha1.schema.json) → Backend validator / projector。compatibility の native は Interpreter を省略する許可ではない |
| 公開版と gate の状態 | [SkillVersion](skills/version/v1.schema.json) → [skills route](../backend/src/projectmind/api/routes/skills.py) / [Web validator](../web/src/api/skills.ts)。gate_passed と PUBLISHED は別の事実 |
| Project の可視性 | [enablement](skills/project-enablement/v1.schema.json) → repository / 同 Web validator。現行 response に有効化/無効化の全履歴や再有効化 token があると仮定しない |

状態の意味と[再有効化の目標](../../docs/design/skill-contract.md#112-可审计的重新启用与回滚)は Skill 設計に集約する。追加時は DB の履歴と現在投影、公開 DTO/Schema、Web の確認/競合表示を同時に設計し、既存 disabled_at を消すだけで監査要件を満たしたと扱わない。

## Run 文書契約を読む

| 確認する場面 | Schema / example | 実装で合わせる入口 |
| --- | --- | --- |
| どの文書を選んで新規作成するか | [task-create request](runs/task-create/v1/request.schema.json)、[単一](examples/create-task-run-documents-single.v1.json) / [集合](examples/create-task-run-documents-set.v1.json) / [全集](examples/create-task-run-documents-all.v1.json) | [選択 parser](../backend/src/projectmind/documents/snapshot.py)、[Web 選択](../web/src/lib/documentSelection.ts) |
| 作成済み Run の元の文書一覧を読む | [detail](runs/detail/v1.schema.json)、[文書なしの detail](examples/run-detail.v1.json)、[三状態を含む detail](examples/run-detail-documents.v1.json) | [公開投影](../backend/src/projectmind/runs/resource_projection.py)、[Web validator](../web/src/api/runResources.ts) |
| 履歴一覧で資源摘要を読む | [history](runs/history/v1.schema.json)、[history example](examples/run-history.v1.json) | detail と同じ摘要 allowlist。内部 binding や全メンバーを一覧へ複製しない |

選択 request と凍結 response は逆方向の契約である。detail の checksum を request に付けて授権を省略しない。三状態の意味は[資源設計](../../docs/design/resource-snapshots.md#读取清单和资源摘要)を正本とし、この表へ enum の説明を複製しない。

業務テストは [test_run_documents.py](../backend/tests/contracts/test_run_documents.py) と[実 API 応答](../backend/tests/api/test_run_api.py)、消費側は [runResources.test.ts](../web/tests/api/runResources.test.ts)へ進む。example は固定データであり、その UUID が配備先に存在する保証ではない。

## 同期義務

- Schema・example の追加/変更時は `scripts/validate_contracts.py` と
  `backend/tests/contracts/test_contracts.py` の**両方**の example 表へ登録する。片方だけでは
  example は誰にも検証されない。
- 公開 endpoint の追加/変更後は `scripts/export_openapi.py` で OpenAPI snapshot を回写する。
  一致性は `backend/tests/contracts/` の dict 相等 assertion が守る。
- Backend DTO、Web validator、テストとの同期点の全体は [AGENTS.md](../AGENTS.md) の
  「よくある変更の同期点」を参照する。

## 検証

以下は `contracts/` 内ではなく `PJM/` で実行する。公開 endpoint を変更した場合のみ OpenAPI を再生成する。Schema の存在は Provider/renderer 実装済みの証明にはならない。

```bash
python3 scripts/validate_contracts.py
```
