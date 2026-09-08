# 設計の読み順と責任分担

[文書ガイド](../README.md) · [システム構成](../overview/architecture.md) · [現在の計画](../planning/roadmap.md#13-当前执行状态) · [コードへの対応](../development/change-guide.md)

この directory は「何を保証するか、失敗したらどう扱うか」の正本を置く。実装状況は計画、正確な field/type は [contracts](../../PJM/contracts/README.md)、検証の記録は [history](../history/README.md) で確認する。設計の存在を実装完了と読み替えない。

## 初めて読むとき

全ファイルを順に読む必要はない。[製品概要](../overview/product.md)と[システム構成](../overview/architecture.md)を読んだ後、担当する体験に応じて進む。

| 理解したい流れ | 最短の読み順 |
| --- | --- |
| Skill が実行可能な Task になるまで | [領域モデル](domain-model.md) → [公開と就緒の判断順](skill-contract.md#发布与就绪的判断顺序) → [解釈・公開の接続](skill-interpretation.md#从候选到项目任务的接线)。版の切替は[回退と再有効化](skill-contract.md#11-版本回滚与评价)を別に確認する |
| ユーザーが一回実行し、結果を見るまで | [領域モデル](domain-model.md) → [Run 作成](run-creation.md) → [資源快照](resource-snapshots.md) → [Runtime](agent-runtime.md) → [Workspace](workspace.md) |
| 既存 Run の回答・批准・故障復旧 | [Runtime の開始・取消境界](agent-runtime.md#74-从领取到模型启动的边界) → [計時器](run-budgets.md#现有计时器的覆盖范围) → [受控書き込み](repository-effects.md)または[子分析](subagents.md)の受入条件 |
| 外部変更を批准し、失敗後の事実を確認する | [四種類の事実](repository-effects.md#先分清四种事实) → [批准要求と結果の UI](workspace.md#审批请求与执行结果) → [不確定結果の回执](repository-effects.md#阶段回执与不确定结果)。批准・commit・PR・DB 保存を分ける |
| 時刻を指定して起動し、保存後の変更を扱う | [規則・発火・Run の具体例](task-scheduling.md#一个例子规则触发与执行分别看) → [管理入口](task-scheduling.md#保存后的管理入口) → [認領・復旧](task-scheduling.md#认领记录与恢复权限)。入力は Workspace、原要求は Run 作成へ渡す |
| 次期の Flow / generated 表示を作る | [Workspace](workspace.md) → [Flow の具体例](task-flow.md#一个例子计划不等于执行事实)と[契約境界](task-flow.md#3-taskflowprojection-目标契约)、または[生成モジュール](generated-modules.md) → [着手条件](../planning/roadmap.md#132-下一步与当前决策) |

以下から各正本へ移動できる。認証と Run 予算は横断的な境界であり、上記の順序で後から権限や上限を付け足すという意味ではない。

## どの設計を変更するか

| 正本 | この文書が決めること | 別の正本へ渡すこと |
| --- | --- | --- |
| [領域モデル](domain-model.md) | 所有者、オブジェクト関係、不変条件、実際の永続載体 | API の field は契約、実行遷移の詳細は Runtime |
| [認証・Secret](authentication.md) | Session、CSRF/Origin、actor 授権、Secret の保護 | Skill の role や説明文は権限を与えない |
| [Skill 契約](skill-contract.md) | source、Blueprint、Manifest、公開 gate、互換性、Project 有効化と回退 | Interpreter と lifecycle の接続責任は解釈・公開 |
| [解釈・公開](skill-interpretation.md) | 解釈入力、候補検証、公開から TaskCatalog への接続 | Task の実行は Run 作成と Runtime |
| [Run 作成・幂等](run-creation.md) | 要求 identity、初回 transaction、再送、原要求の確認 UI | 同一 Run の Attempt 復旧は Runtime、資源内容は快照 |
| [資源快照](resource-snapshots.md) | 選択と凍結、論理/物理 path、Run 全体の準備回执、実 byte の信頼性、ファイル総量 | 準備中の実行監督は Runtime、モデル消費は予算、外部更新は受控書き込み |
| [Agent Runtime](agent-runtime.md) | Segment/Attempt/Session、準備からモデル開始までの監督、Tool、待機、取消、Event/Outbox | 新規要求は Run 作成、入力の完成条件は資源快照 |
| [Run 予算](run-budgets.md) | 局部 limit と共通勘定の区別、主/子の予約・結算・復旧 | ファイル存量は資源快照、組織課金は対象外 |
| [受控書き込み](repository-effects.md) | Proposal/Approval/Effect の区別、決定と実行の identity、CAS、read-back、部分失敗と回执 | ユーザーへの表示は Workspace、配備復旧は Runbook。現行の内容比較を完全な幂等証明と扱わない |
| [TaskSchedule](task-scheduling.md) | 発火時刻、配置の並行更新、occurrence、同 Schedule の重複、遅延・計数・復旧 | Run は普通の作成サービスを使う。管理 UI は Workspace、field/ページングは契約へ渡す |
| [並行子分析](subagents.md) | 能力の縮小、branch の分配、子 Session と根拠の集約 | 共通消費は Run 予算、外部 write は子へ開放しない |
| [Workspace](workspace.md) | ページ責務、入力・待機・結果・三語・可アクセス性 | 要求 identity やサーバーの実行状態を独自に定義しない |
| [Task Flow / Run Flow](task-flow.md) | 計画と実活動、語義 identity と表示 layout、版・凍結・event 関連、歴史 fallback | Run を制御する第二の実行器にはしない |
| [生成モジュール](generated-modules.md) | build、供給元、iframe/Host 境界、投放と fallback | 静的検査だけで実行を許可しない |

この表は責任の索引であり、進捗表ではない。「待実装」は各設計内で現在挙動と分け、着手順・未検証範囲は[計画](../planning/roadmap.md#132-下一步与当前决策)に集約する。

## 境界をまたぐ変更の読み方

例えば「応答が失われた後に再送する」変更は、Run 作成が原要求の同一性を決め、資源快照が初回の内容を守り、Workspace が原要求の確認と新規実行を区別する。Schedule や Worker の復旧を同じ retry として実装しない。

一つの変更では、責任を持つ設計に判断と理由を書き、隣接文書には影響とリンクだけを置く。[変更ガイド](../development/change-guide.md)で実装の入口を選び、公開 field の変更は[契約の同期・版互換](../development/contract-workflow.md)を確認する。各設計の受入条件を観測可能な検証へ落とし、履歴の長文や README に別の設計本文を作らない。
