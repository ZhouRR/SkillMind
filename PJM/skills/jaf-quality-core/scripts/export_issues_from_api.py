#!/usr/bin/env python3
"""Redmine REST API からトラッカー=障害の全チケットを取得し issues.csv としてエクスポート。

処理フロー:
1. limit=1 で total_count を取得 → ループ回数決定
2. 100件/ページで全件取得
3. 各チケット: 標準フィールド(日本語列名) + 全カスタムフィールドをフラット化
4. enumeration_cf は jaf_redmine_field_mappings.json で ID→ラベル解決
5. UTF-8 BOM 付き CSV を /workspace/data/issues.csv に出力
"""

import csv
import json
import math
import os
import sys
import time
from urllib import request, error

CONNECTION_SETTINGS_PATH = "/workspace/config/connection_settings.json"


def _load_config() -> tuple[str, str]:
    """connection_settings.json から Redmine URL と API Key を取得。"""
    if not os.path.exists(CONNECTION_SETTINGS_PATH):
        print(f"Warning: connection_settings.json not found: {CONNECTION_SETTINGS_PATH}", file=sys.stderr)
        return ("", "")

    with open(CONNECTION_SETTINGS_PATH, "r", encoding="utf-8") as f:
        settings = json.load(f)

    url = settings.get("redmine_url", "")
    key = settings.get("redmine_api_key", "")

    if not url or not key:
        print("Warning: could not parse Redmine config from connection_settings.json", file=sys.stderr)
    return (url, key)


REDMINE_URL, API_KEY = _load_config()
PROJECT_ID = 1
TRACKER_ID = 2  # 障害
LIMIT = 100
OUTPUT_PATH = "/workspace/data/issues.csv"
MAPPINGS_PATH = "/workspace/config/jaf_redmine_field_mappings.json"

# API 標準フィールドの JSON キー → CSV 日本語列名 マッピング
# ネストオブジェクト({id,name})は .name を展開
STANDARD_FIELD_MAP = {
    "id": "#",
    "project.name": "プロジェクト",
    "tracker.name": "トラッカー",
    "status.name": "ステータス",
    "priority.name": "優先度",
    "subject": "題名",
    "author.name": "作成者",
    "assigned_to.name": "担当者",
    "updated_on": "更新日",
    "category.name": "カテゴリ",
    "fixed_version.name": "対象バージョン",
    "start_date": "開始日",
    "due_date": "期日",
    "estimated_hours": "予定工数",
    "total_estimated_hours": "合計予定工数",
    "spent_hours": "作業時間",
    "total_spent_hours": "合計作業時間",
    "done_ratio": "進捗率",
    "created_on": "作成日",
    "closed_on": "終了日",
    "description": "詳細内容",
    "is_private": "プライベート",
}

# 標準フィールドの CSV 列順（固定）
STANDARD_FIELD_ORDER = [
    "#", "プロジェクト", "トラッカー", "親チケット", "親チケットの題名",
    "ステータス", "優先度", "題名", "作成者", "担当者", "更新日",
    "カテゴリ", "対象バージョン", "開始日", "期日", "予定工数",
    "合計予定工数", "作業時間", "合計作業時間", "進捗率", "作成日",
    "終了日",
]


def load_enum_mappings() -> dict[str, dict[str, str]]:
    """jaf_redmine_field_mappings.json から enumeration_cf の ID→ラベル マッピングを読み込む。"""
    if not os.path.exists(MAPPINGS_PATH):
        print(f"Warning: mappings file not found: {MAPPINGS_PATH}", file=sys.stderr)
        return {}

    with open(MAPPINGS_PATH, "r", encoding="utf-8") as f:
        mappings = json.load(f)

    enum_map: dict[str, dict[str, str]] = {}
    for cf_id, cf_def in mappings.get("custom_fields", {}).items():
        if cf_def.get("type") == "enumeration_cf":
            enum_map[cf_id] = cf_def.get("values", {})
    return enum_map


def fetch_page(offset: int, limit: int = LIMIT) -> dict:
    """Redmine API から1ページ分のチケットを取得。"""
    url = (
        f"{REDMINE_URL}/issues.json"
        f"?project_id={PROJECT_ID}"
        f"&tracker_id={TRACKER_ID}"
        f"&status_id=*"
        f"&limit={limit}"
        f"&offset={offset}"
    )
    req = request.Request(url, headers={"X-Redmine-API-Key": API_KEY})
    try:
        with request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        print(f"HTTP error at offset={offset}: {e}", file=sys.stderr)
        return {}
    except error.URLError as e:
        print(f"URL error at offset={offset}: {e}", file=sys.stderr)
        return {}


