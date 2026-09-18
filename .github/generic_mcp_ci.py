"""各 CI job に同じ確認済みコードを適用し、ファイル内容の一致を証明する。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

assert os.environ['GITHUB_REPOSITORY'] == 'ZhouRR/SkillMind'
assert os.environ['GITHUB_REF'] == 'refs/heads/perf/generic-mcp-execution'
for script in ('.github/generic_mcp_patch.py', '.github/generic_mcp_finish.py'):
    subprocess.run([sys.executable, script], check=True)
sha = '5e093eef7720440102df91a2c6ec7e224046c403'
request = urllib.request.Request(
    'https://api.github.com/repos/ZhouRR/SkillMind/git/blobs/' + sha,
    headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
             'Accept': 'application/vnd.github+json', 'User-Agent': 'skillmind-reviewed-mcp-ci'},
)
with urllib.request.urlopen(request, timeout=60) as response:
    data = base64.b64decode(json.load(response)['content'])
assert hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == sha
exec(compile(data, 'reviewed_tuple_patch', 'exec'), {'__name__': '__main__'})
subprocess.run([sys.executable, '.github/generic_mcp_api_fix.py'], check=True)
paths = json.loads((Path(os.environ['RUNNER_TEMP']) / 'generic-mcp-paths.json').read_text())
python_paths = [p for p in paths if p.endswith('.py')]
ruff = [sys.executable, '-m', 'ruff']
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--no-cache',
                '--select', 'I', '--fix', *python_paths], check=True)
subprocess.run([*ruff, 'format', '--config', 'SKM/backend/pyproject.toml', '--no-cache',
                *python_paths], check=True)
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--no-cache',
                *python_paths], check=True)
fingerprint = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sorted(paths)}
(Path(os.environ['RUNNER_TEMP']) / 'source-fingerprint.json').write_text(
    json.dumps(fingerprint, sort_keys=True), encoding='utf-8'
)
print('Verified common source fingerprint for', len(fingerprint), 'paths.')