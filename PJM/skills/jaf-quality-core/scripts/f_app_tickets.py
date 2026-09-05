#!/usr/bin/env python3
"""F6/A2: issues.csv から指定アプリの新規バグチケットを集計（原因アプリでフィルタ）"""

import csv
from collections import Counter
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=str, required=True)
parser.add_argument("--detail", action="store_true", help="チケット個別情報も出力")
args = parser.parse_args()

target_app = args.app

NEW_BUG_TYPES = {'バグ（新RS新規機能）', 'バグ（現行改良機能）', 'バグ（現行継承機能）'}

cause_phases = Counter()
detect_phases = Counter()
total = 0
tickets = []

with open('/workspace/data/issues.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        if row.get('原因アプリ', '').strip() != target_app:
            continue
        bug_cat = row.get('バグ区分', '').strip()
        if bug_cat not in NEW_BUG_TYPES:
            continue
        total += 1
        cp = row.get('原因工程', '').strip()
        dp = row.get('検出工程', '').strip()
        if cp:
            cause_phases[cp] += 1
        if dp:
            detect_phases[dp] += 1

        if args.detail:
            tickets.append({
                'id': row['#'].strip(),
                'subject': row.get('題名', '').strip(),
                'status': row.get('ステータス', '').strip(),
                'priority': row.get('優先度', '').strip(),
                'detected_phase': dp,
                'cause_phase': cp,
                'bug_category': bug_cat,
                'bug_cause': row.get('バグ原因', '').strip(),
                'cause_ai': row.get('原因区分_AI', '').strip(),
                'escape_phase': row.get('摘出すべき工程', '').strip(),
                'root_cause': row.get('根本原因内容', '').strip(),
                'detected_func': row.get('検出時機能名', '').strip(),
                'designer': row.get('作り込み担当者（設計書）', '').strip(),
                'maker': row.get('作り込み担当者（ＰＧ）', '').strip(),
                'detected_fid': row.get('検出時 画面ID/バッチID/帳票ID/サービスID', '').strip(),
            })

print(f'総新規バグ件数: {total}')
print(f'原因工程分布: {dict(cause_phases)}')
print(f'検出工程分布: {dict(detect_phases)}')

if args.detail:
    print(f'\n--- チケット一覧 ---')
    for t in tickets:
        print(f"#{t['id']} [{t['status']}] {t['subject'][:80]}")
        print(f"  検出工程: {t['detected_phase']} | 原因工程: {t['cause_phase']} | バグ区分: {t['bug_category']}")
        print(f"  バグ原因: {t['bug_cause']} | 原因区分_AI: {t['cause_ai']} | 摘出工程: {t['escape_phase']}")
        print(f"  検出時機能: {t['detected_func']} | 設計書担当: {t['designer']} | PG担当: {t['maker']}")
        rc = t['root_cause'].replace('\r\n', '\n')[:150] if t['root_cause'] else ''
        if rc:
            print(f"  根本原因: {rc}...")
        print()
