#!/usr/bin/env python3
"""A1-4"""

import csv
from collections import Counter
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=str, default="")
args = parser.parse_args()



# Step A1 で取得したアプリの機能ID一覧を読み込み
func_ids = set()
with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = next(reader)
    col = {name.strip(): i for i, name in enumerate(header)}
    for row in reader:
        if len(row) > col.get('アプリ','') and row[col['アプリ']].strip() == args.app:
            func_ids.add(row[col['機能ID']].strip())

# 単テ障害集計
bug_types = Counter()
cause_cats = Counter()
root_cause_cats = Counter()
embed_phases = Counter()
detect_phases = Counter()
horizontal = Counter()
func_bug_counts = Counter()

with open('/workspace/data/レビュー・単テ指摘/単テ障害一覧.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    for row in reader:
        fid_prefix = row['機能ID'].strip()[:12]
        if fid_prefix not in func_ids:
            continue
        bug_types[row['バグ区分'].strip() or '(未記入)'] += 1
        cause_cats[row['原因区分'].strip() or '(未記入)'] += 1
        root_cause_cats[row['根本原因区分'].strip() or '(未記入)'] += 1
        embed_phases[row['埋め込み工程'].strip() or '(未記入)'] += 1
        detect_phases[row['摘出すべき工程'].strip() or '(未記入)'] += 1
        horizontal[row['横展開有無'].strip() or '(未記入)'] += 1
        func_bug_counts[fid_prefix] += 1

total = sum(bug_types.values())
new_bugs = bug_types.get('新規バグ', 0) + bug_types.get('バグ（新RS新規機能）', 0)
print(f'総障害件数: {total}')
print(f'バグ区分: {dict(bug_types)}')
print(f'新規バグ: {new_bugs}件')
print(f'原因区分: {dict(cause_cats)}')
print(f'根本原因区分: {dict(root_cause_cats)}')
print(f'埋め込み工程: {dict(embed_phases)}')
print(f'摘出すべき工程: {dict(detect_phases)}')
print(f'横展開有無: {dict(horizontal)}')
# 障害数上位の機能ID
print(f'障害数上位5機能: {func_bug_counts.most_common(5)}')

