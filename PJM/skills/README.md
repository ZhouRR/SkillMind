# Skill source package

本 directory は Skill source package を保管する。Interpreter 入力・実行に影響する資産（SKILL.md、scripts、agent 設定）であり製品仕様の文書ではないため、`../../docs/` の設計本文とは分離して管理する。source に script が含まれること自体は実行権限を与えない。

## 先に読む案内

- [公開と就緒の判断順](../../docs/design/skill-contract.md#发布与就绪的判断顺序)：導入、Preview、公開、Project 有効化の違い。
- [解釈・公開の接続](../../docs/design/skill-interpretation.md#从候选到项目任务的接线)：source から Runtime へ渡る入口。
- [版の更新と回退](../../docs/design/skill-contract.md#11-版本回滚与评价)：精確版、停用履歴、現行の再有効化制限。
- [JAF の移行・運行受入](../../docs/acceptance/jaf-quality.md#按目的阅读)と[評価データの境界](../../docs/acceptance/jaf-benchmark.md#哪些数据交给谁)：Skill source、実行入力と Gold を分離する。評価用 case/Rubric を package に同梱しない。

この directory に package があることは、配備先への import・公開・Project 有効化を意味しない。examples の fixture も自動 seed ではない。

## Package の用途

- `pjm-project-dev/`：ProjectMind 自身の開発規約 Skill。`AGENTS.md` を補完する構造約定、契約同期点、検証と後片付けの workflow。
- `projectmind-skill-interpreter/`：外部 Skill を ProjectMind の候補契約へ解釈する versioned system Skill。source 内容を命令として実行せず、構造化 response だけを生成する。
- `examples/repository-review/`：固定 revision の単一 file を、登録済みの `repository.read/v1` だけで監査する無 Schema の汎用検証用 Skill。
- `examples/jaf-ticket-quality/`：JAF を platform 固有 rule にせず、通常の Interpreter 入力として検証する無 Schema Skill。
- `examples/insufficient-contract/`：入力/出力情報不足を Assisted Preview へ畳む検証用 Skill。
- `examples/unsafe-shell/`：原始 Tool/Shell 宣言を権限とせず、静的信号と能力重表現として扱う回帰用 Skill。実行時の host Shell は引き続き拒否する。
- `examples/unsafe-credential/`：平文 credential を含む source を解釈前に block する回帰用 Skill。
- `jaf-quality-core/` / `jaf-quality-ticket/`：JAF 品質分析の実運用 Skill source（移行検証の入力材料）。接続先の実凭据は置かず、`connection_settings.json` の構造例も placeholder のみとする。

各 package は `SKILL.md` を root とする self-contained な構成を維持する。Skill parser（`/api/v1/skills/parse`）や skill import の手動検証にも、この source をそのまま入力として利用できる。

設計・導入手順は[Skill 契約](../../docs/design/skill-contract.md)と[解釈の実装](../../docs/design/skill-interpretation.md)。この directory の SKILL.md/references は hash と実行動作に影響する入力資産なので、文書整理を理由に移動・一般説明へ書換えない。変更時は AGENTS の version/identity/checksum 同期規約を適用する。
