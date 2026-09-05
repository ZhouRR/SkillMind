# ProjectMind

Skill をプロジェクトのタスクとして実行し、根拠・結果・人工評価・外部変更の承認を記録する AI Agent プラットフォーム。

[文書をブラウザで読む](../docs/index.html) · [設計ガイド](../docs/README.md) · [現在の計画](../docs/planning/roadmap.md#13-当前执行状态)

## はじめに

| 目的 | 手順 |
| --- | --- |
| 製品と全体構造を理解する | [製品概要](../docs/overview/product.md)、[システム構成](../docs/overview/architecture.md) |
| ローカルで開発する | [環境構築と検証](../docs/development/local-development.md) |
| Compose で起動する | [起動と初期管理者](../docs/operations/quickstart.md) |
| API を利用する | [API 利用ガイド](../docs/development/api-usage.md) |
| 既存環境を更新・復旧する | [運用 Runbook](../docs/operations/runbook.md) |
| 設計からコードへ進む | [変更ガイド](../docs/development/change-guide.md)、[AGENTS.md](AGENTS.md) |
| 文書を更新・検証する | [文書の管理方法](../docs/development/documentation.md) |

## コード構成

```text
PJM/
├── backend/       API・Worker・domain・repository・migration・test
├── web/           React 画面・API client・純粋な表示 logic・三語文案
├── contracts/     JSON Schema・example・OpenAPI
├── skills/        system Skill・example・業務 Skill の入力 package
├── scripts/       契約/Compose/SDK/文書検証、image export
├── images/        配備用 image archive
├── compose.yaml   service と共有 Traefik への接続
└── AGENTS.md      コード変更時の拘束規約
```

[Backend](backend/README.md) · [Web](web/README.md) · [Contracts](contracts/README.md) · [Skills](skills/README.md) · [Images](images/README.md)

製品仕様の正本は sibling の `../docs/`。この README は入口とコード配置を担当し、endpoint 一覧や詳細設計を複製しない。

## 実行前に知ること

Backend は Python 3.12、Web は Node.js 26 / pnpm 11.7.0 を使う。Compose は既存の共有 Traefik が前提で、ProjectMind 自身の host port は公開しない。

`make run` は配置済み image を起動し、build しない。Worker の業務 dispatch は既定 false。新規 Project は SkillVersion を明示有効化し、資源を設定してから実行する。

初期 ADMIN を作る通常手順は `python -m projectmind.ops.bootstrap_admin`。`make bootstrap-admin` は全 volume データを消す再初期化操作なので、単なる管理者作成には使わない。正確な実行位置と手順は[起動案内](../docs/operations/quickstart.md#最初の-admin-を作成する)を参照する。

設計上の保証と実装の差距、未完了の外部システム・モデル・ブラウザ検証は[計画 §13](../docs/planning/roadmap.md#13-当前执行状态)に記録する。
