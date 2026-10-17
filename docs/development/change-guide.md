# コード変更ガイド

作業前に [AGENTS](../../AGENTS.md) と[現在の状態](../planning/roadmap.md#現在の実行状態)を読み、利用シナリオ・課題・完了基準を明確にしてから下表で対象コードを特定する。維持すべき境界は[実行・データ保守ガイド](runtime-guide.md)、詳細な型はソースと Contracts を参照する。

## 課題別のコード入口

Backend パスは `SKM/backend/src/skillmind/` 基準。

| シナリオ | 主な入口 | 確認重点 |
| --- | --- | --- |
| ログイン・アカウント・Project 権限 | auth/、projects/、api/auth_dependencies.py | 元の主体、権限剥奪、CSRF/API Key、資源隔離 |
| Skill 取込と公開 | skills/service.py、skills/source_loader.py、skills/source_storage.py、agent/interpreter* | プレビュー/保存/再構築でパッケージ解析と byte 保存を共有。認可とトランザクションは Service に置き、原文 hash と公開検証を維持 |
| タスク・スケジュール・入力 | runs/、schedules/、agent/context_builder.py | 正確なタスク、元要求のべき等性、凍結選択と物化 |
| 実行・待機・復旧 | agent/、runs/、worker/settings.py（job・lifecycle）、worker/runtime_wiring.py（実行依存の組立） | Segment/Attempt、lease、イベント、取消、結果不明時の扱い |
| 実行履歴・詳細 | runs/query_repository.py | Project 範囲、ページング、元 Run の投影。状態更新は runs/repository.py に置く |
| DB・Git・MCP 書込 | effects/、agent/*_provider.py、agent/*_source.py | 束縛、承認、競合、原操作の回執と回読。提案の証拠照合は effects/proposal_evidence.py、公開投影は effects/projections.py |
| 文書・フォルダ・ごみ箱 | documents/、storage/、runs/ | 原入力、共有参照、復元と完全削除 |
| 結果と評価 | agent/result_validation.py、evaluations/ | 元結果の不変性、参照検証、追記評価 |
| 画面と対話 | web/src/pages、components、hooks、api、lib | 画面構成・要求ライフサイクル・純粋変換の分離、[UI ガイド](../design/workspace.md)、要求と応答の検証 |
| 設定・移行・復元 | core/settings.py、backend/migrations、compose.yml | [配備](../operations/deployment.md)と[復元](../operations/backup-recovery.md) |

## 変更規模の抑制

局所的なスタイル・文言・エラー診断は既存コンポーネントとサービスをそのまま使い、新たなフローを作らない。権限・永続化・公開プロトコルに関わる場合は凍結点、消費者、失敗/復旧、版互換を確認し、[契約変更手順](contract-workflow.md)に従って同期する。

性能改善のために業務書込・承認・回読・結果検証を省略しない。実行速度の課題はまず[性能観測](../operations/run-performance.md)で計測根拠を確認する。

## 検証と交付

[同期点](coding-rules.md#同期点)と[変更に応じた検証](local-development.md#変更に応じた検証)から回帰テストを選ぶ。単体通過・実 DB/ブラウザ/モデル検証・配備済み状態を区別し、失敗・skip・未実施を明記する。恒久規則は該当ガイド、機能状態は Roadmap を更新し、文書生成は[文書維持](documentation.md)に従う。

