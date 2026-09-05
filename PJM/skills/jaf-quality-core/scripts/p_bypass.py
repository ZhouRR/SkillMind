#!/usr/bin/env python3
"""P5"""

import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--design_review", type=int, default=0)
parser.add_argument("--design_tante", type=int, default=0)
parser.add_argument("--mfg_review", type=int, default=0)
parser.add_argument("--mfg_tante", type=int, default=0)
args = parser.parse_args()


# Step P3, P4 の集計結果から算出
design_review = args.design_review
design_tante = args.design_tante
mfg_review = args.mfg_review
mfg_tante = args.mfg_tante

d_total = design_review + design_tante
m_total = mfg_review + mfg_tante

print('=== 工程すり抜け分析 ===')
if d_total > 0:
    print(f'設計工程: レビュー検出率={design_review/d_total*100:.1f}%, 単テ流出率={design_tante/d_total*100:.1f}%')
else:
    print(f'設計工程: データなし')
if m_total > 0:
    print(f'製造工程: レビュー検出率={mfg_review/m_total*100:.1f}%, 単テ流出率={mfg_tante/m_total*100:.1f}%')
else:
    print(f'製造工程: データなし')

total_review = design_review + mfg_review
total_tante = design_tante + mfg_tante
total = total_review + total_tante
if total > 0:
    print(f'全体: レビュー検出率={total_review/total*100:.1f}%, 単テ流出率={total_tante/total*100:.1f}%')

