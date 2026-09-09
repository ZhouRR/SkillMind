# 設計の読み順と責任分担

[文書ガイド](../README.md) · [現在の計画](../planning/roadmap.md#当前执行状态) · [コードへの対応](../development/change-guide.md)

各設計は保証すべき規則と未解消差距を記す。正確な field/type は [contracts](../../PJM/README.md#contracts)、実装状況と優先順は計画を確認する。

## 初めて読むとき

- 全体：[製品](../overview/product.md) → [構成](../overview/architecture.md) → [領域モデル](domain-model.md)。
- 一回の実行：[作成](run-creation.md) → [資源](resource-snapshots.md) → [Runtime](agent-runtime.md) → [結果](results-evaluation.md)。
- 変更を始める：下表から責任を持つ設計を選び、[変更ガイド](../development/change-guide.md)でコード・回帰へ進む。

## どの設計を変更するか

| 正本 | 担当する規則 |
| --- | --- |
| [領域モデル](domain-model.md) | 所有者、主要オブジェクトと永続化の境界 |
| [Project](project-lifecycle.md) / [文書資産](document-lifecycle.md) | 選択・メンバー・アーカイブ・参照保護 / upload・配額・blob 清理 |
| [認証](authentication.md) / [ログイン防護](login-protection.md) | Session・CSRF・授権 / 配額・退避・Redis 故障 |
| [ユーザー](user-lifecycle.md) / [Secret](secret-storage.md) | 管理 transaction・撤銷・監査 / 暗号化・resolver・鍵更新 |
| [Skill 契約](skill-contract.md) / [解釈・公開](skill-interpretation.md) | Blueprint・Manifest・精確版・Project 有効化 / source から候補への検証 |
| [Run 作成](run-creation.md) / [資源快照](resource-snapshots.md) | 原要求と再送 / 選択・凍結・準備回执・実 byte |
| [Runtime](agent-runtime.md) / [実行監督](run-supervision.md) | Segment・Attempt・Session・event / 取消・timeout・lease・停止確認 |
| [回答と続行](user-interactions.md) / [結果と評価](results-evaluation.md) | 普通回答・期限・未知提交 / 引用・原値・追加式修訂 |
| [予算](run-budgets.md) / [子分析](subagents.md) | 計量・予約・結算 / 子能力・出力・Session/Tool 監査 |
| [受控書き込み](repository-effects.md) / [調度](task-scheduling.md) | Proposal・承認・CAS・read-back / 発火・並行更新・在途復旧 |
| [Workspace](workspace.md) / [Task Flow](task-flow.md) | ページ・入力・三語 / 計画と実活動の投影 |
| [生成モジュール](generated-modules.md) | 構築 identity・隔離・Host・独立表示回退 |

## 境界をまたぐ変更の読み方

一つの規則は一つの正本に置く。たとえば原要求の再送は Run 作成、入力の固定は資源快照、画面の確認は Workspace が担当し、Worker の技術復旧とは分ける。
権限・契約・永続化・消費側が変わる場合は、[同期と版互換](../development/contract-workflow.md)も確認する。
