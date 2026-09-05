#!/usr/bin/env python3
"""P1 — JAF規模一覧から担当者別の役割・規模を集計"""

import csv
from collections import defaultdict
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--name", type=str, default="")
parser.add_argument("--app", type=str, default="")
args = parser.parse_args()

target = args.name
app_filter = args.app

# 役割定義: (役割名, 担当列名, 規模列名, 規模単位)
ROLES = [
    ("詳設担当",     "詳設担当",   "詳設Page", "Page"),
    ("詳設RV担当",   "詳設レビューア", "詳設Page", "Page"),
    ("製造担当",     "製造担当",   "製造Line", "Line"),
    ("製造RV担当",   "製造レビューア", "製造Line", "Line"),
    ("単テ担当",     "単テ担当",   "単テCase", "Case"),
]

results = {}  # role_name -> {"count": int, "scale": int, "apps": set}

with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        if app_filter and row['アプリ'].strip() != app_filter:
            continue
        app = row['アプリ'].strip()

        for role_name, name_col, scale_col, unit in ROLES:
            if row[name_col].strip() != target:
                continue
            if role_name not in results:
                results[role_name] = {"count": 0, "scale": 0, "apps": set()}
            results[role_name]["count"] += 1
            scale_val = row[scale_col].strip()
            results[role_name]["scale"] += int(scale_val) if scale_val.lstrip('-').isdigit() else 0
            results[role_name]["apps"].add(app)

print(f'担当者: {target}')
for role_name, _, _, unit in ROLES:
    if role_name in results:
        r = results[role_name]
        apps_str = '、'.join(sorted(r['apps']))
        print(f'{role_name}: {r["count"]}機能, {r["scale"]}{unit} | {apps_str}')
    else:
        print(f'{role_name}: 0機能, 0{unit} | -')
