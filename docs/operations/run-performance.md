# Run 性能観測と最適化

同一タスク・入力・モデル設定・並行条件で所要時間と業務結果を比較してから最適化範囲を決める。`run_id` / `run_attempt_id` を紐付け、初回実行・承認/回答待機・継続を分けて記録する（単発の最速値を全体の効果とみなさない）。

## 固定ログイベント

INFO JSON ログは `run.performance.` 接頭辞を使う。実装入口は[タイマー](../../SKM/backend/src/skillmind/core/timing.py)・[実行器](../../SKM/backend/src/skillmind/worker/executor.py)・[結果検証](../../SKM/backend/src/skillmind/agent/result_validation.py)・[クエリ](../../SKM/backend/src/skillmind/runs/repository.py)。

| イベント接尾辞 | 観測範囲 |
| --- | --- |
| `history_query` / `detail_query` | 履歴/詳細の読取と DTO 投影 |
| `prepare` | Context 準備と Brief 凍結 |
| `catalog_prepare` | Codex client 新規作成前の設定/catalog 準備（プロセス内キャッシュ検証または discovery を含み、SDK 起動は含まない） |
| `sdk_start` / `sdk_thread` / `sdk_turn_start` / `sdk_reuse` | SDK 起動・スレッド/turn 開始時間、およびプロセス再利用状態（`new` / `reused`） |
| `engine_total` / `engine_wait` | エンジン総処理時間 / エンジンイベント待機の累計時間（SDK・モデル・ツール待機を含む） |
| `event_persist` / `realtime_publish` | 永続イベント保存 / ストリーミング文字通知 |
| `result_finalize` / `result_validation` / `result_schema` / `result_references` | 結果収束・完全検証・Schema 検証・参照照合 |
| `terminal_save` / `final_output` | 終端状態の永続化時間 / 最終データ `output_bytes` |
| `receipt_delivery` / `json_selection` | 原回執または JSON ファイルの `source_bytes` とモデル投影 `output_bytes`（`FILE`/`INLINE` を区別） |

時間単位は `duration_ms` であり、`sample_count` はモデル要求数ではなく累計 `await` 区間数である。各区間には包含・並行関係があるため単純合算しない。

## ボトルネック別の対処

- **準備が遅い**：資源読取・凍結入力サイズ・外部接続を確認し、現在の資源で原スナップショットを置き換えない。
- **実行が遅い**：ツール呼出・承認/継続回数・出力サイズを比較し、モデル推論とリモートツール待機を切り分ける。
- **収束が遅い**：Schema・参照照合・終端保存を比較し、`audit.export/v1`（`include_index: true` で小索引を返し完全 Artifact を参照）を活用してモデルによる全文転記を避ける。
- **画面が遅い**：API 応答と描画を分けて観測する。技術詳細は展開時にマウントされるため、ページ内検索の前に該当セクションを展開する。

## 連続処理の観測要点

権限と復旧境界は[実行・データ保守ガイド](../development/runtime-guide.md#限定ツール列とウォーム継続)、検証方法は[ローカル開発](../development/local-development.md)を参照。

- **読取専用短列（`tool.sequence/v1`）**：サブ呼出の統合による往復ターン数の削減を観測する（各サブ呼出は個別に監査・計量される）。
- **Codex ウォーム継続**：同一 Worker job 内で自動承認継続を最大 5 セグメント連続再利用し、`sdk_start` 回数の減少と `sdk_reuse: reused` を観測する。新規 client 作成時は成功した派生 bundled catalog バイト列をプロセス内に最大 32 件（各 ≤1 MiB）キャッシュする（`catalog_prepare` に計上）。
- **自動承認の直接回執**：試験運用時は同一業務での `SESSION_STARTED`/`Segment` 数・モデル呼出間隔・`EFFECT_APPLIED` の `delivery: INLINE` を比較し、Proposal/Approval/Effect/Evidence が欠落なく保持されていることを確認する。

