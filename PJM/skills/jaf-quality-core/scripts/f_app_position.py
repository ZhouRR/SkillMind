#!/usr/bin/env python3
"""F6"""

import csv
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=str, default="")
args = parser.parse_args()


with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = next(reader)
    col = {name.strip(): i for i, name in enumerate(header)}
    total = sum(int(row[col['製造Line']]) for row in reader if len(row) > col.get('アプリ','') and row[col['アプリ']].strip() == args.app)
    print(f'総KL={total/1000:.3f}')

