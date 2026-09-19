"""確認済み patch byte だけを適用し、実回帰と最終提交の内容を照合する一時処理。"""
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
BRANCH = 'perf/audit-export-continuations'
assert os.environ['GITHUB_REPOSITORY'] == REPO
assert os.environ['GITHUB_REF'] == 'refs/heads/' + BRANCH
BLOBS = [
    'cfa3dfa64c5a3601b8a6ac3dedfd6fd449bb91db',
    'e1f83b42e2d46f8c7ae95c490a8b8b3cb5f93e54',
    '3117ec9671fe1138bf07df16957df724e1ac391b',
    '7371b5e95403c78bb7f625b222d6813708f835ba',
    'aca76e3ae4f46a08ffc5cc22965d66697beface3',
    '3ebad63312ad28e01d4c52019031926552f496b1',
    'a5e3822f8a5559051782edeff39d4fc818b8ee10',
    '0d16acfdeb6f222c26a0b3d6180b4ec68804f88e',
    '0e339f07e75f10e6f5759bb3fdf4519a6eff7371',
    'a8fcdc483c859a41ead509c002e3ad7b4289d578',
    '787a9a426fb37975c7b3fdc4a764519012e0d279',
    '0eeaae64473854bbe6c0d53ab6606d42b965c174',
    '93f22f0ba3720220cbfa48f433456c180a82333a',
]
parts = []
for sha in BLOBS:
    request = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/git/blobs/{sha}',
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        part = base64.b64decode(json.load(response)['content'])
    assert hashlib.sha1(b'blob ' + str(len(part)).encode() + b'\0' + part).hexdigest() == sha
    parts.append(part)
encoded = b''.join(parts)
assert len(encoded) == 31572
patch = gzip.decompress(base64.b64decode(encoded, validate=True))
assert len(patch) == 82132
assert hashlib.sha256(patch).hexdigest() == '9e8925c7a43b2f3329522f31f0fd1a1233baa41cd7c8bb71344e6c843ca21eeb'
subprocess.run(['git', 'apply', '--check', '-'], input=patch, check=True)
subprocess.run(['git', 'apply', '-'], input=patch, check=True)

def replace_once(path, old, new):
    """一意な確認済み箇所だけを変更する。"""
    p = Path(path)
    text = p.read_text()
    assert text.count(old) == 1, path
    p.write_text(text.replace(old, new, 1))

# 同じ Run/Integration の子操作を試す。操作ごとの Effect/Proposal identity は別に保つ。
replace_once('SKM/backend/tests/worker/test_receipt_sequence_experiment.py',
    '        self.claims = claims or [self._claim() for _ in range(count)]\n',
    '''        if claims is None:
            first = self._claim()
            self.claims = [replace(first, effect_execution_id=uuid4(), proposal_id=uuid4())
                           for _ in range(count)]
        else:
            self.claims = claims
''')
replace_once('SKM/backend/src/skillmind/agent/audit_export.py',
    '"meaning": "Saved observations and effect receipts; not a business verdict or current remote state.",',
    '"meaning": ("Saved observations and effect receipts; "\n                    "not a business verdict or current remote state."),')
replace_once('SKM/backend/src/skillmind/agent/audit_source.py',
    '"request_content": "Only the stored arguments summary is available for this ToolCall; exact effect payloads remain in proposal records.",',
    '"request_content": ("Only the stored arguments summary is available for this ToolCall; "\n                            "exact effect payloads remain in proposal records."),')
replace_once('SKM/backend/tests/worker/test_receipt_sequence_experiment.py',
    '"CREATE TABLE IF NOT EXISTS steps (position INTEGER PRIMARY KEY, identity TEXT, fingerprint TEXT, attempted INTEGER, receipt TEXT)"',
    '"CREATE TABLE IF NOT EXISTS steps (position INTEGER PRIMARY KEY, "\n            "identity TEXT, fingerprint TEXT, attempted INTEGER, receipt TEXT)"')
# tracked 差分と追加ファイルだけを選び、既存ファイルを大規模に整形しない。
changed = set(subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines())
changed.update(subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], text=True).splitlines())
paths = sorted(p for p in changed if p.startswith(('SKM/', 'docs/')))
new_python = [p for p in paths if p.endswith('.py') and subprocess.run(
    ['git', 'cat-file', '-e', 'HEAD:' + p], capture_output=True).returncode != 0]
python_paths = [p for p in paths if p.endswith('.py')]
ruff = [sys.executable, '-m', 'ruff']
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--select', 'I,F401', '--fix', *python_paths], check=True)
subprocess.run([*ruff, 'format', '--config', 'SKM/backend/pyproject.toml', *new_python], check=True)
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', *new_python], check=True)
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--select', 'F,I', *python_paths], check=True)
root = Path(os.environ['RUNNER_TEMP'])
(root/'runtime-paths.json').write_text(json.dumps(paths))
(root/'source-fingerprint.json').write_text(json.dumps(
    {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}, sort_keys=True))
print('Verified and prepared', len(paths), 'source files')
