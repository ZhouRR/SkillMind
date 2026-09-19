# Skillmind 文書ガイド

[ブラウザで読む](index.html) · [コードの入口](../SKM/README.md) · [現在の進捗](planning/roadmap.md#当前执行状态)

主要フローの実装を基準に、利用・開発・運用に必要なガイドをまとめる。今後の改善は実際の利用結果から選ぶ。

## 目的から探す

| 目的 | 入口 |
| --- | --- |
| 製品を使う | [利用の流れと画面](overview/product.md) |
| 全体を理解する | [システム構成と用語](overview/architecture.md) |
| 変更する | [変更ガイド](development/change-guide.md) / [維持する実行境界](development/runtime-guide.md) / [実装細則](development/coding-rules.md) |
| 画面を整える | [UI ガイド](design/workspace.md#视觉规范) |
| 開発・連携する | [ローカル開発](development/local-development.md) / [API 利用](development/api-usage.md) / [契約変更](development/contract-workflow.md) |
| 起動・更新する | [Quickstart](operations/quickstart.md) / [配備](operations/deployment.md) |
| 障害・遅延を調べる | [Runbook](operations/runbook.md) / [復元](operations/backup-recovery.md) / [性能観測](operations/run-performance.md) |
| 進捗・文書を更新する | [Roadmap](planning/roadmap.md) / [文書維持](development/documentation.md) |

## 文書とコードの役割

ガイドには操作手順、判断基準、維持すべき境界を置く。詳細な型・字段・状態遷移は [Contracts](../SKM/README.md#contracts) と実装・回帰テストを参照する。
コード上の実装、隔離テスト、実環境の確認、配備済み状態を区別する。過去の詳細設計と重複する構造図は保持せず、必要な経緯は Git 履歴を参照する。

業務 Skill の `SKILL.md`、参照資料、Schema は実行入力であり、この文書整理の対象に含めない。
