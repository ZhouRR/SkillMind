"""原ファイルを検証し、全回帰と最終提交で同じ変更だけを使用する。"""
from __future__ import annotations

import base64
import hashlib
import json
import lzma
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

REPO = 'ZhouRR/SkillMind'
BRANCH = 'feat/multi-resource-routing'
assert os.environ['GITHUB_REPOSITORY'] == REPO
assert os.environ['GITHUB_REF'] == 'refs/heads/' + BRANCH
SHAS = (
    '75997fae985f967e2527c019b6564812ad6b7ec1',
    '61add0f2e2634757c66aa74cfc72d203c99c2452',
    'e0e88d803427dbe0ebfcadadd15303dc72d69bac',
    '7c3775b16b1d303dac729cd97bb350978495aa9c',
    '731f5a1bd1a4478bab504141ae09ac8e173b7d47',
    '0f7e14b1c2181d72c41da47925d0927bfd72b8d1',
)
parts = []
for sha in SHAS:
    request = urllib.request.Request(f'https://api.github.com/repos/{REPO}/git/blobs/{sha}',
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = base64.b64decode(json.load(response)['content'])
    assert hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == sha
    parts.append(data)
raw = lzma.decompress(base64.b64decode(b''.join(parts), validate=True))
assert len(raw) == 73981
assert hashlib.sha256(raw).hexdigest() == 'fd82995de3203cb8d44708c9599ec77bbb5e7e48e643624a520eb12d3812d8c8'
packet = json.loads(raw)
assert packet['base'] == 'c3c41c051e69eacbdf74302c9ac1728b3349a8ea'
for path, expected in packet['originals'].items():
    assert path.startswith(('SKM/', 'docs/')) and '..' not in Path(path).parts
    result = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD:' + path], capture_output=True, text=True)
    actual = result.stdout.strip() if result.returncode == 0 else None
    assert actual == expected, (path, expected, actual)
patch = packet['patch'].encode('utf-8')
subprocess.run(['git', 'apply', '--check', '-'], input=patch, check=True)
subprocess.run(['git', 'apply', '-'], input=patch, check=True)
repair = Path('.github/fix_multi_resource.py')
if repair.exists():
    subprocess.run([sys.executable, str(repair)], check=True)
changed = set(subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines())
changed.update(subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], text=True).splitlines())
paths = sorted(p for p in changed if p.startswith(('SKM/', 'docs/')))
python_paths = [p for p in paths if p.endswith('.py')]
new_python = [p for p in python_paths if subprocess.run(['git', 'cat-file', '-e', 'HEAD:' + p], capture_output=True).returncode]
ruff = [sys.executable, '-m', 'ruff']
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--select', 'I,F401', '--fix', *python_paths], check=True)
subprocess.run([*ruff, 'format', '--config', 'SKM/backend/pyproject.toml', *new_python], check=True)
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--select', 'F,I', *python_paths], check=True)
subprocess.run(['git', 'diff', '--check'], check=True)
root = Path(os.environ['RUNNER_TEMP'])
(root / 'multi-paths.json').write_text(json.dumps(paths))
(root / 'source-fingerprint.json').write_text(json.dumps({p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}, sort_keys=True))
print('PREPARED_FILES', len(paths))
