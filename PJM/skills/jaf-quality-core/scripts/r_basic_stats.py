#!/usr/bin/env python3
"""R1"""

import csv
from collections import Counter



# JAF規模一覧から機能ID→アプリの辞書
fid_to_app = {}
with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        fid_to_app[row['機能ID'].strip()] = row['アプリ'].strip()

total_kl_by_app = Counter()
total_page_by_app = Counter()
with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        app = row['アプリ'].strip()
        line = int(row['製造Line']) if row['製造Line'].strip().lstrip('-').isdigit() else 0
        page = int(row['詳設Page']) if row['詳設Page'].strip().lstrip('-').isdigit() else 0
        total_kl_by_app[app] += line
        total_page_by_app[app] += page

print('=== アプリ別 総KL / 総Page ===')
for app in sorted(total_kl_by_app.keys()):
    print(f'  {app}: {total_kl_by_app[app]/1000:.3f} KL, {total_page_by_app[app]} Page')

for fname, label, key_type in [
    ('詳設レビュー指摘一覧.csv', '詳設レビュー', '完全一致'),
    ('製造レビュー指摘一覧.csv', '製造レビュー', '完全一致'),
    ('単テ障害一覧.csv', '単テ障害', '先頭12桁')
]:
    path = f'/workspace/data/レビュー・単テ指摘/{fname}'
    
    # アプリ別集計
    app_bugs = Counter()
    app_new_bugs = Counter()
    bug_types = Counter()
    cause_cats = Counter()
    
    with open(path, 'r', encoding='utf-8-sig') as f:
        if fname.endswith('単テ障害一覧.csv'):
            reader = csv.DictReader(f)
            for row in reader:
                fid = row['機能ID'].strip()[:12] if key_type == '先頭12桁' else row['機能ID'].strip()
                app = fid_to_app.get(fid, '(不明)')
                bt = row['バグ区分'].strip() or '(未記入)'
                cc = row.get('原因区分', row.get('原因区分', '')).strip() or '(未記入)'
                app_bugs[app] += 1
                bug_types[bt] += 1
                cause_cats[cc] += 1
                if bt in ('新規バグ', 'バグ（新RS新規機能）'):
                    app_new_bugs[app] += 1
        else:
            reader = csv.reader(f)
            header = next(reader)
            col = {name.strip(): i for i, name in enumerate(header)}
            for row in reader:
                fid = row[0].strip().strip('\"')
                app = fid_to_app.get(fid, '(不明)')
                bt = row[col.get('バグ 区分', 0)].strip().strip('\"') or '(未記入)'
                cc = row[col.get('原因区分', 0)].strip().strip('\"') or '(未記入)'
                app_bugs[app] += 1
                bug_types[bt] += 1
                cause_cats[cc] += 1
                if bt == '新規バグ':
                    app_new_bugs[app] += 1
    
    total = sum(app_bugs.values())
    new_total = sum(app_new_bugs.values())
    print(f'\n[{label}] 総件数: {total} (新規バグ {new_total})')
    print(f'  バグ区分分布: {dict(bug_types)}')
    print(f'  原因区分(上位5): {cause_cats.most_common(5)}')
    
    # アプリ別 新規バグ密度
    print(f'  アプリ別 新規バグ数 / 密度:')
    for app in sorted(app_new_bugs.keys(), key=lambda a: app_new_bugs[a], reverse=True):
        n = app_new_bugs[app]
        if fname == '詳設レビュー指摘一覧.csv':
            page = total_page_by_app.get(app, 0)
            density = f'{n/page:.3f} 件/Page' if page > 0 else 'N/A'
            print(f'    {app}: {n}件, {density}')
        else:
            kl = total_kl_by_app.get(app, 0) / 1000
            density = f'{n/kl:.3f} 件/KL' if kl > 0 else 'N/A'
            print(f'    {app}: {n}件, {density}')

