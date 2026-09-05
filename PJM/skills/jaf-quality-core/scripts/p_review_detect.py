#!/usr/bin/env python3
"""P2"""

import csv
from collections import Counter
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--name", type=str, default="")
parser.add_argument("--app", type=str, default="")
args = parser.parse_args()



target = args.name
app_filter = args.app

app_fids = set()
if app_filter:
    with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row['アプリ'].strip() == app_filter:
                app_fids.add(row['機能ID'].strip())

for fname, label in [('詳設レビュー指摘一覧.csv', '詳設'), ('製造レビュー指摘一覧.csv', '製造')]:
    path = f'/workspace/data/レビュー・単テ指摘/{fname}'
    bug_types = Counter()
    cause_cats = Counter()

    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['検出者名'].strip() != target:
                continue
            if app_filter and row['機能ID'].strip() not in app_fids:
                continue
            bug_types[row['バグ 区分'].strip() or '(未記入)'] += 1
            cause_cats[row['原因区分'].strip() or '(未記入)'] += 1

    total = sum(bug_types.values())
    new_bugs = bug_types.get('新規バグ', 0)
    print(f'[{label}レビュー] 検出件数: {total} (新規バグ{new_bugs})')
    print(f'  バグ区分: {dict(bug_types)}')
    print(f'  原因区分(上位5): {cause_cats.most_common(5)}')

