#!/usr/bin/env python3
"""A6: 機能別内訳"""
import csv
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--app", type=str, default="")
args = parser.parse_args()


# 機能一覧の読み込み
with open('/workspace/config/JAF規模一覧.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = next(reader)
    col = {name.strip(): i for i, name in enumerate(header)}
    funcs = {}
    for row in reader:
        if len(row) > col['アプリ'] and row[col['アプリ']].strip() == args.app:
            fid = row[col['機能ID']].strip()
            fname = row[col['機能名']].strip()
            line = int(row[col['製造Line']]) if row[col['製造Line']].strip().lstrip('-').isdigit() else 0
            funcs[fname] = {'id': fid, 'line': line, 'tickets': 0}

# チケットを機能名にマッチング（ticketsはA2で取得済みのJSONを想定）
# ... マッチング処理 ...

# 結果出力
for fname, info in sorted(funcs.items(), key=lambda x: x[1]['tickets'], reverse=True):
    kl = info['line'] / 1000 if info['line'] > 0 else 0
    density = info['tickets'] / kl if kl > 0 else 0
    fid = info['id']
    tickets = info['tickets']
    print(f'{fid}\t{fname}\t{kl:.3f}\t{tickets}\t{density:.3f}')
