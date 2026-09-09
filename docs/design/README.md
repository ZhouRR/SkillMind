# 設計の読み順と責任分担

[文書ガイド](../README.md) · [システム構成](../overview/architecture.md) · [現在の計画](../planning/roadmap.md#13-当前执行状态) · [コードへの対応](../development/change-guide.md)

この directory は「何を保証するか、失敗したらどう扱うか」の正本を置く。実装状況は計画、正確な field/type は [contracts](../../PJM/contracts/README.md)、検証の記録は [history](../history/README.md) で確認する。設計の存在を実装完了と読み替えない。

## 初めて読むとき

全ファイルを順に読む必要はない。[製品概要](../overview/product.md)と[システム構成](../overview/architecture.md)を読んだ後、担当する体験に応じて進む。

| 理解したい流れ | 最短の読み順 |
| --- | --- |
| ログイン・権限・外部の認証情報を扱う | [四種類の credential](authentication.md#先分清四类凭据)から、ログイン前は[入口防護の例](login-protection.md#一个例子一次登录两次入口请求)と[提出・離頁](login-protection.md#提交离页与结果未知)、ログイン後は[二つのページの例](authentication.md#一个例子同一账号打开两个页面) → [v2 と切替](authentication.md#会话凭据-v2-与切换要求) → [認証と業務提交](authentication.md#认证与业务提交不是同一个事务)へ。外部 key は別の [Secret 設計](secret-storage.md)へ進む |
| アカウントの作成・停止・改密を接続する | [停止と再有効化の例](user-lifecycle.md#一个例子停用再启用不恢复旧登录) → [内部実装と公開入口](user-lifecycle.md#工作副本与公开入口) → [管理 transaction](user-lifecycle.md#事务与并发) → [失敗と画面](user-lifecycle.md#生效与界面)。ProjectMember、ログイン配額、永続失効を分ける |
| Skill が実行可能な Task になるまで | [領域モデル](domain-model.md) → [公開と就緒の判断順](skill-contract.md#发布与就绪的判断顺序) → [解釈・公開の接続](skill-interpretation.md#从候选到项目任务的接线)。版の切替は[回退と再有効化](skill-contract.md#11-版本回滚与评价)を別に確認する |
| ユーザーが一回実行し、結果を見るまで | [領域モデル](domain-model.md) → [Run 作成](run-creation.md) → [資源快照](resource-snapshots.md) → [Runtime](agent-runtime.md) → [Workspace](workspace.md) |
| 既存 Run の回答・批准・故障復旧 | [Runtime の開始境界](agent-runtime.md#74-从领取到模型启动的边界) → [取消後の具体例](run-supervision.md#一个例子点击取消之后)と[提交の判断点](run-supervision.md#提交时谁决定最终状态) → [受控書き込み](repository-effects.md)または[子分析](subagents.md)の受入条件 |
| 主/子の予算を制限し、結果と用量を読む | [結果の具体例](subagents.md#一个例子完成的是哪一层) → [子指令/出力](subagents.md#子任务指令与结果的边界) → [数値例](run-budgets.md#一个例子已用占用与可用)と[用量経路](run-budgets.md#用量现在流向哪里) → [起動/結算](run-budgets.md#启动与结算的提交边界)。分配・消費・成功を分ける |
| 外部変更を批准し、失敗後の事実を確認する | [四種類の事実](repository-effects.md#先分清四种事实) → [批准要求と結果の UI](workspace.md#审批请求与执行结果) → [不確定結果の回执](repository-effects.md#阶段回执与不确定结果)。批准・commit・PR・DB 保存を分ける |
| 時刻を指定して起動し、保存後の変更を扱う | [規則・発火・Run の具体例](task-scheduling.md#一个例子规则触发与执行分别看) → [管理入口](task-scheduling.md#保存后的管理入口) → [認領・復旧](task-scheduling.md#认领记录与恢复权限)。入力は Workspace、原要求は Run 作成へ渡す |
| 次期の Flow を作る | [Workspace](workspace.md) → [Flow の具体例](task-flow.md#一个例子计划不等于执行事实)と[契約境界](task-flow.md#3-taskflowprojection-目标契约) → [着手条件](../planning/roadmap.md#132-下一步与当前决策) |
| 生成界面を build・表示・回退する | [三つの module/preview](generated-modules.md#先分清三种模块与预览) → [表示故障の例](generated-modules.md#一个例子图表坏了任务没有失败) → [構築の提交](generated-modules.md#构建输入与结果的提交) → [Host と古い応答](generated-modules.md#挂载切换与晚到消息) → [表示選択と全体停止](generated-modules.md#展示选择与全局停用)。preview も初回実行 gate の対象 |

以下から各正本へ移動できる。認証と Run 予算は横断的な境界であり、上記の順序で後から権限や上限を付け足すという意味ではない。

## どの設計を変更するか

| 正本 | この文書が決めること | 別の正本へ渡すこと |
| --- | --- | --- |
| [領域モデル](domain-model.md) | 所有者、オブジェクト関係、不変条件、実際の永続載体 | API の field は契約、実行遷移の詳細は Runtime |
| [認証と Session](authentication.md) | 密碼、Session、CSRF/Origin、現在の actor/Project 授権、失効と多ページ | ログイン前の配額は入口防護、外部 credential は Secret、実行の停止は実行監督 |
| [ユーザー lifecycle](user-lifecycle.md) | アカウント版、最後の活動 ADMIN、原会話の再認証、管理/失効/監査の原子性、結果不明の扱い | password/Session protocol は認証、共用配額は入口防護、公開 field は契約。API と画面の接線を別々に確認する |
| [ログイン入口防護](login-protection.md) | 来源・account・組合の計数、有限退避、Redis 故障、429/503 と client の責任 | Session の失効は認証、配備環境の分診は Runbook。配額の消失を撤権や復元完了と扱わない |
| [Secret の保存と鍵更新](secret-storage.md) | resolver、非公開情報、実際の暗号化形式、鍵と backup の互換性 | ログインは認証、binding は資源設計、実操作は Runbook |
| [Skill 契約](skill-contract.md) | source、Blueprint、Manifest、公開 gate、互換性、Project 有効化と回退 | Interpreter と lifecycle の接続責任は解釈・公開 |
| [解釈・公開](skill-interpretation.md) | 解釈入力、候補検証、公開から TaskCatalog への接続 | Task の実行は Run 作成と Runtime |
| [Run 作成・幂等](run-creation.md) | 要求 identity、初回 transaction、再送、原要求の確認 UI | 同一 Run の Attempt 復旧は Runtime、資源内容は快照 |
| [資源快照](resource-snapshots.md) | 選択と凍結、論理/物理 path、Run 全体の準備回执、実 byte の信頼性、ファイル総量 | 準備中の実行監督は Runtime、モデル消費は予算、外部更新は受控書き込み |
| [Agent Runtime](agent-runtime.md) | Segment/Attempt/Session、モデル開始、Tool、待機、Event/Outbox | 新規要求は Run 作成、入力の完成条件は資源快照、停止の詳細は実行監督 |
| [実行監督と停止](run-supervision.md) | 取消・timeout・失効 lease の分類、task/client の清理、終態と停止確認の分離 | Run 状態は領域モデル、秒数と用量結算は予算、独立 Effect は受控書き込み |
| [Run 予算](run-budgets.md) | 局部 limit と共通勘定、計量の正規化、主/子の予約・起動・結算・復旧 | ファイル存量は資源快照、子結果の成否は子分析、組織課金は対象外 |
| [受控書き込み](repository-effects.md) | Proposal/Approval/Effect の区別、決定と実行の identity、CAS、read-back、部分失敗と回执 | ユーザーへの表示は Workspace、配備復旧は Runbook。現行の内容比較を完全な幂等証明と扱わない |
| [TaskSchedule](task-scheduling.md) | 発火時刻、配置の並行更新、occurrence、同 Schedule の重複、遅延・計数・復旧 | Run は普通の作成サービスを使う。管理 UI は Workspace、field/ページングは契約へ渡す |
| [並行子分析](subagents.md) | 能力・指令・出力の派生、branch の終端、Session/Tool 監査の段階と版互換 | 共通消費は Run 予算、主 Result は Runtime；外部 write は子へ開放しない |
| [Workspace](workspace.md) | ページ責務、入力・待機・結果・三語・可アクセス性 | 要求 identity やサーバーの実行状態を独自に定義しない |
| [Task Flow / Run Flow](task-flow.md) | 計画と実活動、語義 identity と表示 layout、版・凍結・event 関連、歴史 fallback | Run を制御する第二の実行器にはしない |
| [生成モジュール](generated-modules.md) | 構築 identity/提交、供給元、iframe/Host、表示選択と停用 | SkillComposition の業務版・Run の実行状態を変えず、静的検査だけで実行を許可しない |

この表は責任の索引であり、進捗表ではない。「待実装」は各設計内で現在挙動と分け、着手順・未検証範囲は[計画](../planning/roadmap.md#132-下一步与当前决策)に集約する。

## 境界をまたぐ変更の読み方

例えば「応答が失われた後に再送する」変更は、Run 作成が原要求の同一性を決め、資源快照が初回の内容を守り、Workspace が原要求の確認と新規実行を区別する。Schedule や Worker の復旧を同じ retry として実装しない。

一つの変更では、責任を持つ設計に判断と理由を書き、隣接文書には影響とリンクだけを置く。[変更ガイド](../development/change-guide.md)で実装の入口を選び、公開 field の変更は[契約の同期・版互換](../development/contract-workflow.md)を確認する。各設計の受入条件を観測可能な検証へ落とし、履歴の長文や README に別の設計本文を作らない。
