#!/usr/bin/env python3
"""R2"""

import csv
from collections import Counter, defaultdict



# 機能ID→アプリ
fid_to_app = {}
with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        fid_to_app[row['機能ID'].strip()] = row['アプリ'].strip()

# === クロス集計①: アプリ × バグ区分 ===
print('【クロス①: アプリ × バグ区分】')
for fname, label, key_type in [
    ('詳設レビュー指摘一覧.csv', '詳設レビュー', '完全一致'),
    ('製造レビュー指摘一覧.csv', '製造レビュー', '完全一致'),
    ('単テ障害一覧.csv', '単テ障害', '先頭12桁')
]:
    cross = defaultdict(Counter)
    path = f'/workspace/data/レビュー・単テ指摘/{fname}'
    print(f'\n--- {label} ---')
    
    if fname.endswith('単テ障害一覧.csv'):
        with open(path, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                fid = row['機能ID'].strip()[:12]
                app = fid_to_app.get(fid, '(不明)')
                bt = row['バグ区分'].strip() or '(未記入)'
                cross[app][bt] += 1
    else:
        with open(path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            header = next(reader)
            col = {name.strip(): i for i, name in enumerate(header)}
            for row in reader:
                fid = row[0].strip().strip('\"')
                app = fid_to_app.get(fid, '(不明)')
                bt = row[col['バグ 区分']].strip().strip('\"') or '(未記入)'
                cross[app][bt] += 1
    
    # 表示
    apps = sorted(cross.keys(), key=lambda a: sum(cross[a].values()), reverse=True)
    bug_types = sorted(set(bt for app in cross for bt in cross[app]))
    header_row = 'アプリ | ' + ' | '.join(bug_types) + ' | 合計'
    print(header_row)
    print('|'.join(['---'] * (len(bug_types) + 2)))
    for app in apps:
        total = sum(cross[app].values())
        vals = ' | '.join(str(cross[app].get(bt, 0)) for bt in bug_types)
        print(f'{app} | {vals} | {total}')

# === クロス集計②: アプリ × 工程すり抜け ===
print(f'\n【クロス②: アプリ × 工程すり抜け（レビュー検出率）】')
# アプリ別に詳設+製造レビュー新規バグ / 単テ新規バグ を比較
app_review_new = Counter()
app_tante_new = Counter()

for fname, key_type in [
    ('詳設レビュー指摘一覧.csv', '完全一致'),
    ('製造レビュー指摘一覧.csv', '完全一致'),
]:
    path = f'/workspace/data/レビュー・単テ指摘/{fname}'
    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = next(reader)
        col = {name.strip(): i for i, name in enumerate(header)}
        for row in reader:
            fid = row[0].strip().strip('\"')
            app = fid_to_app.get(fid, '(不明)')
            bt = row[col['バグ 区分']].strip().strip('\"')
            if bt == '新規バグ':
                app_review_new[app] += 1

with open('/workspace/data/レビュー・単テ指摘/単テ障害一覧.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        fid = row['機能ID'].strip()[:12]
        app = fid_to_app.get(fid, '(不明)')
        bt = row['バグ区分'].strip()
        if bt in ('新規バグ', 'バグ（新RS新規機能）'):
            app_tante_new[app] += 1

for app in sorted(set(list(app_review_new.keys()) + list(app_tante_new.keys()))):
    rv = app_review_new.get(app, 0)
    tt = app_tante_new.get(app, 0)
    total = rv + tt
    if total > 0:
        rate = rv / total * 100
        print(f'  {app}: レビュー{rv}件 + 単テ{tt}件 = {total}件, レビュー検出率 {rate:.1f}%')

# === 機能別ランキング ===
print(f'\n【機能別 総指摘・障害数 ランキング TOP10】')
func_total = Counter()

for fname, key_type in [
    ('詳設レビュー指摘一覧.csv', '完全一致'),
    ('製造レビュー指摘一覧.csv', '完全一致'),
    ('単テ障害一覧.csv', '先頭12桁'),
]:
    path = f'/workspace/data/レビュー・単テ指摘/{fname}'
    if fname.endswith('単テ障害一覧.csv'):
        with open(path, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                fid = row['機能ID'].strip()[:12]
                func_total[fid] += 1
    else:
        with open(path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                fid = row[0].strip().strip('\"')
                func_total[fid] += 1

for fid, cnt in func_total.most_common(10):
    app = fid_to_app.get(fid, '(不明)')
    print(f'  {fid} [{app}]: {cnt}件')

