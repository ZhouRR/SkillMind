#!/usr/bin/env python3
"""A1"""

import csv
from collections import Counter
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=str, default="")
args = parser.parse_args()



with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = next(reader)
    col = {name.strip(): i for i, name in enumerate(header)}
    rows = [row for row in reader if len(row) > col.get('アプリ','') and row[col['アプリ']].strip() == args.app]

func_count = len(rows)
total_line = sum(int(row[col['製造Line']]) for row in rows if row[col['製造Line']].strip().lstrip('-').isdigit())
total_page = sum(int(row[col['詳設Page']]) for row in rows if row[col['詳設Page']].strip().lstrip('-').isdigit())
total_case = sum(int(row[col['単テCase']]) for row in rows if row[col['単テCase']].strip().lstrip('-').isdigit())

# 区分集計
kubun = Counter(row[col['区分']].strip() for row in rows)
# 言語集計
lang = Counter(row[col['言語']].strip() for row in rows)
# ON/OFF集計
onoff = Counter(row[col['ON/OFF']].strip() for row in rows)
# 所属G集計
group = Counter(row[col['所属G']].strip() for row in rows)

# 担当者一覧
designers = set(row[col['詳設担当']].strip() for row in rows if row[col['詳設担当']].strip())
makers = set(row[col['製造担当']].strip() for row in rows if row[col['製造担当']].strip())
testers = set(row[col['単テ担当']].strip() for row in rows if row[col['単テ担当']].strip())

print(f'機能数={func_count}')
print(f'総KL={total_line/1000:.3f}')
print(f'総Page={total_page}')
print(f'総Case={total_case}')
print(f'区分={dict(kubun)}')
print(f'言語={dict(lang)}')
print(f'ON/OFF={dict(onoff)}')
print(f'所属G={dict(group)}')
print(f'詳設担当者数={len(designers)}')
print(f'製造担当者数={len(makers)}')
print(f'単テ担当者数={len(testers)}')

