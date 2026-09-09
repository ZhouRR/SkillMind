# ProjectMind 文書ガイド

[ブラウザで読む](index.html) · [コードの入口](../PJM/README.md) · [現在の計画](planning/roadmap.md#当前执行状态)

## 目的から探す

| 目的 | 読む順序 |
| --- | --- |
| 製品を理解する | [概要](overview/product.md) → [システム構成](overview/architecture.md) → [用語](overview/glossary.md) |
| 機能を開発する | [設計の責任分担](design/README.md) → [変更ガイド](development/change-guide.md) → [実装細則](development/coding-rules.md) |
| 環境を用意・API を変更する | [ローカル開発](development/local-development.md) / [API 利用](development/api-usage.md) / [契約変更](development/contract-workflow.md) |
| 起動・更新・障害対応 | [初回起動](operations/quickstart.md) → [公開・移行](operations/deployment.md) / [復旧](operations/backup-recovery.md) / [Runbook](operations/runbook.md) |
| 文書を更新する | [維持と閲覧チェック](development/documentation.md) |

## 配置と責任

- `overview/`：製品・用語・構成。[業務構造図](overview/business-structure.html) / [技術構造図](overview/technical-architecture.html)は全体像の補助。
- `design/`：各領域の規則、失敗・復旧、実装との未解消差距。
- `planning/`：現在の未完了範囲と着手順。設計や検証ログを複製しない。
- `development/`：実装規約、開発と検証の手順。
- `operations/`：起動、公開、backup/restore と症状別の確認。

正式コードは `../PJM/`。設計は保証すべき規則、契約はデータの形、コードと検証は実際の到達点を示す。
「設計済み」「局部実装」「実環境で受入済み」を区別し、未完成の機能を操作手順として案内しない。

## 旧番号の対応

コード注釈の `docs/06` 等は旧文書番号。未掲載の番号を現在の開発規則として使わない。

| ID | 現在の正本 |
| --- | --- |
| 01 | [計画](planning/roadmap.md) |
| 02 / 03 | [業務構造](overview/business-structure.html) / [技術構造](overview/technical-architecture.html) |
| 04 / 05 | [領域モデル](design/domain-model.md) / [Skill 契約](design/skill-contract.md) |
| 06 / 07 | [Runtime](design/agent-runtime.md) / [Workspace](design/workspace.md) |
| 09 / 10 | [認証](design/authentication.md) / [運用](operations/runbook.md) |
| 11 | [解釈・公開](design/skill-interpretation.md) |
