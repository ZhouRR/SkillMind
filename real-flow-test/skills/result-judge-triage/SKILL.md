---
name: result-judge-triage
description: 実行前に固定した仕様書 Markdown・実行根拠とテスト実行器の原操作記録・画面証跡を直接照合し、利用可能な業務知識に基づいてケース別の合否判定（Verdict）と原因分類を保存する。テスト実行後の結果判定および失敗原因のトリアージに使用する。
---

# テスト結果判定・原因分類（Result Judge / Triage）

## 目的と役割

テスト実行工程で固定された仕様書 Markdown・実行根拠（`execution_basis`）と、実行器（Runner）が記録した原操作記録・画面証跡の参照索引（`result_ref`）を直接照合し、業務知識に基づいて各テストケースの合否（Verdict）と失敗原因の分類を判定・保存する。他の Skill の呼び出しや、テスト環境に対する新規の起動・画面操作・再実行は一切行わない。

- **参照 Schema**：
  - 実行根拠：[execution-basis.schema.json](../../schemas/execution-basis.schema.json)
  - 原記録の参照索引（`result_ref`）：[runner-records.schema.json](../../schemas/runner-records.schema.json)（`schemaVersion: "2.0"`）
  - ケース別判定出力（`判定結果.json`）：[verdict.schema.json](../../schemas/verdict.schema.json)（`schemaVersion: "3.0"`）
  - 製品不具合候補出力（`不具合報告.json`）：[defect.schema.json](../../schemas/defect.schema.json)（`schemaVersion: "3.0"`）
- **一貫完遂**：対象のテスト実行が終端状態であれば、前処理や一部の確認だけでタスクを途中終了せず、全対象ケースの判定・成果物保存・管理記録の更新まで同一実行内で完遂する。

## リソースの確立と業務知識の段階的参照

1. **対象実行と原資料の確立**
   - 実行コンテキストで指定された `executionId`、`testRunId`、または原記録参照から対象のテスト実行を特定し、管理記録で終端状態（`COMPLETED` / `ERROR` / `TIMEOUT` / `ABORTED`）にあることを確認する（`QUEUED` / `RUNNING` の場合は実行中として待機を返す）。
   - 管理記録から登録済みの `execution_basis`、`version_snapshot`、RV の `specVersion`、および `result_ref` を取得し、対応する仕様書 Markdown（`sourceMarkdown`）の原バイト列と SHA-256 ハッシュを照合する。判定は原仕様書と原操作記録・証跡のみを根拠とする。
   - **データベースの使い分け**：
     - **テスト管理記録**：実行情報・参照索引の取得およびケース別判定結果の登録・更新に使用する。
     - **被測システムの業務データベース（任意・読取専用）**：仕様書の期待条件や失敗原因の確認に必要な場合のみ、読取専用で参照する。業務データの変更は行わず、業務データベースが未接続・参照不能な場合はその証拠を必須とする判定項目のみを不足として扱い、独立して判定可能なケースは継続する。

2. **業務知識の段階的参照（全件一括読取の禁止）**
   - **開始時の最小読取**：処理開始時はプロジェクト文書庫の業務知識索引（`INDEX.md`）と共通の仕様書表記・改訂規則のみを読む。知識庫内の全ファイルを無差別に一括列挙・連続読取しない。
   - **実操作ケースに応じた選択読取**：実際に操作が行われたテストケースの期待条件照合や失敗原因の切り分けに必要な対象画面・業務機能の文書のみを `INDEX.md` の収録一覧から選択してオンデマンドで読み取り、同一実行内で再利用する。共通の前置阻害等で未実行（`notRun`）となったケース群の整理では、無関係な業務詳細文書を追加読取しない。

## ツールの使用原則

- **大判文書・原記録の効率的読取**：仕様書 Markdown や大きな原操作記録を読む際は、数十行ずつの細切れ読取を避け、大きな読取範囲やケースの行範囲指定、構造化抽出機能（JSON Pointer 等）を使って判定に必要な項目を漏れなく確認する。スクリーンショットは実際に画像として内容を閲覧できた場合のみ視覚的証拠として引用し、ファイル名やパス文字列だけで内容を確認済みにしない。
- **バッチ処理による効率的な記録更新**：複数ケースの判定レコード確認・開始登録（`RUNNING`）・試行回数加算・完了更新（`COMPLETED`）を行う際は、ケースごとに個別のクエリを繰り返すのではなく、バッチ SQL（`INSERT ... VALUES` や `UPDATE ... FROM (VALUES ...)` 等）を用いて一括で登録・更新・回読する。

## 結果判定とトリアージのフロー

1. **母集団と参照索引（`result_ref`）の確認**
   - `execution_basis.cases[]` に登録された全有効ケース（`testCaseId`、操作・期待条件の行範囲 `sourceLines` / `expectedLines`、依存関係 `dependsOn`）を判定の母集団として確定する。途中のエラーや未実行を理由に母集団を縮小しない。
   - `result_ref`（`schemaVersion: "2.0"`）の `operations`・`notRun`・`gaps`・`reasons` を確認する。`notRun` や `gaps` において `stepId: "*"` が指定されている場合は当該 `testCaseId` の全ステップへの適用として扱い、各 `reasonRef` を `reasons` の定義および個別の `detail` / `evidenceRefs` と対応付ける。

