# Skillmind

Skill をタスクとして実行し、結果・根拠・人工評価・外部変更の承認を記録する AI Agent プラットフォーム。

[文書ブラウザ](../docs/index.html) · [設計索引](../docs/design/README.md) · [現在の計画](../docs/planning/roadmap.md#当前执行状态) · [開発規約](AGENTS.md)

## はじめに

| 目的 | 入口 |
| --- | --- |
| 開発環境と検証 | [ローカル開発](../docs/development/local-development.md) / [変更ガイド](../docs/development/change-guide.md) |
| 起動・更新 | [Quickstart](../docs/operations/quickstart.md) / [配備手順](../docs/operations/deployment.md) |
| API・障害対応 | [API 利用](../docs/development/api-usage.md) / [Runbook](../docs/operations/runbook.md) / [復元](../docs/operations/backup-recovery.md) |

ソース開発は Python 3.12、Node.js 26 / pnpm 11.7.0。配備は Windows/Rancher で image を構築し、Linux の Docker Compose + make で実行する（宿主 Python/Node 不要）。
配備先は images.tar・compose.yml・.env・Makefile の四 file だけ。`make deploy` は初回/更新共通で API/Web/Worker を起動し、`make bootstrap-admin` は既存 data を削除せず最初の ADMIN を作成する。

## Backend

[backend/src/skillmind](backend/src/skillmind/) は API と Worker の共有 Python package。
route は認証と入出力、service は業務処理、repository は永続化、AgentEngine/Provider は実行境界を担当する。

- auth・users・projects・documents・skills・compositions：[アカウント](../docs/design/user-lifecycle.md)・[Project](../docs/design/project-lifecycle.md)・[文書資産](../docs/design/document-lifecycle.md)・Skill。
- runs・agent・worker・schedules：実行・監督・調度。[Run 作成](../docs/design/run-creation.md)と [Runtime](../docs/design/agent-runtime.md)。
- evaluations・effects・modules・integrations・storage：評価・外部連携・保存。`db/` と [migrations](backend/migrations/) は永続形式。

起動・検証は[Backend 手順](../docs/development/local-development.md#backend)へ。
全 pytest は専用 DB の作成・強制削除を伴うため、[実 DB の前提](../docs/development/local-development.md#実-postgresql-の前提)を先に確認する。

## Web

[web/src](web/src/) は React + TypeScript。pages・components は画面、api は共有 HTTP client、lib・hooks は純粋処理・非同期制御。
Task Center は実行対象の選択、[Workspace](../docs/design/workspace.md) は一つの Run の観察を担当する。配色・情報密度・操作配置・PC 検証は[全画面の視覚規範](../docs/design/workspace.md#视觉规范)に従う。

環境・proxy・typecheck/Vitest/build・mock browser は[Web 手順](../docs/development/local-development.md#web)へ。画面別の実装状態は計画で確認する。

## Contracts

[contracts](contracts/) に versioned JSON Schema（例：[アカウント](contracts/users/v1/)）、example、Tool capability、[OpenAPI](contracts/openapi/skillmind-api.v1.json) を置く。
形状は契約、権限・凍結・再送の意味は設計が正本。[契約変更手順](../docs/development/contract-workflow.md)に従い、Backend・Web・テストを同期する。OpenAPI は生成物であり手編集しない。

## Scripts

[scripts](scripts/) に契約/Compose の検査、OpenAPI・文書生成、image export の工具を置く。配備先へのコピーは不要。
コマンドと副作用は[検証表](../docs/development/local-development.md#変更に応じた検証)、[文書管理](../docs/development/documentation.md)、[配備手順](../docs/operations/deployment.md)を参照する。

## Skills

[skills](skills/) には system Skill `skillmind-skill-interpreter` と開発 workflow `skm-project-dev` を置く。業務 Skill は配備後に実際の内容を取り込む。合成入力は Backend の tests 専用とし、配備 image に含めない。
各 SKILL.md を入口とし、文書整理では変更しない。配置や script の存在は実行許可ではなく、[公開・Project 有効化](../docs/design/skill-contract.md)が別途必要。

## Images

`images/images.tar` は構築済み image の export であり、DB・blob・workspace・KEK の backup ではない。`-Force` は archive の置換のみ許可する。
[image 移送と更新](../docs/operations/quickstart.md#image-移送と更新)で必要 image・信頼できる移送・旧 archive の保全を確認し、復旧は[同一復元点](../docs/operations/backup-recovery.md#一致恢复点包含什么)を使う。
