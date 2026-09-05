# ProjectMind 文書ガイド

[ブラウザで読む](index.html) · [コードの入口](../PJM/README.md) · [文書の更新方法](development/documentation.md)

この文書群は、後続開発で判断を引き継ぐための設計基盤である。初めて読む場合は「製品概要 → システム構成 → 対象領域の設計 → 現在の計画」の順に進む。

## 目的から探す

| 読みたいこと | 入口 |
| --- | --- |
| 何を作る製品か | [製品概要](overview/product.md)、[用語](overview/glossary.md) |
| 全体の仕組みと責務 | [システム構成](overview/architecture.md)、[業務構造図](overview/business-structure.html)、[技術構成図](overview/technical-architecture.html) |
| 次に何を実装するか | [現在の計画と受入残項目](planning/roadmap.md) |
| Skill をどう実行可能にするか | [Skill 契約](design/skill-contract.md)、[解釈・公開の実装](design/skill-interpretation.md) |
| Run・資源・権限を変更する | [領域モデル](design/domain-model.md)、[Agent Runtime](design/agent-runtime.md)、[認証](design/authentication.md) |
| 外部書き込み・時刻起動・子 Agent | [受控書き込み](design/repository-effects.md)、[調度](design/task-scheduling.md)、[並行子分析](design/subagents.md) |
| 画面を変更する | [Workspace](design/workspace.md)、[Task Flow 設計](design/task-flow.md)、[生成モジュール設計](design/generated-modules.md) |
| 開発環境・API・変更手順 | [ローカル開発](development/local-development.md)、[API 利用](development/api-usage.md)、[設計からコードへの対応](development/change-guide.md) |
| 配備・復旧する | [起動と初期管理者](operations/quickstart.md)、[運用 Runbook](operations/runbook.md) |
| JAF の品質を評価する | [JAF 受入 profile](acceptance/jaf-quality.md) |
| 過去の判断を追う | [交付履歴](history/delivery-history.md)、[再編前の計画](history/unified-plan-2026-08-04.md) |

## 配置と責任

```text
docs/
├── overview/      製品・用語・全体構成
├── design/        領域ごとの現行契約と、明示された後続設計
├── planning/      状態・優先順・受入残項目
├── development/   環境構築・API 利用・変更時の参照先
├── operations/    配備・backup・復旧
├── acceptance/    業務別の品質評価条件
├── history/       当時の記録。現在の仕様や状態を上書きしない
├── README.md      人が探すための索引
└── index.html     Markdown から生成したオフライン閲覧版
```

正式コードは同階層の `../PJM/` に置く。本文中の `backend/`、`web/`、`contracts/`、`skills/` は、明記がなければ `PJM/` 起点である。クリックできるリンクは各文書からの相対 path を使う。

仕様は「何を保証するか」、`PJM/contracts/` は「交換するデータの形」、実装とテストは「現在実行できること」を示す。矛盾は影響範囲と根拠を確認して解消し、一方を無条件で正しいと扱わない。現在の進捗は [計画](planning/roadmap.md) §13 に集約する。

## 旧番号の対応

コード注釈の `docs/06 §6.2` のような略記は文書 ID であり、ファイル path ではない。番号を維持した章へこの表から移動できる。旧 PLAN の各章は新計画の同番号から現在の設計へ案内する。

| 旧 ID | 現在の正本 |
| --- | --- |
| 01 | [計画](planning/roadmap.md) |
| 02 / 03 | [業務構造図](overview/business-structure.html) / [技術構成図](overview/technical-architecture.html) |
| 04 | [領域モデル](design/domain-model.md) |
| 05 | [Skill 契約](design/skill-contract.md) |
| 06 | [Agent Runtime](design/agent-runtime.md) |
| 07 | [Workspace](design/workspace.md) |
| 08 | [JAF 受入](acceptance/jaf-quality.md) |
| 09 | [認証](design/authentication.md) |
| 10 | [運用](operations/runbook.md) |
| 11 | [解釈・公開の実装](design/skill-interpretation.md) |
| 12 | [交付履歴](history/delivery-history.md) |
| 13 | [旧 Roadmap 図](history/roadmap-2026-08-04.html)。現在の状態は 01 §13 |

`PJM/` 内の README は各実装の案内、[AGENTS.md](../PJM/AGENTS.md) は変更規約を担当する。`skills/**/SKILL.md` とその references は Interpreter 入力・実行資産のため、この文書体系へ移動しない。
