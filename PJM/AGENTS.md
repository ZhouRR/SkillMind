# ProjectMind エージェント開発規約

本規約は ProjectMind 全体に適用する。起動・コード配置は [README](README.md)、詳細設計は [docs](../docs/README.md) に置き、ここへ重複させない。

## 作業前に読むもの

1. [計画の現在状態](../docs/planning/roadmap.md#当前执行状态)で現在の実装・差距・作業範囲を確認する。規約の存在を実装済みの証拠にしない。
2. [変更ガイド](../docs/development/change-guide.md)から対象設計を選ぶ。仕様とコードが矛盾する場合は影響を明示して整合させ、推測で片方を書き換えない。
3. 変更に該当する以下の細則を読み、遵守する。文書だけの変更では実装細則を一律に読み直す必要はない。

| 変更対象 | 必読の細則 |
| --- | --- |
| Backend・共通処理 | [Backend と単一実装](../docs/development/coding-rules.md#backend) |
| Run・Worker・Session・調度 | [Run lifecycle](../docs/development/coding-rules.md#run-lifecycle)と対象の設計 |
| Web | [Web の責任分担](../docs/development/coding-rules.md#web) |
| 設定・契約・DB・Tool・system Skill | [変更の同期点](../docs/development/coding-rules.md#同步点) |
| Compose・Ingress | [配備境界](../docs/development/coding-rules.md#ingress) |
| 文書・閲覧版 | [文書維持と検証](../docs/development/documentation.md) |

`docs/01`・`docs/06` 等のコード注釈は旧文書番号であり、[対応表](../docs/README.md#旧番号の対応)から辿る。注釈を一括 path 置換しない。

## 変更してはいけない境界

- Backend は API/Worker が同じ package を共有するモジュラーモノリス。Business 層は `AgentEngine` 抽象へ依存し、SDK 型へ直接依存しない。route/ARQ job に業務規則を埋め込まない。
- 業務 API の認証・授権は `api/auth_dependencies.py` の actor dependency に集約する。越権と不存在は同じ 404、unsafe request は Origin/CSRF を検証する。
- Run snapshot・権限上限・Result は不変。人工修正は Evaluation、回答/承認による継続は新 Segment、同 Segment の復旧は新 Attempt とする。
- PostgreSQL は Run/監査の正本。Redis は Queue・短期 Lock・通知に限定する。凍結 snapshot や歴史記録を「整合」のために書き換えない。
- Skill は Organization 資産 + Project 明示有効化。Blueprint は Interpreter のみが生成し、Manifest からの逆算や blueprint 無しの発行を復活させない。
- guidance・source script・model の提案は権限ではない。未知の業務 capability と実行可能な登録済み versioned Tool capability を区別し、公開 checksum に一致しない Script を実行しない。
- SDK 組み込み Tool は `DENIED_BUILTIN_TOOLS` で fail closed。`cwd` は隔離境界ではない。Run workspace の write は `workspace/`・`output/` のみ、凍結 `input/` は読取専用とする。
- 外部 write は observe → propose → apply。ChangeProposal、承認/明示的低 risk 事前許可、登録 Provider、固定 scope、競合制御、idempotency、read-back を必須とする。`repository.write/v1` は事前許可不可・force 不使用。
- 生成 FrontendModule、並行子 Agent、Shell/network の拡張は、計画を先に更新し、脅威モデル・対象設計の門禁を満たすまで追加しない。実装済みの局部を全面開放の許可にしない。
- 生成 bundle は `Content-Security-Policy: sandbox allow-scripts` を必須とし、iframe に `allow-same-origin` を付けない。子 Agent の能力は `resolve_subagent_capabilities` で拒否規則を強制し、Run 共通予算を迂回しない。
- 業務固有 Schema・active seed・専用 renderer を generic service/API/通常 Workspace の rule source に戻さない。参照済み SkillVersion/Run は read-only audit asset として保持する。

## 共通実装の利用規約

同じ意味の処理を複製しない。[細則の単一実装表](../docs/development/coding-rules.md#backend)で hash、redaction、lease、event/outbox、認可、binding、repository、effect、binary 変換の入口を確認する。

## 言語とコード

- 会話は中国語。AGENTS/README と code の docstring/JSDoc/コメントは日本語、識別子・公開 field・Schema key は既存表記を維持する。
- Class・関数・method・Component・型・fixture・migration entry point に責務の説明を付ける。複雑な分岐・安全境界・transaction・復旧には「なぜ必要か」を書き、変更時に古いコメントも直す。単純な代入や return に逐語的コメントを付けない。
- Python は `from __future__ import annotations` を維持する。広い `Exception` 捕捉は理由のある境界層に限定する。
- TypeScript strict を維持し、`any` で契約を回避しない。Effect は cleanup、非同期結果は unmount/対象切替後に反映しない。三語文案は共有 catalog に置く。

## よくある変更の同期点

契約・Schema/example・OpenAPI・Backend/Web consumer と回帰を同時に確認する。[同期表](../docs/development/coding-rules.md#同步点)を適用し、公開契約の変更は [契約 workflow](../docs/development/contract-workflow.md)に従う。

## 検証と変更規律

- 動作変更と同時に `tests/` の対応回帰を追加/更新し、test 名は検証する振る舞いを表す。[変更別検証](../docs/development/local-development.md#変更に応じた検証)を実行し、mock・実 DB・配備・モデルの証拠を区別する。未実行・skip・失敗と残る risk を報告する。
- 文書のみなら build/check・文書工具回帰・関連例・変更した閲覧経路を検証する。生成 HTML を直接編集せず、Skill の SKILL.md/references を文書整理に巻き込まない。
- 既存の変更を保全し、生成 lockfile を手編集しない。Credential・内部 URL・Ticket 本文・Gold・Secret を fixture/ログへ入れない。
- Git の初期化・commit・push・履歴変更、破壊的操作はユーザーの明示依頼なしに行わない。実 DB・モデル・外部 write の検証は対象と副作用を確認してから行う。
- 完了時は設計との整合、必要な同期、実際の検証結果を報告する。現在の状態が変わる場合だけ計画を更新し、文書整理の履歴ページは新設しない。
