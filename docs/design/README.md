# 設計の読み順と責任分担

[文書ガイド](../README.md) · [現在の計画](../planning/roadmap.md#当前执行状态) · [変更ガイド](../development/change-guide.md)

設計は保証すべき規則、[contracts](../../SKM/README.md#contracts) は正確な field/type、計画は実装範囲と不足を記す。

## 初めて読むとき

全体は[製品](../overview/product.md) → [構成](../overview/architecture.md) → [領域モデル](domain-model.md)。
一回の実行は[作成](run-creation.md) → [資源](resource-snapshots.md) → [Runtime](agent-runtime.md) → [結果](results-evaluation.md)。

## どの設計を変更するか

| 正本 | 担当 |
| --- | --- |
| [領域モデル](domain-model.md) | 所有者、主要オブジェクト、永続化 |
| [Project](project-lifecycle.md) / [文書](document-lifecycle.md) | 成員・アーカイブ・参照 / upload・配額・清理 |
| [認証](authentication.md) / [ログイン防護](login-protection.md) | Session・CSRF・授権 / 配額・退避 |
| [ユーザー](user-lifecycle.md) / [Secret](secret-storage.md) | 管理・撤銷・監査 / 暗号化・鍵更新 |
| [Skill](skill-contract.md) / [解釈](skill-interpretation.md) | 契約・精確版・有効化 / source・候補・公開 |
| [Run 作成](run-creation.md) / [資源快照](resource-snapshots.md) | 原要求・再送 / 選択・凍結・実 byte |
| [Runtime](agent-runtime.md) / [監督](run-supervision.md) | Segment・Attempt・Session・event / 停止・timeout |
| [回答](user-interactions.md) / [結果と評価](results-evaluation.md) | 答復・期限 / 引用・原値・修訂 |
| [予算](run-budgets.md) / [子分析](subagents.md) | 計量・予約・結算 / 子能力・Session 監査 |
| [受控写入](repository-effects.md) / [調度](task-scheduling.md) | Proposal・承認・CAS・read-back / 発火・在途復旧 |
| [Workspace](workspace.md) / [Task Flow](task-flow.md) | 画面・入力 / 計画と活動の投影 |
| [生成モジュール](generated-modules.md) | 構築 identity・隔離・Host・表示回退 |

## 境界をまたぐ変更の読み方

一つの規則は一つの正本に置き、関連設計はリンクで参照する。
権限・契約・永続化・消費側にまたがる変更は[同期と版互換](../development/contract-workflow.md)を確認する。
