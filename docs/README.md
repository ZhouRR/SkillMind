# Skillmind 文書ガイド

[ブラウザで読む](index.html) · [コードの入口](../SKM/README.md) · [現在の計画](planning/roadmap.md#当前执行状态)

## 目的から探す

| 目的 | 入口 |
| --- | --- |
| 製品を理解する | [概要](overview/product.md) → [構成](overview/architecture.md) → [用語](overview/glossary.md) |
| 機能を開発する | [設計索引](design/README.md) → [変更ガイド](development/change-guide.md) |
| 環境・API | [ローカル開発](development/local-development.md) / [API 利用](development/api-usage.md) / [契約変更](development/contract-workflow.md) |
| 起動・運用 | [Quickstart](operations/quickstart.md) / [配備](operations/deployment.md) / [復元](operations/backup-recovery.md) / [Runbook](operations/runbook.md) |
| 文書を更新する | [文書維持](development/documentation.md) |

## 配置と責任

`overview/` は全体像、`design/` は規則、`planning/` は現在の不足、`development/` は開発手順、`operations/` は運用手順。
[業務構造図](overview/business-structure.html)と[技術構造図](overview/technical-architecture.html)は全体像の補助資料である。

設計は要求、契約は形状、コードと検証は到達点を示す。設計済み・実装済み・実環境で受入済みを区別する。

## 旧番号の対応

コード注釈に残る旧番号の案内のみ。歴史文書や現行仕様の別版ではなく、注釈を一括 path 置換しない。

| ID | 現在の正本 |
| --- | --- |
| 01 | [計画](planning/roadmap.md) |
| 02 / 03 | [業務構造](overview/business-structure.html) / [技術構造](overview/technical-architecture.html) |
| 04 / 05 | [領域モデル](design/domain-model.md) / [Skill 契約](design/skill-contract.md) |
| 06 / 07 | [Runtime](design/agent-runtime.md) / [Workspace](design/workspace.md) |
| 09 / 10 | [認証](design/authentication.md) / [運用](operations/runbook.md) |
| 11 | [解釈・公開](design/skill-interpretation.md) |
