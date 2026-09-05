#!/usr/bin/env python3
"""F1-3"""

import csv
from collections import Counter
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--fid", type=str, default="")
parser.add_argument("--kl", type=float, default=0)
args = parser.parse_args()



with open('/workspace/data/レビュー・単テ指摘/製造レビュー指摘一覧.csv', 'r', encoding='utf-8-sig') as f:
    reader = csv.reader(f)
    header = next(reader)
    col = {name.strip(): i for i, name in enumerate(header)}
    
    internal = Counter()
    accept = Counter()
    for row in reader:
        fid = row[0].strip().strip('\"')
        if fid == args.fid:
            src = row[col['指摘元']].strip().strip('\"')
            bug_type = row[col['バグ 区分']].strip().strip('\"') or '(未記入)'
            if src == '内部':
                internal[bug_type] += 1
            elif src == '受入':
                accept[bug_type] += 1
    
    int_total = sum(internal.values())
    acc_total = sum(accept.values())
    total = int_total + acc_total
    print(f'総指摘件数: {total} (内部{int_total}/受入{acc_total})')
    print(f'内部: {dict(internal)}')
    print(f'受入: {dict(accept)}')
    int_new = internal.get('新規バグ', 0)
    acc_new = accept.get('新規バグ', 0)
    print(f'新規バグ: {int_new + acc_new}件 (内部{int_new}/受入{acc_new})')
    print(f'新規バグ密度: {(int_new + acc_new) / args.kl:.3f} 件/KL')

