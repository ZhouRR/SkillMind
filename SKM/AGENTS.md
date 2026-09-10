# Skillmind エージェント開発規約

本規約は Skillmind 全体に適用する。コード・起動は [README](README.md)、設計は [docs](../docs/README.md)を正本とし、ここに複製しない。

## 作業前に読むもの

1. [現在の計画](../docs/planning/roadmap.md#当前执行状态)で実装範囲と不足を確認する。規約や test の存在を実装済みの証拠にしない。
2. [変更ガイド](../docs/development/change-guide.md)から対象設計を読む。仕様と実装の矛盾は影響を明示して整合させる。
3. [実装細則](../docs/development/coding-rules.md)の該当章を遵守する。文書のみなら[文書維持](../docs/development/documentation.md)を読み、実装細則の一律再読は不要。

## 変更してはいけない境界

- API/Worker は共有 package のモジュラーモノリス。Business は AgentEngine 抽象に依存し、SDK 型や route/job に業務規則を漏らさない。
- 認証は共有 actor dependency、unsafe request は Origin/CSRF。越権と不存在は同じ 404。hash・認可・event/outbox 等は[単一実装](../docs/development/coding-rules.md#backend)を使う。
- PostgreSQL は正本、Redis は Queue・短期 Lock・通知。Run snapshot/権限上限/Result は不変。人工修正は Evaluation、答復/批准は新 Segment、同 Segment 復旧は新 Attempt。
- Skill は Organization 資産 + Project 明示有効化。Blueprint は Interpreter のみが生成する。Manifest から逆算せず、業務専用 Schema/seed/renderer を共通規則へ戻さない。
- guidance・script・model 提案は権限ではない。未知の業務能力と登録済み versioned Tool を区別し、実行 Script は公開 checksum に一致させる。SDK builtin は DENIED_BUILTIN_TOOLS で fail closed、cwd は隔離ではない。凍結 input/ は読取専用、write は workspace/・output/ のみ。
- 外部 write は observe → propose → apply。[受控写入](../docs/design/repository-effects.md)の承認、Provider、scope、競合・idempotency・read-back を省略しない。repository.write/v1 は事前許可不可・force 禁止。
- 生成 UI・並行子 Agent・Shell/network の拡張は計画と脅威モデルを先に更新し、各設計の門禁を満たす。局部実装を全面許可にしない。生成 bundle は CSP sandbox allow-scripts、iframe に allow-same-origin を付けない。子能力は resolve_subagent_capabilities と Run 共通予算を通す。

## 言語とコード

会話は中国語、AGENTS/README・docstring/JSDoc/コメントは日本語。識別子と公開契約は既存表記を維持する。
関数・型・Component・fixture・migration には責務、複雑な分岐・安全/transaction/復旧には理由を記し、古いコメントも同期する。単純処理の逐語説明は不要。
Python の future annotations、TypeScript strict を維持し、any で契約を回避しない。広い例外捕捉は理由のある境界層に限定する。Effect は cleanup、遅延応答は unmount/対象切替後に反映しない。三語は共有 catalog を使う。

## 検証と変更規律

- [同期点](../docs/development/coding-rules.md#同步点)と[変更別検証](../docs/development/local-development.md#変更に応じた検証)を適用する。動作変更には対応回帰を追加し、未実行・skip・失敗と残る risk を報告する。
- 文書は規則を更新し、進捗ログを増やさない。生成 HTML は直接編集せず、SKILL.md/references は整理対象外。
- ユーザーの既存変更・成果物を保全する。lockfile を手編集せず、Credential・内部 URL・Ticket 本文・Gold・Secret を fixture/log に置かない。
- Git 書込・履歴変更・破壊的操作は明示依頼が必要。実 DB・モデル・外部 write の検証は対象と副作用を事前確認する。
- 完了時は設計/契約との整合と今回の検証を報告し、mock と実環境の証拠を区別する。計画は機能状態が変わった時だけ更新する。
