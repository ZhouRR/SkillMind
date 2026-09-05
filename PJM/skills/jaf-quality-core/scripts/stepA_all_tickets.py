#!/usr/bin/env python3
"""Step A"""

import csv
from collections import Counter



with open('/workspace/data/issues.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = next(reader)
    col = {name.strip(): i for i, name in enumerate(header)}
    rows = [row for row in reader if row]

def safe(row, name):
    idx = col.get(name)
    return row[idx].strip() if idx is not None and len(row) > idx else ''

# 集計対象フィールド
targets = [
    '原因工程', '原因区分', 'バグ区分', 'バグ原因', '原因区分_AI',
    '検出工程', '横展開有無', '再現性',
    '検出時アプリ', '原因アプリ', '重要度', '摘出すべき工程', '状態',
]

for name in targets:
    if name not in col:
        continue
    c = Counter()
    empty = 0
    for row in rows:
        v = safe(row, name)
        if v: c[v] += 1
        else: empty += 1
    print(f'FIELD:{name}')
    print(f'EMPTY:{empty}')
    for val, cnt in c.most_common():
        print(f'{cnt}\t{val}')
    print('END')

