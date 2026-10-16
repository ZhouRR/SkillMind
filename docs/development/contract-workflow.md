# 公開契約の変更と結合

本ページは公開プロトコルおよび永続フォーマットの変更に適用する。内部リファクタリング・スタイル・文言のみの変更では Schema、版、migration を追加しない。保守境界は[実行境界](runtime-guide.md)、状態は[計画](../planning/roadmap.md)、コマンド環境は[ローカル開発](local-development.md)を参照。

## 交付経路の全体像

```text
領域規則/認可/互換 → Schema/example → Backend
  → Web/Worker → 契約・フロー回帰 → 版整合と配備
```

影響する層のみを更新する。`required` 追加・項目削除・enum 縮小・null 許容や HTTP 拒否意味の変更は、URL や `v1` ファイル名が同じでも互換性を壊し得る。

## 3 種類のデータを混同しない

| 層 | 境界 |
| --- | --- |
| 永続スナップショット | 当時の事実・フォーマット版・checksum を保持し、新画面のために旧 Run を書き換えない |
| 公開応答 | 認可後の allowlist 投影とし、内部 JSON の直接露出や元値の書き換えによるマスクを行わない |
| 画面状態 | validator 通過後にビューを生成し、履歴資源・主体・欠落項目を捏造しない |

## 消費者と同期順序の特定

[同期点](coding-rules.md#同期点)に従い Worker・CLI・Schedule 等の経路を含む実際の消費者を特定し、影響項目のみを同期する。JSON 項目だけでなく route の status、media type、header、キャッシュ、Problem も照合する。[OpenAPI](../../SKM/contracts/openapi/skillmind-api.v1.json) は app から生成し、手編集しない。
Schema は認可や並行制御を検証せず、OpenAPI 整合は宣言が実応答を網羅することまでは保証しない（例：ログイン 429 では `Retry-After` と画面処理も確認する）。

## 未接続の交付経路と検査

実際の route・配線・消費者から欠落箇所を特定する。DTO・example・単体テストの存在はユーザー動線の開通を意味しない。整合検査や一括テストの失敗は原因箇所を修復し、検証を無効化してエラーを隠さない。

読取検査は lifespan を起動せず、DB 接続や書き戻しも行わない。Backend 依存の準備後、`SKM/` で `python3 scripts/validate_contracts.py` を、`SKM/backend/` で以下を実行する。

```bash
python3 -m pytest -p no:cacheprovider -o addopts= -q \
  tests/contracts/test_contracts.py::test_exported_openapi_is_current
```

公開 API を変更した場合は `SKM/` で `python3 scripts/export_openapi.py` を実行し、消費者とテストを同期する（読取専用レビューや文書整理では再出力しない）。

## 履歴互換と版互換

- 新 API で旧 Run を読む：明確な履歴状態を返し、現在の資源からスナップショットを補完しない。
- 新 Web で旧 API を読む：規定された互換形状のみを受け入れ、必須項目を空配列へ既定化しない。
- 旧 Web で新 API を読む：追加項目や enum を確認する。一括配備でも旧ブラウザ・Worker・処理中リクエストが残る。
- API/Worker のロールバック：schema・キュー・非終端状態の読取互換を確認し、監査削除や検証緩和を行わない。

任意の表示データの破損は既存規則で縮退表示できるが、主体・認可・必須プロトコルエラーを成功へ縮退させてはならない。

## 検証結果の記録

変更範囲に応じて文書・契約・Backend/Web・実トランザクション・配備/モデルの結果を報告し、mock・失敗・skip・未実行を明記する。
実 DB や外部 write は承認された隔離対象のみで行う。継続的な能力不足は計画へ反映し、一時的な診断は交付報告に留めて履歴文書の追加や過去の通過件数の流用を行わない。

