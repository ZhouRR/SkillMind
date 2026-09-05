# Skill source package

本 directory は Skill source package を保管する。実行可能な資産（SKILL.md、scripts、agent 設定）であり製品仕様の文書ではないため、repository 外の `../../docs/` とは分離して管理する。

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
