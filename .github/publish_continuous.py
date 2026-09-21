"""検証済みの同一内容だけを元 branch へ提交し、main と業務環境に触れない。"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import urllib.request
import xml.etree.ElementTree as ET

repo = 'ZhouRR/SkillMind'
branch = 'perf/continuous-tool-execution'
parent = os.environ['GITHUB_SHA']
assert os.environ['GITHUB_REPOSITORY'] == repo
assert os.environ['GITHUB_REF'] == 'refs/heads/' + branch
root = Path(os.environ['RUNNER_TEMP'])
expected = json.loads((root / 'source-fingerprint.json').read_text())
groups = list((root / 'groups').iterdir())
assert len(groups) == 3
totals = dict(tests=0, failures=0, errors=0, skipped=0)
for group in groups:
    assert json.loads((group / 'source-fingerprint.json').read_text()) == expected
    for name in ('focused.xml', 'results.xml'):
        result = ET.parse(group / name).getroot()
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
for path in ('.github/prepare_continuous.py', '.github/publish_continuous.py',
             '.github/review_continuous.py', '.github/workflows/continuous-execution-review.yml'):
    entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': None})
tree = api('git/trees', {'base_tree': api('git/commits/' + parent)['tree']['sha'], 'tree': entries})
commit = api('git/commits', {'tree': tree['sha'], 'parents': [parent], 'message':
    'perf(runtime): reuse approved continuation transports and sequence bounded local tools\n\n'
    'Return compact audit indexes without rewriting source facts. Preserve independent '
    'claims, effect approvals and readback. Restrict sequences to authorized read/local tools; '
    'external effect sequences and same-tool inline delivery are not introduced.'})
assert api('git/ref/heads/' + branch)['object']['sha'] == parent
result = api('git/refs/heads/' + branch, {'sha': commit['sha'], 'force': False}, 'PATCH')
assert result['object']['sha'] == commit['sha']
print('VERIFIED_RESULTS', json.dumps(totals))
print('FINAL_COMMIT=' + commit['sha'])
print('FINAL_TREE=' + tree['sha'])
