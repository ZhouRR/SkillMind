#!/usr/bin/env python3
"""F1-4"""

import csv
from collections import Counter
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--fid", type=str, default="")
parser.add_argument("--kl", type=float, default=0)
args = parser.parse_args()



target_fid = args.fid

with open('/workspace/data/レビュー・単テ指摘/単テ障害一覧.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    
    bug_types = Counter()
    cause_cats = Counter()
    root_cause_cats = Counter()
    embed_phases = Counter()
    detect_phases = Counter()
    horizontal = Counter()
    
    for row in reader:
        fid_prefix = row['機能ID'].strip()[:12]
        if fid_prefix != target_fid:
            continue
        bug_types[row['バグ区分'].strip() or '(未記入)'] += 1
        cause_cats[row['原因区分'].strip() or '(未記入)'] += 1
        root_cause_cats[row['根本原因区分'].strip() or '(未記入)'] += 1
        embed_phases[row['埋め込み工程'].strip() or '(未記入)'] += 1
        detect_phases[row['摘出すべき工程'].strip() or '(未記入)'] += 1
        horizontal[row['横展開有無'].strip() or '(未記入)'] += 1

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
if args.kl > 0:
    print(f'新規バグ密度: {new_bugs / args.kl:.3f} 件/KL')

