#!/usr/bin/env python3
"""Parse a specific issue from the Redmine issues.csv and output as structured JSON.

Column positions are resolved dynamically from the CSV header, so the script
tolerates column reordering and addition/removal of non-essential columns.
Supports both Japanese and Chinese CSV headers via canonical key normalization.
"""

import csv
import json
import sys
import os


# Canonical key → [Japanese header, Chinese header]
CANONICAL_INFO_KEYS = {
    "ticket_no":        ["#", "#"],
    "project":          ["プロジェクト", "项目"],
    "tracker":          ["トラッカー", "跟踪"],
    "status":           ["ステータス", "状态"],
    "priority":         ["優先度", "优先级"],
    "subject":          ["題名", "主题"],
    "author":           ["作成者", "作者"],
    "assignee":         ["担当者", "指派给"],
    "category":         ["カテゴリ", "カテゴリ"],
    "detail":           ["詳細内容", "詳細内容"],
    "detector_org":     ["検出者所属", "検出者所属"],
    "detector":         ["検出者", "検出者"],
    "detected_date":    ["検出日", "検出日"],
    "detected_phase":   ["検出工程", "検出工程"],
    "detected_env":     ["検出時環境", "検出時環境"],
    "detected_app":     ["検出時アプリ", "検出時アプリ"],
    "detected_func":    ["検出時機能名", "検出時機能名"],
    "cause_app":        ["原因アプリ", "原因アプリ"],
    "cause_func":       ["原因機能名", "原因機能名"],
#    "recent_comment":   ["直近コメント", "直近コメント"],
    "disposal_result":  ["処置結果区分", "処置結果区分"],
    "closed_date":      ["終了日", "終了日"],
    "assignee_org":     ["現担当者所属", "現担当者所属"],
    "severity":         ["重要度", "重要度"],
    "failure_type":     ["障害区分", "障害区分"],
}

QUALITY_COLUMN_NAMES = [
#    "原因サブシステム",
#    "原因区分",
    "バグ区分",
    "バグ原因",
    "原因区分_AI",
    "原因内容",
#    "移行原因区分",
    "原因工程",
    "摘出すべき工程",
    "作り込み担当者（設計書）",
    "作り込み担当者（ＰＧ）",
    "根本原因内容",
    "対応内容・回答内容",
    "横展開有無",
    "横展開結果",
    "修正ドキュメント1",
    "修正ドキュメント2",
    "修正ドキュメント3",
    "修正プログラム1",
    "修正プログラム2",
    "修正プログラム3",
    "見積工数(H)",
    "実績工数(H)",
    "再現性",
    "対応期限",
]


def build_header_map(header: list[str]) -> dict[str, int]:
    """Build a raw header-name → index map from the CSV header row."""
    return {name.strip(): i for i, name in enumerate(header)}


def resolve_canonical_keys(header_map: dict[str, int],
                           canonical_map: dict[str, list[str]]) -> dict[str, int]:
    """Resolve canonical keys to column indices.

    For each canonical key, tries Japanese header name first, then Chinese.
    Returns dict of canonical_key → column_index.
    """
    resolved = {}
    missing = []
    for canon_key, (ja_name, zh_name) in canonical_map.items():
        if ja_name in header_map:
            resolved[canon_key] = header_map[ja_name]
        elif zh_name in header_map:
            resolved[canon_key] = header_map[zh_name]
        else:
            missing.append(f"{canon_key}({ja_name}/{zh_name})")

    if missing:
        print(f"Warning: columns not found in CSV: {missing}", file=sys.stderr)

    return resolved


def resolve_quality_columns(header_map: dict[str, int]) -> dict[str, int]:
    """Resolve quality column names to indices. These are Japanese-only."""
    resolved = {}
    missing = []
    for name in QUALITY_COLUMN_NAMES:
        if name in header_map:
            resolved[name] = header_map[name]
        else:
            missing.append(name)

    if missing:
        print(f"Warning: quality columns not found in CSV: {missing}", file=sys.stderr)

    return resolved


