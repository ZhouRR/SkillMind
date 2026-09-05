"""マスターメンテアプリ 機能ごと担当者・レビューア一覧を生成 (xlsm C8/C9 優先)"""
import csv
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

JAF_SCALE = "/workspace/config/JAF規模一覧.csv"
DD_REVIEW = "/workspace/data/レビュー・単テ指摘/詳設レビュー指摘一覧.csv"
MFG_REVIEW = "/workspace/data/レビュー・単テ指摘/製造レビュー指摘一覧.csv"
XLSM_DIR = "/workspace/レビュー・単テ指摘原本/05_詳細設計"
OUTPUT = "/workspace/output/マスターメンテアプリ_機能別担当者一覧.csv"

# --- Step 1: xlsm からC8(レビューイ=詳設担当), C9(レビューア) を抽出 ---
ns = {'ns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
xlsm_data = defaultdict(lambda: {'dd_person': set(), 'dd_reviewer': set()})

for fname in sorted(os.listdir(XLSM_DIR)):
    if ("JAFRSLO" not in fname and "JAFRSLB" not in fname) or "内部レビュー" not in fname:
        continue
    if not fname.endswith('.xlsm'):
        continue
    filepath = os.path.join(XLSM_DIR, fname)
    m = re.search(r'(JAFRSL[OB]\d{5})', fname)
    if not m:
        continue
    fid = m.group(1)
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            shared_strings = []
            if 'xl/sharedStrings.xml' in z.namelist():
                with z.open('xl/sharedStrings.xml') as f:
                    tree = ET.parse(f)
                    for si in tree.findall('.//ns:si', ns):
                        text = ''.join(t.text or '' for t in si.findall('.//ns:t', ns))
                        shared_strings.append(text)
            with z.open('xl/worksheets/sheet1.xml') as f:
                tree = ET.parse(f)
                for row in tree.findall('.//ns:row', ns):
                    for cell in row.findall('ns:c', ns):
                        ref = cell.get('r')
                        if ref not in ('C8', 'C9'):
                            continue
                        t = cell.get('t')
                        v = cell.find('ns:v', ns)
                        if v is not None and v.text is not None:
                            if t == 's':
                                idx = int(v.text)
                                val = shared_strings[idx] if idx < len(shared_strings) else ''
                            else:
                                val = v.text
                            if val.strip():
                                if ref == 'C8':
                                    xlsm_data[fid]['dd_person'].add(val.strip())
                                else:
                                    xlsm_data[fid]['dd_reviewer'].add(val.strip())
    except Exception as e:
        print(f"WARN: {fname}: {e}")

# --- Step 2: JAF規模一覧からマスターメンテアプリの行を抽出 ---
functions = []
with open(JAF_SCALE, "r", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    header = next(reader)
    for row in reader:
        if len(row) < 20:
            continue
        if row[3].strip() == "マスターメンテアプリ":
            functions.append(row)

# --- Step 3: 詳設レビュー指摘から検出者名を集約 (内部のみ) ---
dd_reviewers = defaultdict(set)
with open(DD_REVIEW, "r", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    next(reader)
    for row in reader:
        if len(row) < 11:
            continue
        if row[1].strip() != "内部":
            continue
        fid = row[0].strip()
        reviewer = row[10].strip()
        if fid and reviewer:
            dd_reviewers[fid].add(reviewer)

# --- Step 4: 製造レビュー指摘から検出者名を集約 (内部のみ) ---
mfg_reviewers = defaultdict(set)
with open(MFG_REVIEW, "r", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    next(reader)
    for row in reader:
        if len(row) < 11:
            continue
        if row[1].strip() != "内部":
            continue
        fid = row[0].strip()
        reviewer = row[10].strip()
        if fid and reviewer:
            mfg_reviewers[fid].add(reviewer)

# --- Step 5: 統合してCSV出力 ---
with open(OUTPUT, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([
        "No", "機能ID", "機能名", "区分", "言語", "ON/OFF",
        "詳細設計担当(xlsm C8優先)", "詳細設計Page",
        "内部DDR指摘件数", "外部DDR指摘件数",
        "詳細設計レビューア(内部)", "詳細設計レビューア(xlsm C9)",
        "製造担当", "製造Line",
        "内部CDI指摘件数", "外部CDI指摘件数",
        "ソースレビューア(内部)",
        "単テ担当", "単テCase"
    ])
    for row in functions:
        fid = row[1].strip()
        onoff = row[5].strip()

        # 詳細設計担当: xlsm C8 を優先、なければJAF規模一覧の値
        dd_csv = row[10].strip()
        dd_csv = "" if dd_csv == "#N/A" else dd_csv
        dd_xlsm = sorted(xlsm_data.get(fid, {}).get('dd_person', set()))
        dd_person = "\n".join(dd_xlsm) if dd_xlsm else dd_csv

        # 詳細設計レビューア(xlsm C9)
        dd_rev_xlsm = sorted(xlsm_data.get(fid, {}).get('dd_reviewer', set()))
        dd_rev_csv = sorted(dd_reviewers.get(fid, set()))

        # 製造担当
        mfg_csv = row[14].strip()
        mfg_csv = "" if mfg_csv == "#N/A" else mfg_csv
        mfg_rev = sorted(mfg_reviewers.get(fid, set()))

        # 単テ担当
        ut_csv = row[18].strip()
        ut_csv = "" if ut_csv == "#N/A" else ut_csv

        writer.writerow([
            row[0], fid, row[2].strip(), row[4].strip(), row[8].strip(), onoff,
            dd_person, row[11].strip(),
            row[12].strip(), row[13].strip(),
            "\n".join(dd_rev_csv), "\n".join(dd_rev_xlsm),
            mfg_csv, row[15].strip(),
            row[16].strip(), row[17].strip(),
            "\n".join(mfg_rev),
            ut_csv, row[19].strip()
        ])

count = len(functions)
on_filled = sum(1 for row in functions if row[5].strip() == "ON" and xlsm_data.get(row[1].strip(), {}).get('dd_person'))
print(f"出力完了: {OUTPUT}")
print(f"機能数: {count}")
print(f"ON(オンサイト) 補完: {on_filled}/80")
print(f"xlsm C8 未発見: {sum(1 for row in functions if not xlsm_data.get(row[1].strip(), {}).get('dd_person'))} 件")
