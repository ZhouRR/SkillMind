# Skillmind

Skill 原文をタスクとして実行し、入力、資源、結果、根拠、人工評価と外部変更を管理する AI Agent プラットフォーム。

[文書ガイド](../docs/README.md) · [現在の進捗](../docs/planning/roadmap.md#当前执行状态) · [開発規約](AGENTS.md)

## はじめに

| 目的 | 入口 |
| --- | --- |
| 開発と検証 | [ローカル開発](../docs/development/local-development.md) / [変更ガイド](../docs/development/change-guide.md) |
| 起動と更新 | [Quickstart](../docs/operations/quickstart.md) / [配備手順](../docs/operations/deployment.md) |
| API と運用 | [API 利用](../docs/development/api-usage.md) / [Runbook](../docs/operations/runbook.md) / [復元](../docs/operations/backup-recovery.md) |

ソース開発は Python 3.12、Node.js 26 / pnpm 11.7.0。配備先は `images.tar`・`compose.yml`・`.env`・`Makefile` を使い、Docker Compose + make で API/Web と業務・保守 Worker を起動する。宿主の Python/Node は不要。

## Backend

[backend/src/skillmind](backend/src/skillmind/) は API と Worker の共有 package。route は認証と入出力、service は業務処理、repository は永続化、AgentEngine/Provider は実行境界を担当する。

- `auth/`、`projects/`、`documents/`、`skills/`：アカウント、資源・文書管理、Skill 取込と公開。
- `runs/`、`agent/`、`worker/`、`schedules/`：実行、続行、監督、定期実行。
- `effects/`、`evaluations/`、`integrations/`、`storage/`：受控変更、評価、接続と保存。
- [db](backend/src/skillmind/db/) と [migrations](backend/migrations/)：永続形式と移行。

変更時は[維持する実行境界](../docs/development/runtime-guide.md)を確認する。[Backend 手順](../docs/development/local-development.md#backend)と[実 DB テストの前提](../docs/development/local-development.md#実-postgresql-の前提)に従って検証する。

## Web

[web/src](web/src/) は React + TypeScript。pages/components は画面、api は共有 HTTP client、lib/hooks は純粋処理と非同期制御。
タスク一覧で実行と定期実行を管理し、Workspace で待機・進行・最新レポートを確認する。全履歴と結果の詳細は実行履歴へ遷移する。
[UI ガイド](../docs/design/workspace.md#视觉规范)と[ブラウザ回帰](../docs/development/local-development.md#ブラウザ回帰)を参照する。

## Contracts

[contracts](contracts/) に versioned JSON Schema、example、Tool capability、[OpenAPI](contracts/openapi/skillmind-api.v1.json) を置く。[アカウント契約](contracts/users/v1/)などの詳細形状はこちらを参照する。
[契約変更手順](../docs/development/contract-workflow.md)に従い Backend/Web/Worker とテストを同期する。OpenAPI は生成物であり手編集しない。

## Scripts

[scripts](scripts/) は契約・Compose 検査、文書/OpenAPI 生成、image export と隔離 probe の入口。配備先へのコピーは不要。
[検証の選び方](../docs/development/local-development.md#変更に応じた検証)と[文書管理](../docs/development/documentation.md)に実行方法を集約する。

## Skills

[skills](skills/) は system Interpreter と開発 workflow。業務 Skill は実際の内容を取り込み、[原文の凍結と公開](../docs/development/runtime-guide.md#skill-与冻结原文)に従う。
Skill package 内の説明・Schema・script は実行入力であり文書整理の対象外。配置済み file や model 提案は実行権限を意味しない。

## Images

`images/images.tar` は image の移送用で、DB/blob/workspace/暗号鍵の backup ではない。
更新は[image 移送と更新](../docs/operations/quickstart.md#image-移送と更新)、障害からの復元は[一致恢复点](../docs/operations/backup-recovery.md#一致恢复点包含什么)を参照する。
