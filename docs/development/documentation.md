# 文書維持

正式文書には現在の利用・開発・運用を導く内容のみを残す。入口は[文書ガイド](../README.md)、コードモジュール案内は [SKM README](../../SKM/README.md) に集約する。

## 残す内容

- 利用者の操作手順、保守者が守るべき境界、実行可能な検証・復元コマンド。
- 重要な設計判断とコード/契約の入口（詳細なフィールド・状態図・実装手順はソースコードを直接参照し、並行仕様を重複管理しない）。
- [Roadmap](../planning/roadmap.md) には現在の能力、再確認待ち事項、実利用に基づく改善方針のみを記録する。

各規則は単一のガイドでのみ保守し、他所からはリンクで参照する。廃止された詳細設計や進捗ログは正式ディレクトリに残さず、経緯は Git 履歴を参照する。

## 整理の境界

`SKILL.md` および Skill の references/Schema/template は実行入力であり、製品文書の整理対象に含めない（業務規則・凍結内容・モデル設定を書き換えない）。
実消費者が存在する安定アンカーは維持し、ページ統合時は入口と検査を更新する。

文書に資格情報・内部 URL・実業務本文・一時的な受入データを含めず、例示には合成値を使う。生成器は正式 Markdown と 3 つのルート入口のみを読み、`.env` や依存ディレクトリを走査しない。

## ビルドと検査

`SKM/` で実行する（生成物である `docs/index.html` は直接編集しない）：

```bash
python3 -m pip install --user -r scripts/docs-requirements.txt
python3 scripts/build_docs.py
python3 scripts/build_docs.py --check
python3 -m unittest discover -s scripts/tests -p 'test_build_docs.py' -v
```

ビルドは見出し階層・コードフェンス・リンク・アンカー・ViewSpec 例を検証し、`--check` は生成結果が最新かも確認する。レイアウトやナビゲーションを変更した場合は、[ブラウザ回帰依存](local-development.md#ブラウザ回帰)を導入して次を実行する：

```bash
python3 scripts/check_docs_browser.py --output /tmp/skillmind-docs-check
```

全ページ・全章の複数ウィンドウ幅と文字サイズでのレイアウト、検索、実リンク、キーボード操作、履歴、印刷をローカルで検査する（業務環境へは接続しない）。完了時は変更範囲・実施した検査・未検証の制限を報告する。

