# ProjectMind 文書ガイド

[ブラウザで読む](index.html) · [コードの入口](../PJM/README.md) · [文書の更新方法](development/documentation.md)

この文書群は、後続開発で判断を引き継ぐための設計基盤である。初めて読む場合は「製品概要 → システム構成 → 対象領域の設計 → 現在の計画」の順に進む。

## 目的から探す

| 読みたいこと | 入口 |
| --- | --- |
| 初めて製品を理解する | [製品概要](overview/product.md) → [システム構成](overview/architecture.md) → [用語](overview/glossary.md) |
| 担当する機能の設計を読む | [設計ガイド](design/README.md)：認証/アカウント、Skill、Run、資源、画面、調度の読み順と責任分担 |
| 次の開発・未完了の変更を引き継ぐ | [現在の計画](planning/roadmap.md#13-当前执行状态) → [変更ガイド](development/change-guide.md) → [コードの入口](../PJM/README.md#コード構成) |
| 開発環境を用意・API を変更する | [ローカル開発](development/local-development.md)、[契約変更](development/contract-workflow.md)、[API 利用](development/api-usage.md)。資料が食い違う場合は[交付の接線確認](development/contract-workflow.md#遇到未接齐的交付链) |
| 配備・障害対応・復旧する | [起動案内](operations/quickstart.md)、[公開・移行](operations/deployment.md)、[backup・復元](operations/backup-recovery.md)。障害時は [Runbook](operations/runbook.md#按问题找入口) |
| 業務品質を評価する | [JAF 移行・運行受入](acceptance/jaf-quality.md) → [Benchmark](acceptance/jaf-benchmark.md)：実行入力、Gold 隔離、指標と人工判定を分ける |
| 文書を直す・閲覧を確認する | [文書管理とブラウザ検証](development/documentation.md)、[script の案内](../PJM/scripts/README.md) |
| 過去の判断や検証を確認する | [履歴の索引](history/README.md)。当時の証拠だけを読み、現在状態は計画へ戻る |

この表は最初の入口だけを示す。個別の失敗条件や実装箇所は設計ガイド・変更ガイド・Runbook に任せ、総索引へ重複させない。具体的な用語や file 名が分かる場合は、[ブラウザ版](index.html)の全文検索から該当章へ進める。

## 配置と責任

```text
docs/
├── overview/      製品・用語・全体構成
├── design/        README で責任を選ぶ → 領域別の現行規則と後続設計
├── planning/      状態・優先順・受入残項目
├── development/   環境構築・API 利用・変更時の参照先
├── operations/    初回起動 / 公開・移行 / backup・復元 / 症状別 Runbook
├── acceptance/    業務別の品質評価条件
├── history/       当時の記録。現在の仕様や状態を上書きしない
├── README.md      人が探すための索引
└── index.html     Markdown から生成したオフライン閲覧版
```

正式コードは同階層の `../PJM/` に置く。本文中の `backend/`、`web/`、`contracts/`、`skills/` は、明記がなければ `PJM/` 起点である。クリックできるリンクは各文書からの相対 path を使う。

仕様は「何を保証するか」、`PJM/contracts/` は「交換するデータの形」、実装とテストは「現在実行できること」を示す。矛盾は影響範囲と根拠を確認して解消し、一方を無条件で正しいと扱わない。現在の進捗は [計画](planning/roadmap.md) §13 に集約する。

## 本文の状態を読み分ける

| 表現 | 読み方 |
| --- | --- |
| 現行 / 現在のコード | 指定時点の工作副本。未完了の編集や失敗中のテストもあり得る。配備先と同じとは限らない |
| 修正要求 / 目標 / 待実装 | 後続開発の判断基準。現行 API の操作説明ではない |
| 本地回帰 / 専項受入 | 記録した条件だけの検証結果。skip や未実施は成功に数えない |
| 歴史 | 当時の証拠。現在の状態は計画、現在の判断は対象設計で確認する |

初めて実装を引き継ぐ場合は、[システム構成](overview/architecture.md)で責任を把握し、[計画 §13.3](planning/roadmap.md#133-全项目重构与缺失功能实施2026-09-05-启动)で担当領域を選び、[変更ガイド](development/change-guide.md)からコードと検証へ進む。全履歴を読み通す必要はない。

## 旧番号の対応

コード注釈の `docs/06 §6.2` のような略記は文書 ID であり、ファイル path ではない。番号を維持した章へこの表から移動できる。旧 PLAN の各章は[計画末尾の引用索引](planning/roadmap.md#旧章节引用索引)から現在の設計へ案内する。現在状態の §13 は計画の先頭に置き、古い章を順番に読む必要はない。

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
