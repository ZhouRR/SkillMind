# システム構成と用語

Backend はモジュラーモノリスであり、API・業務 Worker・保守 Worker が同一 Python package を共有して個別プロセスで動作する。業務層は `AgentEngine` 抽象に依存し、SDK アダプタが Codex/Claude との通信を担う。

## 実行経路

```text
Web / API 呼出元 → Traefik → FastAPI → ドメインサービス → PostgreSQL
                                           ↓ Outbox
                                 Redis キューと通知
                                   ↓          ↓
                              業務 Worker  保守 Worker
                                   ↓
                            AgentEngine → モデル
                                   ↓
                          Tool Gateway / Provider
                                   ↓
                         束縛・認可された外部資源
```

- **Skill**：原文取込 → 最小実行宣言 → 審査・公開 → Project 有効化 → タスク登録（旧 Blueprint は原版互換を維持）。
- **Run**：バージョン・入力・権限上限を凍結 → キュー投入 → 読取専用入力の物化 → 実行 → 回答/承認待ち、または結果提出。
- **外部変更**：観測（observe） → 提案（propose） → 人工または適用可能な自動承認 → 実行（apply） → 回読と回執保存。

PostgreSQL が業務と監査の正本であり、Redis はキュー・短期ロック・通知・ログイン防護を担う。文書 blob と Run workspace は別ストレージであり同一トランザクションではない。復元は[一致復元点](../operations/backup-recovery.md#一致復元点の内容)を参照する。

## 主要用語

| 名称 | 意味 |
| --- | --- |
| Organization / Project | 組織資産とプロジェクトのアクセス境界 |
| SkillVersion / Task | 不変の公開バージョンと、そのバージョンが提供するタスク入口 |
| Integration / ResourceBinding | 外部接続と実行時に許可された資源範囲（資格情報はモデルに渡さない） |
| Run / Segment / Attempt | 1 回の業務要求、分析/継続フェーズ、同一フェーズ内の技術的試行 |
| Brief / Snapshot | 凍結された実行指示と入力事実（現在設定で過去値を埋め戻さない） |
| Interaction / Proposal | 通常の質問と外部変更提案（回答プロトコルが異なる） |
| Effect / Receipt | 実際の外部変更と原操作の回執（回執は外部の現在状態を保証しない） |
| Evidence / Artifact | 追跡可能な観測証拠と保存済み成果物ファイル |
| Result / Evaluation | 不変の実行結果と追記式の人工評価・修正 |

コード配置は[変更ガイド](../development/change-guide.md)、詳細スキーマは [Contracts](../../SKM/README.md#contracts)、実行制約は[実行境界](../development/runtime-guide.md)、配備構成は[配備手順](../operations/deployment.md)を参照する。

