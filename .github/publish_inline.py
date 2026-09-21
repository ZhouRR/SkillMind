"""同じ原 byte の全回帰後にだけ review branch を進め、main と業務環境を変更しない。"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import urllib.request
import xml.etree.ElementTree as ET

repo = 'ZhouRR/SkillMind'
branch = 'perf/inline-approved-effects'
parent = os.environ['GITHUB_SHA']
assert os.environ['GITHUB_REPOSITORY'] == repo
assert os.environ['GITHUB_REF'] == 'refs/heads/' + branch
root = Path(os.environ['RUNNER_TEMP'])
expected = json.loads((root/'source-fingerprint.json').read_text())
groups = list((root/'groups').iterdir())
assert len(groups) == 3
totals = dict(tests=0, failures=0, errors=0, skipped=0)
for group in groups:
    assert json.loads((group/'source-fingerprint.json').read_text()) == expected
    for name in ('focused.xml', 'results.xml'):
        result = ET.parse(group/name).getroot()
        for suite in result.iter('testsuite'):
            assert int(suite.get('failures', 0)) == int(suite.get('errors', 0)) == 0
            if name == 'results.xml':
                for key in totals:
                    totals[key] += int(suite.get(key, 0))
assert totals['tests'] > 6000
for path, checksum in expected.items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == checksum, path

def api(path, data=None, method=None):
    request = urllib.request.Request('https://api.github.com/repos/' + repo + '/' + path,
        data=None if data is None else json.dumps(data).encode(), method=method,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)

assert api('git/ref/heads/' + branch)['object']['sha'] == parent
entries = []
for path in sorted(set(expected) | {'docs/index.html'}):
    data = Path(path).read_bytes()
    blob = api('git/blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
    assert blob['sha'] == hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
for path in ('.github/prepare_inline.py', '.github/publish_inline.py', '.github/repair_inline.py',
             '.github/workflows/inline-effect-review.yml'):
    if Path(path).exists():
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': None})
tree = api('git/trees', {'base_tree': api('git/commits/' + parent)['tree']['sha'], 'tree': entries})
commit = api('git/commits', {'tree': tree['sha'], 'parents': [parent], 'message':
    'perf(effects): return approved receipts within the original tool call\n\n'
    'Keep exact proposal, approval, effect and readback facts. Compact optional '
    'management fields; default-off Codex inline delivery retains parent ownership '
    'and falls back to original operation reconciliation, never a new write.'})
assert api('git/ref/heads/' + branch)['object']['sha'] == parent
result = api('git/refs/heads/' + branch, {'sha': commit['sha'], 'force': False}, 'PATCH')
assert result['object']['sha'] == commit['sha']
print('VERIFIED_RESULTS', json.dumps(totals))
print('FINAL_COMMIT=' + commit['sha'])
print('FINAL_TREE=' + tree['sha'])
