---
name: result-judge-triage
description: 実行前に固定した RV 済み Markdown のステップ索引・実測結果・Evidence を照合し、ケース別 Verdict と原因分類を MinIO・PostgreSQL に保存する。実行後の判定や失敗のトリアージに使用する。
---

# Result Judge / Triage

## 入力と環境

必要な入力は `testRunId`、`executionId`、または実行結果のディレクトリ名・ファイル名。実行完了からの継続では追加入力を求めず、実行根拠、期待結果、版、ログ、Evidence を接続済みシステムから取得する。

PostgreSQL、MinIO、Evidence 読み取り、JSON Schema 検証ツールを使う。入力は [execution-result.schema.json](schemas/execution-result.schema.json)、出力は [verdict.schema.json](schemas/verdict.schema.json)（`schemaVersion: 2.0`）に従う。独立した計画ファイル・`sourcePlan`・`test_execution_plan` を要求しない。旧形式は当時の凍結 Skill・Schema で扱い、根拠を後付けして変換しない。既知 Issue の参照機能は利用できる場合に使用する。他の Skill は参照・呼び出しせず、本タスクは実行後の判定・分類と成果保存までを扱う。

## 前処理

1. `test_automation.test_execution` の対象を取得する。`testRunId` 指定は配下の実行、パス指定は実行結果ファイルと DB の `result_ref` を照合する。実際の保存先を使い、`execution-result.json` 等の特定のファイル名を必須にしない。異なる `executionId` を混ぜず、登録のない結果から実行記録を作らない。
2. `QUEUED` / `RUNNING` は待機として返す。`COMPLETED` / `ERROR` / `TIMEOUT` / `ABORTED` の実行を判定対象とする。
3. 外部参照を使わず結果の Schema・format を検証する。`executionBasis` と `versionSnapshot` が実行前の DB 登録値と完全一致し、実行・文書 ID と `specVersion` が対応することを確認する。`executionBasis.sourceMarkdown` の文書を許可範囲で取得し、原バイト列の SHA-256 を検証する。取得不能・不一致は `BLOCKED` とし、現在の文書や旧計画へ置換しない。
4. 登録された全ケース・ステップの ID、依存関係、原文行範囲を確認する。`sourceLines` は操作、`expectedLines` は元の期待条件を指す。範囲の妥当性、ID の一意性、依存先の存在・非循環と対象 Markdown の全件対応を照合する。結果のケース・ステップ集合は索引と完全一致させ、欠落・重複・追加 ID は不整合として扱う。未実行は `NOT_RUN` のまま判定対象に含める。Runner の期待値や実行後の要約で原文の条件を置き換えず、候補 IR・Git の正式 IR を要求しない。
5. `test_case_judgement` の同じ `(execution_id, test_case_id)` を確認し、ケースごとに `RUNNING` で登録して `model_version` を保存する。実行中の処理を重ねず、完了済み判定は入力と成果物を照合して再利用する。登録内容は回読で確認する。

## 判定と分類

各期待条件を実測値と実際に読めた Evidence に照らして判定し、根拠、反証、未確認条件を残す。

| Verdict | 条件 |
| ------- | ---- |
| `PASS` | 全必須ステップが完了し、同じ実行・ケースの Evidence ですべての必須条件を満たす |
| `FAIL` | 観測した結果が期待条件と明確に一致しない |
| `BLOCKED` | 環境や依存サービスが実行を妨げた |
| `INCONCLUSIVE` | 証拠不足、結果の矛盾、判定条件不足で確定できない |

例外・タイムアウト・未実行・欠落を PASS にしない。画像・動画等を読めなければ URL やファイル名から内容を推測せず、confidence で証拠不足を補わない。途中で実行全体が異常終了しても完了済みケースは自身の証拠で判定できるが、実行全体の成功とは表現しない。

Non-PASS の分類は次から選ぶ。

- `PRODUCT`：期待値不一致が確認でき、テスト・環境の誤りでは説明できない。
- `TEST`：期待値、手順、セレクター、データなどテスト側の不備。
- `ENV`：環境、権限、依存サービス等による阻害。
- `FLAKY`：比較可能な版・条件の過去結果が成功と失敗の揺れを裏付ける。単発の失敗だけで断定しない。
- `UNKNOWN`：証拠不足や複数原因が残る。

`failureFingerprint` はプロジェクト、ケース ID、失敗した期待条件、分類、正規化した症状をキー順固定の JSON にして SHA-256 を求め、`sha256:` を付ける。日時・実行 ID・ローカルパス・秘密情報を除外し、`fingerprintBasis` に根拠を残す。PASS の分類と指紋は NULL とする。

PRODUCT の場合だけ `不具合報告.json` を作る。既知 Issue を指紋・症状・版で照合し、一致すれば `KNOWN_ISSUE` と参照・更新候補を示す。一致しなければ新規候補、参照不能なら未確認とする。外部 Issue の作成・更新は明示された別工程で扱う。

## 保存と後処理

新規成果物のディレクトリ名・ファイル名は以下の日本語名を既定とし、明示されたプロジェクト設定を優先する。既存の結果・実行根拠・判定は DB に記録された参照から取得し、再開時も同じ保存先を使う。bucket・ID・JSON フィールド名・Schema 名は変更しない。

1. 出力生成前にケースの `attempt_count` を増やす。Schema に従って `判定結果.json` を生成し、識別子・期待条件・Evidence の対応も検証する。判定と不具合 Payload の `sourceMarkdown` は検証済み `executionBasis.sourceMarkdown`、`versionSnapshot` は実行結果から引き継ぎ、判定モデルは別の `judgementModelVersion` に記録する。製品不具合の Payload は同じ Schema の `$defs/defectPayload` で検証する。
2. `判定結果/{executionId}/{caseKey}/` へ `判定結果.json`、PRODUCT の場合の `不具合報告.json`、処理状態・異常理由を含む `判定処理結果.json` を保存する。`caseKey` は `testCaseId` の UTF-8 内容の SHA-256。prefix はプロジェクト設定に従う。
3. アップロード成功ごとに `verdict_ref` / `defect_ref` / `result_ref` を記録する。参照は `documentLibraryId`、`bucket`、`objectKey` とする。
4. `test_case_judgement` の `verdict` と同じ判定値を含む `verdict_result` を同時に更新し、ケースの `execution_status`、confidence、分類、指紋を対応付ける。検証・全成果物の保存・DB 記録が完了してから `COMPLETED` にし、`status` と `finished_at` は同時に更新する。
5. ケース別の Verdict・処理状態・成果物パス・推奨対応を返す。実行前の索引の全ケースを母数に集計し、未判定件数も示す。

`status` は判定処理、`verdict` はテストの判定として分ける。証拠不足を INCONCLUSIVE と判定・保存できれば処理は COMPLETED でよい。実測がない場合に判定を創作せず、可能な範囲で処理結果だけを保存する。

## 異常と再開

構造化出力の修正は初回を含め最大 3 回とし、そのために Runner を再実行しない。版不一致や設定・ツールの不足は `BLOCKED`、DB・MinIO・判定ツールの実行異常や修正上限は `FAILED`。Runner の状態は変更しない。

保存途中の失敗は同じ実行・ケース・保存先から回復する。前の処理の終了を確認し、累積試行回数を保ち、再開時は `finished_at` を NULL、今回のエラーをクリアする。DB が利用不能なら中断し、ID と保存済みパスをログへ残す。保存後の障害は DB と返却結果へ反映する。

完了済み判定を別モデルで上書きしない。判定改版の履歴は未実装のため、再評価が必要なら別途扱いを決める。