def parse_issue(csv_path: str, issue_no: int,
                info_col_map: dict[str, int]) -> dict:
    """Parse a single issue from the CSV file.

    Returns a dict with canonical keys for info fields and raw header names
    for quality fields.
    """
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            if row and row[0].strip() == str(issue_no):
                # Build raw data dict (header_name → value)
                raw = {}
                for i, value in enumerate(row):
                    if i < len(header):
                        raw[header[i].strip()] = value.strip()

                # Normalize info fields to canonical keys
                result = {}
                for canon_key, idx in info_col_map.items():
                    ja_name = CANONICAL_INFO_KEYS[canon_key][0]
                    zh_name = CANONICAL_INFO_KEYS[canon_key][1]
                    # Find which name was actually used in the header
                    if ja_name in raw:
                        result[canon_key] = raw[ja_name]
                    elif zh_name in raw:
                        result[canon_key] = raw[zh_name]
                    else:
                        result[canon_key] = ""

                # Keep quality fields under their raw names
                for name in QUALITY_COLUMN_NAMES:
                    result[name] = raw.get(name, "")

                return result
    return {}


def _get(issue: dict, canon_key: str, default: str = "") -> str:
    """Get a canonical key from the issue dict."""
    return issue.get(canon_key, default)


def format_issue(issue: dict, quality_col_map: dict[str, int]) -> str:
    """Format issue data for human-readable output."""
    lines = []

    # Basic info
    lines.append("=" * 60)
    lines.append(f"チケット #{_get(issue, 'ticket_no', 'N/A')}: {_get(issue, 'subject', 'N/A')}")
    lines.append("=" * 60)
    lines.append(f"ステータス: {_get(issue, 'status')}  優先度: {_get(issue, 'priority')}")
    lines.append(f"トラッカー: {_get(issue, 'tracker')}  カテゴリ: {_get(issue, 'category')}")
    lines.append(f"作成者: {_get(issue, 'author')}  担当者: {_get(issue, 'assignee')}")
    lines.append(f"検出日: {_get(issue, 'detected_date')}  検出工程: {_get(issue, 'detected_phase')}")
    lines.append(f"検出時アプリ: {_get(issue, 'detected_app')}")
    lines.append(f"検出時機能名: {_get(issue, 'detected_func')}")
    lines.append(f"処置結果区分: {_get(issue, 'disposal_result')}")

    # Detailed content
    lines.append("\n" + "-" * 40)
    lines.append("【詳細内容 / 问题描述】")
    lines.append("-" * 40)
    lines.append(_get(issue, "detail", "(空)"))

    # Comments
    comments = _get(issue, "recent_comment")
    if comments:
        lines.append("\n" + "-" * 40)
        lines.append("【直近コメント / 最近评论】")
        lines.append("-" * 40)
        lines.append(comments)

    # Quality analysis fields - split into filled and empty
    lines.append("\n" + "-" * 40)
    lines.append("【品質分析フィールド - 記入済み】")
    lines.append("-" * 40)

    empty_fields = []
    for name in QUALITY_COLUMN_NAMES:
        if name not in quality_col_map:
            continue
        value = issue.get(name, "")
        if value:
            lines.append(f"  [{name}]: {value[:200]}{'...' if len(value) > 200 else ''}")
        else:
            empty_fields.append(name)

    if empty_fields:
        lines.append("\n" + "-" * 40)
        lines.append("【品質分析フィールド - 未記入】")
        lines.append("-" * 40)
        for name in empty_fields:
            lines.append(f"  [ ] {name}")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 3:
        print("Usage: parse_issue.py <csv_path> <issue_number>", file=sys.stderr)
        sys.exit(1)

    csv_path = sys.argv[1]
    issue_no = int(sys.argv[2])

    if not os.path.exists(csv_path):
        print(f"Error: file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    # Parse header to resolve column indices dynamically
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)

    header_map = build_header_map(header)
    info_col_map = resolve_canonical_keys(header_map, CANONICAL_INFO_KEYS)
    quality_col_map = resolve_quality_columns(header_map)

    issue = parse_issue(csv_path, issue_no, info_col_map)
    if not issue:
        print(f"Error: issue #{issue_no} not found in CSV", file=sys.stderr)
        sys.exit(1)

    # Output formatted text
    print(format_issue(issue, quality_col_map))

    # Output JSON metadata for programmatic use (to stderr so stdout stays clean)
    empty_fields = {
        name: ""
        for name in QUALITY_COLUMN_NAMES
        if name in quality_col_map and not issue.get(name, "")
    }
    filled_quality = {
        name: issue.get(name, "")
        for name in QUALITY_COLUMN_NAMES
        if name in quality_col_map and issue.get(name, "")
    }

    meta = {
        "issue_no": issue_no,
        "empty_fields": empty_fields,
        "filled_quality": filled_quality,
        "subject": _get(issue, "subject"),
        "detail": _get(issue, "detail"),
        "comments": _get(issue, "recent_comment"),
    }
    print("\n" + json.dumps(meta, ensure_ascii=False, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
