"""書式解釈の指示と既存の交付境界を守る静的回帰。実モデルの判定試験ではない。"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = {
    "rv-reviewer": "書式・改訂と対象範囲",
    "execution-plan-generator": "書式を含む仕様の実行範囲",
}


def skill_text(skill: str) -> str:
    """公開する原文を UTF-8 で読む。テスト専用の解釈器は追加しない。"""
    return (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def style_section(skill: str) -> str:
    """該当節だけを検証し、他節の偶然の語句で保護を満たさない。"""
    text = skill_text(skill)
    heading = f"## {SECTIONS[skill]}\n"
    assert text.count(heading) == 1
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize("skill", SECTIONS)
def test_colors_need_source_rules_not_a_gray_only_filter(skill: str) -> None:
    """任意色と凡例の根拠を扱い、書式だけの除外判定を導入しない。"""
    section = style_section(skill)
    assert "灰色・赤・黄・緑" in section
    assert "凡例" in section and "同版" in section
    assert "色だけで PASS / FAIL" in section or "背景色だけで実行対象を決めず" in section
    assert "確認や FAIL を増やさない" in section or "付記だけでは停止しない" in section


@pytest.mark.parametrize("skill", SECTIONS)
def test_partial_strike_keeps_unmodified_step_content(skill: str) -> None:
    """文字片の改訂を行全体の廃止と混同させない。"""
    section = style_section(skill)
    assert "`~~旧ボタン名~~ 新ボタン名`" in section
    assert "行全体" in section and "未変更" in section
    assert "確認できた場合" in section or "確認済みなら" in section
    assert "旧記述を現行の操作" in section


@pytest.mark.parametrize("skill", SECTIONS)
def test_uninspected_or_unevaluated_style_is_not_absence(skill: str) -> None:
    """未評価・未抽出を通常書式と誤認せず、影響のある不明点だけ扱う。"""
    section = style_section(skill)
    for status in ("NOT_EVALUATED", "NOT_INSPECTED", "STYLE_EXTRACTION_UNAVAILABLE"):
        assert status in section
    assert "条件付き書式" in section
    assert "静的" in section
    assert "影響" in section or "左右する" in section


@pytest.mark.parametrize("skill", SECTIONS)
def test_handoff_does_not_invent_a_required_scope_report(skill: str) -> None:
    """最小 RV 判定から範囲を補造せず、新しい報告前提を作らない。"""
    section = style_section(skill)
    assert "PASS 値だけ" in section
    assert "範囲ファイル" in section or "範囲専用" in section
    assert "作らず" in section or "増やさない" in section
    for protocol in ("change.propose", "${effect_id}", "artifact_refs", "rv_scope.json"):
        assert protocol not in section


def test_review_preserves_color_legends_and_minimal_verdict() -> None:
    """空白の色見本も残し、PASS 用の報告や verdict 契約を追加しない。"""
    text = skill_text("rv-reviewer")
    section = style_section("rv-reviewer")
    assert "色付きの空白セルを無意味な空列として除去しない" in text
    assert "色見本、表頭、改訂履歴" in section
    assert "独立した範囲 JSON を作らず" in section
    assert "`rv_result` の最小判定記録は変更しない" in section
    assert "PASS ではレビュー報告を生成・保存せず" in text
    assert "必要な内容を取得できなければ変換の処理異常" in text


def test_execution_keeps_source_locations_and_frozen_scope() -> None:
    """付記を手順に数えず、除外と対象内の未実行・失敗を区別する。"""
    section = style_section("execution-plan-generator")
    assert "書式付記や色見本自体を業務ステップに数えない" in section
    assert "Excel 行番号/セル座標と Markdown 行番号は別物" in section
    assert "`sourceLines` / `expectedLines`" in section
    assert "開始前から対象外の記述" in section and "`NOT_RUN` を区別する" in section
    assert "登録後の範囲を色や実測に合わせて変更せず" in section
    assert "失敗・結果不明・未実行を母集団から除かない" in section
    assert "有効ステップが 0 件なら対象なし" in section
    assert "空の根拠や架空の操作・完了件数を作らない" in section


def test_execution_summarizes_receipts_without_artifact_prerequisites() -> None:
    """成果物の取得/保持を前提にせず、回执・原文・終端化の境界を維持する。"""
    text = skill_text("execution-plan-generator")
    section = style_section("execution-plan-generator")
    assert "元 Excel を取得・再変換せず" in section
    assert "新しい入力フォームを要求したりしない" in section
    assert "追加承認・毎ステップの文書保存は増やさない" in section
    assert "ステップごとの追加報告書" in text
    assert "中間の `実行結果.json` は生成しない" in text
    assert "実際に取得した MCP 回执と画面観測に基づき" in text
    assert "成果物へ接続できないこと、保持期限・取得経路が不明なことを理由に停止・質問しない" in text
    assert "操作回执の照合や現在画面の確認を省略する指示ではない" in text
    assert "成果物未取得だけで確認済み回执を不明へ変更しない" in text
    assert "各業務ステップの操作概要と確認できた結果" in text
    assert "操作完了だけから仕様全体の PASS や未観測の送達を主張しない" in text
    assert "タスク終了時に `RUNNING` を残さない" in text
    assert "## 原記録の保持と引渡し" not in text
    assert "原ファイルの必須退避" not in text
    assert "登録後は根拠・期待条件・固定版を変更せず" in text
    assert "結果未知のまま完了扱いにせず" in text
