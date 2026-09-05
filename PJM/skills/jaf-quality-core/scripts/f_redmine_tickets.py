#!/usr/bin/env python3
"""F2: issues.csv から指定機能IDのバグチケットを検索（原因FIDで完全一致）"""

import csv
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--fid", type=str, required=True)
args = parser.parse_args()

target_fid = args.fid

tickets = []
with open('/workspace/data/issues.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        if row.get('原因 画面ID/バッチID/帳票ID/サービスID', '').strip() != target_fid:
            continue
        tickets.append({
            'id': row['#'].strip(),
            'subject': row.get('題名', '').strip(),
            'status': row.get('ステータス', '').strip(),
            'priority': row.get('優先度', '').strip(),
            'detected_phase': row.get('検出工程', '').strip(),
            'cause_phase': row.get('原因工程', '').strip(),
            'bug_category': row.get('バグ区分', '').strip(),
            'bug_cause': row.get('バグ原因', '').strip(),
            'cause_ai': row.get('原因区分_AI', '').strip(),
            'escape_phase': row.get('摘出すべき工程', '').strip(),
            'root_cause': row.get('根本原因内容', '').strip(),
            'detected_app': row.get('検出時アプリ', '').strip(),
        })

print(f'総件数: {len(tickets)}')
for t in tickets:
    print(f"#{t['id']} [{t['status']}] {t['subject']}")
    print(f"  検出工程: {t['detected_phase']} | 原因工程: {t['cause_phase']} | バグ区分: {t['bug_category']}")
    print(f"  バグ原因: {t['bug_cause']} | 原因区分_AI: {t['cause_ai']} | 摘出工程: {t['escape_phase']}")
    print(f"  検出時アプリ: {t['detected_app']}")
    if t['root_cause']:
        rc = t['root_cause'].replace('\r\n', '\n')[:200]
        print(f"  根本原因: {rc}...")
    print()