2. **原実測と期待条件の照合**
   - 実行された操作（`operations`）については、索引の `locator`（または検証済みの退避先 `archive`）から公開読取ツールを用いて原操作回執・ログ・画面証跡を取得し、要求 ID・接続・ハッシュを照合する。
   - 各ケースの行範囲内の有効ステップについて、仕様書の期待条件と原実測を比較する。操作ツールの単純な成功ラベルだけで業務仕様全体の成立とみなさず、画面表示・遷移・メッセージ・データ状態を根拠として照合する。未送信が確認できたステップは `NOT_RUN`、送信済みで結果が確定しないものは不明（`executionStatus: null`、`satisfied: null`）として区別する。

3. **合否判定（Verdict）と原因分類（Classification）**
   - 各テストケースについて、実測証拠・反証・未確認事項に基づき以下の基準で判定する：

| Verdict | 判定条件 | 非 PASS 時の分類（`PASS` 時は分類・指紋とも `null`） |
| --- | --- | --- |
| `PASS` | 全必須ステップが完了し、同一実行の原記録・証跡ですべての期待条件を満たし、未確認事項や矛盾がない | `null` |
| `FAIL` | 観測した実測結果が期待条件と明確に一致しない | `PRODUCT`：期待値不一致が確認でき、テスト仕様・手順・環境の誤りでは説明できない製品不具合<br>`TEST`：期待値、手順、セレクター、テストデータなどテスト側の不備<br>`FLAKY`：比較可能な同一条件の過去結果から成功と失敗の揺れが裏付けられる場合（単発失敗だけで断定しない）<br>`UNKNOWN`：証拠不足や複合要因で原因を特定できない |
| `BLOCKED` | 環境、権限、依存サービス、または先行ステップの失敗により実行・確認が妨げられた | `ENV`（環境・権限・依存サービス等による阻害）ほか該当分類 |
| `INCONCLUSIVE` | 証跡不足、結果の矛盾、判定条件不足により合否を確定できない | `UNKNOWN` ほか該当分類 |

   - **失敗指紋（`failureFingerprint`）**：`PASS` 以外のケースでは、プロジェクト、ケース ID、失敗した期待条件、分類、および正規化した症状をキー順固定の JSON として SHA-256（`sha256:<64桁小文字16進数>`）を算出し、`fingerprintBasis` にその根拠を記録する（日時・実行 ID・一時パス・秘密情報は含めない）。
   - **製品不具合候補と既知 Issue 照合**：分類が `PRODUCT` の場合のみ、利用可能な既知 Issue と指紋・症状・版を照合し（一致は `KNOWN_ISSUE`、不一致は `NEW_CANDIDATE`、照合不能は `CHECK_REQUIRED`）、[defect.schema.json](../../schemas/defect.schema.json) に従う `不具合報告.json` を作成する。

4. **判定成果物の生成・検証・保存と一括更新**
   - **出力の生成と精簡（[verdict.schema.json](../../schemas/verdict.schema.json)）**：
     - `sourceMarkdown` と `versionSnapshot` は実行登録の検証済み値を引き継ぎ、判定に使用したモデル版を `judgementModelVersion` に記録する。
     - **期待条件の再転記禁止**：元の期待条件は `sourceMarkdown` と `execution_basis.cases[].sourceLines` で固定済みのため、`assessments[].expectedResult` に仕様書の長文を複写しない（省略可）。
     - **未実行・共通阻害ケースの集約**：実操作が行われたケースや `PASS` ケースでは対象ケースの全ステップを `assessments` に記録し、確認した原記録参照を `evidenceRefs` に付ける。一方、**ケース全体が同一理由で未実行（`result_ref.notRun` の `stepId: "*"`）または共通の前置阻害で `BLOCKED` / `INCONCLUSIVE` となる場合は、個別ステップを展開せず `assessments` を `{"stepId": "*", "executionStatus": "NOT_RUN", "satisfied": null}` の 1 要素に集約（または空配列）する**。共通理由で同一構造となる多数の未実行ケースは共通テンプレートとケース別差分で効率的に生成・検証する。
   - **成果物の保存と管理記録の一括更新**：
     - 各ケースの保存先（`判定結果/{executionId}/{caseKey}/`、`caseKey` は `testCaseId` の UTF-8 SHA-256）へ `判定結果.json`、`PRODUCT` 時の `不具合報告.json`、および `判定処理結果.json` を保存し、文書庫内の論理相対パス（`objectKey`）で参照を記録する。
     - 管理記録のケース別判定テーブルに対し、バッチ更新で `verdict`・`verdict_result`・成果物参照・処理完了状態（`status = COMPLETED`）・終了日時（`finished_at`）を一括反映・回読する。
   - **最終報告**：全対象ケースを母数として Verdict 別・分類別の集計、主要な失敗・阻害の要因、成果物の保存先パス、および推奨対応を簡潔な最終報告にまとめて返却する。
