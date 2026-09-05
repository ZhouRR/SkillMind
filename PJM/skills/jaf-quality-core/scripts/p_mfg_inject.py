#!/usr/bin/env python3
"""P4"""

import csv
from collections import Counter
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--name", type=str, default="")
parser.add_argument("--app", type=str, default="")
args = parser.parse_args()



target = args.name
app_filter = args.app

# JAF規模一覧から機能ID→製造担当の辞書を作成
fid_to_maker = {}
with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        if app_filter and row['アプリ'].strip() != app_filter:
            continue
        fid_to_maker[row['機能ID'].strip()] = row['製造担当'].strip()

# 製造レビュー指摘: 原因工程が製造
mfg_bugs = Counter()
mfg_causes = Counter()
for fname in ['詳設レビュー指摘一覧.csv', '製造レビュー指摘一覧.csv']:
    path = f'/workspace/data/レビュー・単テ指摘/{fname}'
    with open(path, 'r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            phase = row.get('原因工程','').strip()
            if '製造' not in phase:
                continue
            fid = row['機能ID'].strip()
            maker = fid_to_maker.get(fid, '')
            if maker != target:
                continue
            bt = row['バグ 区分'].strip()
            if bt == '新規バグ':
                mfg_bugs[fid] += 1
                mfg_causes[row.get('原因区分','').strip() or '(未記入)'] += 1

# 単テ障害: 埋め込み工程が製造
tante_mfg_bugs = Counter()
tante_mfg_root_causes = Counter()
with open('/workspace/data/レビュー・単テ指摘/単テ障害一覧.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        embed = row['埋め込み工程'].strip()
        if '製造' not in embed:
            continue
        fid12 = row['機能ID'].strip()[:12]
        maker = fid_to_maker.get(fid12, '')
        if maker != target:
            continue
        bt = row['バグ区分'].strip()
        if bt in ('新規バグ', 'バグ（新RS新規機能）'):
            tante_mfg_bugs[fid12] += 1
            tante_mfg_root_causes[row['根本原因区分'].strip() or '(未記入)'] += 1

review_total = sum(mfg_bugs.values())
tante_total = sum(tante_mfg_bugs.values())
all_total = review_total + tante_total
print(f'製造工程の作り込み（担当者: {target}）')
print(f'  レビュー検出: {review_total}件')
print(f'  単テ検出: {tante_total}件')
print(f'  合計: {all_total}件')
print(f'  レビュー原因区分: {dict(mfg_causes)}')
print(f'  単テ根本原因区分: {dict(tante_mfg_root_causes)}')

