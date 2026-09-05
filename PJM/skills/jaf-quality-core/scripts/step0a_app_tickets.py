#!/usr/bin/env python3
"""Step 0a"""

import csv
from collections import Counter



with open('/workspace/data/issues.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = next(reader)
    col = {name.strip(): i for i, name in enumerate(header)}
    rows = [row for row in reader if row]

app_idx = col.get('検出時アプリ')
c = Counter()
empty = 0
for row in rows:
    v = row[app_idx].strip() if app_idx is not None and len(row) > app_idx else ''
    if v: c[v] += 1
    else: empty += 1

# 標準選択肢順で出力（mappings の values 順）
app_order = ['調査中','受付アプリ','指令アプリ','自動指令アプリ','車両動態管理アプリ','スマホ指令アプリ','基地業務アプリ','車両業務アプリ','乗務予定アプリ','車載連携アプリ','マスターメンテアプリ','外部連携(対JAF内システム)アプリ','外部連携(対外業務連携)アプリ','ランチャーアプリ','損保データ連携アプリ','AP基盤','端末','サーバ','ネットワーク','クラウド']
for i, app in enumerate(app_order, 1):
    cnt = c.get(app, 0)
    print(f'{i:2d}. {app} ({cnt}件)')
total = len(rows)
print(f'21. **絞り込みなし（全件）** ({total}件)')
print(f'※ 未記入: {empty}件')