def flatten_standard_fields(issue: dict) -> dict[str, str]:
    """API の issue 辞書から標準フィールドをフラット化（日本語列名）。"""
    row: dict[str, str] = {}

    # ネストオブジェクトを .name で展開
    for api_key, csv_col in STANDARD_FIELD_MAP.items():
        if "." in api_key:
            parent_key, child_key = api_key.split(".", 1)
            parent = issue.get(parent_key)
            if isinstance(parent, dict):
                row[csv_col] = str(parent.get(child_key, ""))
            else:
                row[csv_col] = ""
        else:
            val = issue.get(api_key)
            if val is None:
                row[csv_col] = ""
            elif isinstance(val, bool):
                row[csv_col] = str(val).lower()
            else:
                row[csv_col] = str(val)

    # API が返さないが既存CSVにある列（空欄でプレースホルダ）
    extra_cols = [
        "親チケット",
        "親チケットの題名",
        "最終更新者",
        "関連するチケット",
        "ファイル",
        "直近コメント",
    ]
    for c in extra_cols:
        row[c] = ""

    return row


def resolve_custom_field_value(cf: dict, enum_mappings: dict[str, dict[str, str]]) -> str:
    """カスタムフィールドの値を文字列に変換。enumeration_cf は ID→ラベル解決。"""
    cf_id = str(cf.get("id", ""))
    value = cf.get("value", "")

    if value is None or value == "":
        return ""

    # list_cf の場合、value が配列で返ることがある
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)

    # enumeration_cf の場合は ID→ラベル変換
    if cf_id in enum_mappings:
        return enum_mappings[cf_id].get(str(value), str(value))

    return str(value)


def collect_all_custom_field_names(all_issues: list[dict]) -> list[tuple[str, str]]:
    """全チケットからカスタムフィールドの name を収集し、(cf_id, name) のリストを返す。
    ID昇順でソート。
    """
    seen: dict[str, str] = {}  # cf_id → name
    for issue in all_issues:
        for cf in issue.get("custom_fields", []):
            cf_id = str(cf.get("id", ""))
            if cf_id and cf_id not in seen:
                seen[cf_id] = cf.get("name", "")
    # ID 昇順
    return sorted(seen.items(), key=lambda x: int(x[0]))


def issue_to_row(
    issue: dict,
    custom_field_columns: list[tuple[str, str]],
    enum_mappings: dict[str, dict[str, str]],
) -> dict[str, str]:
    """1チケットをCSV行（列名→値の辞書）に変換。"""
    row = flatten_standard_fields(issue)

    # カスタムフィールドのインデックス (cf_id → name)
    cf_map: dict[str, str] = {}
    for cf in issue.get("custom_fields", []):
        cf_id = str(cf.get("id", ""))
        cf_name = cf.get("name", "")
        cf_map[cf_id] = cf_name
        if cf_name:
            row[cf_name] = resolve_custom_field_value(cf, enum_mappings)

    # 存在しないカスタムフィールド列は空欄
    for cf_id, cf_name in custom_field_columns:
        if cf_name not in row:
            row[cf_name] = ""

    return row


def build_header(custom_field_columns: list[tuple[str, str]]) -> list[str]:
    """CSVヘッダを構築。標準フィールド + カスタムフィールド。"""
    header = list(STANDARD_FIELD_ORDER)
    # APIが返す標準フィールドのうち STANDARD_FIELD_ORDER に入っていないものがあれば末尾に追加
    api_standard_cols = set(STANDARD_FIELD_MAP.values())
    for col in api_standard_cols:
        if col not in header:
            header.append(col)
    # カスタムフィールド
    for _, cf_name in custom_field_columns:
        if cf_name and cf_name not in header:
            header.append(cf_name)
    return header


def main():
    enum_mappings = load_enum_mappings()
    print(f"Loaded {sum(len(v) for v in enum_mappings.values())} enum mappings for {len(enum_mappings)} fields", file=sys.stderr)

    # 1. 全件数取得
    print("Fetching total_count...", file=sys.stderr)
    first_page = fetch_page(0, limit=1)
    total_count = first_page.get("total_count", 0)
    if total_count == 0:
        print("Error: total_count is 0, check API connection", file=sys.stderr)
        sys.exit(1)
    pages = math.ceil(total_count / LIMIT)
    print(f"Total: {total_count} tickets, {pages} pages (limit={LIMIT})", file=sys.stderr)

    # 2. 全件取得
    all_issues: list[dict] = []
    for page in range(pages):
        offset = page * LIMIT
        print(f"Fetching page {page + 1}/{pages} (offset={offset})...", file=sys.stderr)
        data = fetch_page(offset)
        issues = data.get("issues", [])
        all_issues.extend(issues)
        if len(issues) < LIMIT and page < pages - 1:
            # 最終ページより前で中途半端になった場合（サーバー上限に達した）
            break
        if page < pages - 1:
            time.sleep(0.3)  # サーバー負荷軽減

    print(f"Fetched {len(all_issues)} issues", file=sys.stderr)

    # 3. カスタムフィールド列の収集
    custom_field_columns = collect_all_custom_field_names(all_issues)
    print(f"Custom field columns: {len(custom_field_columns)}", file=sys.stderr)

    # 4. CSV 構築
    header = build_header(custom_field_columns)

    rows = []
    for i, issue in enumerate(all_issues):
        row = issue_to_row(issue, custom_field_columns, enum_mappings)
        rows.append(row)
        if (i + 1) % 100 == 0:
            print(f"  Processed {i + 1}/{len(all_issues)}", file=sys.stderr)

    # 5. 出力
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} rows, {len(header)} columns to {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
