# ProjectMind 工程スクリプト

契約・構成・文書の検証と派生物の生成を担当する。下表の command はすべて `PJM/` で実行する。Backend/Web の起動入口ではなく、環境構築は[ローカル開発](../../docs/development/local-development.md)を参照する。

## 目的と副作用

| 目的 | 入口 | 実行前に確認すること |
| --- | --- | --- |
| Schema と example を検証 | `python3 scripts/validate_contracts.py` | 読取検査。Backend と共通の検証依存が必要 |
| Compose の構造規約を検証 | `python3 scripts/validate_compose.py` | 読取検査。Docker 実起動や実環境設定の検証ではない |
| SDK の型・契約互換を確認 | `PYTHONPATH=backend/src python3 scripts/probe_claude_agent_sdk.py` | offline probe。実モデル品質や本番接続は証明しない |
| 文書のリンク・生成物一致を確認 | `python3 scripts/build_docs.py --check` | 読取検査。文書専用依存が必要 |
| 文書ブラウザ版を更新 | `python3 scripts/build_docs.py` | `docs/index.html` を上書き生成する |
| 文書工具を回帰検証 | `python3 -m unittest discover -s scripts/tests -v` | 一時 fixture を作成・回収する。外部 API は使わない |
| 実 browser で文書を検査 | `python3 scripts/check_docs_browser.py` | 生成 HTML を file として読む。HTTP(S) は阻断して失敗にする。既定は保存なし、`--output` 指定時のみ固定名の screenshot を上書き保存 |
| 公開 API snapshot を更新 | `python3 scripts/export_openapi.py` | 現在の app から OpenAPI を上書きする。公開 API の変更時だけ実行 |
| Interpreter の反復品質を測定 | [measure_skill_interpretations.py](measure_skill_interpretations.py) | 実モデル呼出し・課金を伴う。安全な入力と設定が必要 |
| 配備用 image を書き出す | [export-images.ps1](export-images.ps1) | 既存 local image を export する。build/pull は行わず、application image 不足時は拒否する。[移送手順](../../docs/operations/quickstart.md#image-移送と更新)に従う |

## 文書の生成元

[build_docs.py](build_docs.py) が Markdown を解析し、[docs-viewer.html](docs-viewer.html) を使って自己完結 HTML を生成する。本文やリンクは Markdown 側を直し、見た目・ブラウザ操作は template を直す。生成 HTML の本文を直接編集しない。

[文書管理](../../docs/development/documentation.md)に対象範囲、依存導入、build/check、ブラウザ確認と更新順序を集約する。`skills/**/SKILL.md` と references は実行資産のため、このブラウザへ取り込まない。

[check_docs_browser.py](check_docs_browser.py) は実 Chromium で次の三点を確認する。依存と操作の詳細は[ブラウザ検証](../../docs/development/documentation.md#可重复执行的浏览器检查)を正本とし、検査を追加するたびにここへ実施記録を複製しない。

- 全文の desktop/窄幅 layout と、重要章の拡大文字・focus・native reload。対象章は SECTION_TARGETS に集約する。
- 目的別 guide、コード README から設計への実クリック、章検索/高亮、履歴分離、keyboard、前進/後退、print。
- HTTP(S) 非依存と screenshot の明示保存。業務 API、DB、model、掲載 command は実行しない。

検索は見出しと本文/path を対象にするが、取り込む file の範囲は増やさない。文書検証の成功は、アプリの typecheck・公開 API・モデル品質・配備復旧の成功とは別に報告する。

OpenAPI を調べるだけなら exporter を実行せず、[保存快照の只読確認](../../docs/development/contract-workflow.md#遇到未接齐的交付链)を使う。Schema/example の検査と snapshot の一致は別の結果であり、どちらかの成功で未接続の Web や失敗中の回帰を隠さない。

## アプリケーションを操作する場合

初期 ADMIN、preflight、smoke、Secret rotation は [Backend の CLI 案内](../backend/README.md#運用-cli-と停止境界を確認する)から辿る。smoke は専用 Project に Run/Evaluation を作り、rotation は暗号文を更新するため、読取検査と混同しない。

[Makefile](../Makefile) の `make deploy` は container/旧 image を置換し、`make bootstrap-admin` は全 volume データを消す。通常の ADMIN 作成に後者を使わず、必ず[起動案内](../../docs/operations/quickstart.md)と[Runbook](../../docs/operations/runbook.md)で対象・backup・成功条件を確認する。
