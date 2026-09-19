"""同一回帰内容だけを明示分岐へ提交する。main/実環境を変更しない一時処理。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import urllib.request
import xml.etree.ElementTree as ET

repo = 'ZhouRR/SkillMind'
branch = 'perf/audit-export-continuations'
parent = os.environ['GITHUB_SHA']
assert os.environ['GITHUB_REPOSITORY'] == repo
assert os.environ['GITHUB_REF'] == 'refs/heads/' + branch
root = Path(os.environ['RUNNER_TEMP'])
expected = json.loads((root/'source-fingerprint.json').read_text())
groups = list((root/'groups').iterdir())
assert len(groups) == 3
totals = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
for group in groups:
    assert json.loads((group/'source-fingerprint.json').read_text()) == expected
    result = ET.parse(group/'results.xml').getroot()
    for suite in result.iter('testsuite'):
        for key in totals:
            totals[key] += int(suite.get(key, '0'))
assert totals['tests'] > 6000 and totals['failures'] == totals['errors'] == 0
print('VERIFIED_RESULTS', json.dumps(totals))
for path, checksum in expected.items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == checksum, path

def api(path, data=None, method=None):
    """所有リポジトリ内の Git data だけを扱い、token を出力しない。"""
    request = urllib.request.Request('https://api.github.com/repos/' + repo + '/' + path,
        data=None if data is None else json.dumps(data).encode(), method=method,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)

assert api('git/ref/heads/' + branch)['object']['sha'] == parent
paths = sorted(set(expected) | {'docs/index.html'})
entries = []
for path in paths:
    raw = Path(path).read_bytes()
    blob = api('git/blobs', {'content': base64.b64encode(raw).decode(), 'encoding': 'base64'})
    assert blob['sha'] == hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
for path in ('.github/prepare_runtime_changes.py', '.github/publish_runtime_changes.py',
             '.github/workflows/runtime-optimization-review.yml'):
    entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': None})
tree = api('git/trees', {'base_tree': api('git/commits/' + parent)['tree']['sha'], 'tree': entries})
commit = api('git/commits', {'tree': tree['sha'], 'parents': [parent], 'message':
    'perf(runtime): export audited facts and reduce result lookup overhead\n\n'
    'Preserve exact recorded values and the existing Artifact/effect authorization path. '
    'Include isolated receipt/sequence experiments only; production inline execution remains unchanged. '
    'Verified matching source fingerprints, targeted/backend regressions, types and contracts.'})
assert api('git/ref/heads/' + branch)['object']['sha'] == parent
updated = api('git/refs/heads/' + branch, {'sha': commit['sha'], 'force': False}, 'PATCH')
assert updated['object']['sha'] == commit['sha']
print('FINAL_COMMIT=' + commit['sha'])
print('FINAL_TREE=' + tree['sha'])
