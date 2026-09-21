"""原 patch の hash を照合し、回帰と最終提交に同じ application byte を使用する。"""
from __future__ import annotations
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

REPO = 'ZhouRR/SkillMind'
BRANCH = 'perf/continuous-tool-execution'
assert os.environ['GITHUB_REPOSITORY'] == REPO
assert os.environ['GITHUB_REF'] == 'refs/heads/' + BRANCH
SHAS = [
    'bd20bd2bcf4df0fe58addf4564a3c639cbcef58f',
    '63b44c9ada7f387f42d5c75327005e9740b57006',
    '7eb013411a8868fcae4fbf2ee71bf999b82c593c',
    'e04fe6ee8a57ff1017f9f92a0c44073373fcd096',
    'dd17e717c5a2acc931f84a027732b65bb92f25ff',
    'e8dd02b5a3d2f5eb47bdc869816bf5c099bf3e14',
    '021a85157c064eed02cb7e107dafdd8b30e8f4f9',
]
parts = []
for sha in SHAS:
    request = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/git/blobs/{sha}',
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = base64.b64decode(json.load(response)['content'])
    assert hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == sha
    parts.append(data)
patch = gzip.decompress(base64.b64decode(b''.join(parts), validate=True))
assert len(patch) == 89658
assert hashlib.sha256(patch).hexdigest() == '7ba34ed029f81c1d633cfcffc59a6b1113a862375c1ad6042c246fd8dc088ede'
subprocess.run(['git', 'apply', '--check', '-'], input=patch, check=True)
subprocess.run(['git', 'apply', '-'], input=patch, check=True)
# Test fixture も production と同じ必須 profile を明示し、認可の default を追加しない。
p = Path('SKM/backend/tests/agent/test_tool_sequence.py')
t = p.read_text()
old = 'registry.resolve_unbound("tool.sequence/v1")'
assert t.count(old) == 1
t = t.replace(old, 'registry.resolve_unbound("tool.sequence/v1", execution_profile="SUPERVISED")')
p.write_text(t)
changed = set(subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines())
changed.update(subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], text=True).splitlines())
paths = sorted(p for p in changed if p.startswith(('SKM/', 'docs/', 'real-flow-test/')))
python_paths = [p for p in paths if p.endswith('.py')]
new_python = [p for p in python_paths if subprocess.run(['git', 'cat-file', '-e', 'HEAD:' + p], capture_output=True).returncode]
ruff = [sys.executable, '-m', 'ruff']
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--select', 'I,F401', '--fix', *python_paths], check=True)
subprocess.run([*ruff, 'format', '--config', 'SKM/backend/pyproject.toml', *new_python], check=True)
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--select', 'F,I', *python_paths], check=True)
root = Path(os.environ['RUNNER_TEMP'])
(root / 'continuous-paths.json').write_text(json.dumps(paths))
(root / 'source-fingerprint.json').write_text(json.dumps({p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}, sort_keys=True))
print('PREPARED_FILES', len(paths))
