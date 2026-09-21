"""原 byte を検証し、回帰と最終提交で同じ application 内容を使用する。"""
from __future__ import annotations
import base64
import hashlib
import json
import lzma
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import urllib.request

REPO = 'ZhouRR/SkillMind'
BRANCH = 'perf/inline-approved-effects'
assert os.environ['GITHUB_REPOSITORY'] == REPO
assert os.environ['GITHUB_REF'] == 'refs/heads/' + BRANCH
SHAS = [
 '657341bbbecdc535096db58db544b9181917d55f',
 '5cbdb7b4530e310c7377b601194703639b94883a',
 'a6714ea6d9ee474ea791087ac4fb35335fc0e842',
 'fbd1edb4d0929e69d786e3a2e955cd1dfaf525fd',
 '4142b6293809f357bc3ffc0bb2ee81292a89ba4a',
 '32d2ff83661a117e5c4b4c6e1916cb46a7c37cd1',
 '6144c2b1cbcc9257c348d5028716c91d1d549bb0',
 'ae300afb1267ef1192bb1319cfb6b18cdfbe5810',
 'f5b261b7f95e8a30b74f916620fedac7c1c49f8e',
 '9664a53cf4568d46e2373671d8a2065eb01e11c7',
 'ca736f26bd1935aa100476cb3bb3855f6ffea489',
]
parts = []
for sha in SHAS:
    request = urllib.request.Request(f'https://api.github.com/repos/{REPO}/git/blobs/{sha}',
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = base64.b64decode(json.load(response)['content'])
    assert hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == sha
    parts.append(data)
patch = lzma.decompress(base64.b64decode(b''.join(parts), validate=True))
assert len(patch) == 93458
assert hashlib.sha256(patch).hexdigest() == '8949fc8a8391cbe489cffab8ee2b0a3152bee0d2eec859c93e383c44c2a154bc'
subprocess.run(['git', 'apply', '--check', '-'], input=patch, check=True)
subprocess.run(['git', 'apply', '-'], input=patch, check=True)
for script in ('.github/repair_inline.py', '.github/finish_inline.py'):
    subprocess.run([sys.executable, script], check=True)
# 新しい説明は現行 runtime のみ。旧指示の byte/hash 回帰は変更しない。
p = Path('SKM/backend/src/skillmind/agent/task_brief.py')
t = p.read_text()
marker = '    sections.append(\n        "For change.propose, '
assert t.count(marker) == 1
start = t.index(marker)
end = t.index('\n    sections.append(', start + len(marker))
t = t[:start] + '    if runtime_policy(brief) == "skillmind.runtime/v4":\n' + textwrap.indent(t[start:end], '    ') + t[end:]
p.write_text(t)
p = Path('SKM/backend/tests/effects/test_compact_proposal.py')
t = p.read_text()
old = '''    from skillmind.agent.task_brief import render_task_brief_prompt
    from tests.agent.test_task_brief import _build

    prompt = render_task_brief_prompt(_build().brief, input_json={}, output_schema={})
'''
new = '''    from skillmind.agent.task_brief import build_agent_task_brief, render_task_brief_prompt
    from tests.agent.test_task_brief import RUN_ID, _blueprint, _limits, _manifest, _task_snapshot, _tools

    manifest = _manifest(blueprint=_blueprint())
    compiled = build_agent_task_brief(
        run_id=RUN_ID,
        task_snapshot={**_task_snapshot(manifest), "runtime_policy": "skillmind.runtime/v4"},
        manifest=manifest, selected_sources={}, tools=_tools(), limits=_limits(), model="fixture-model",
    )
    prompt = render_task_brief_prompt(compiled.brief, input_json={}, output_schema={})
'''
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t)
changed = set(subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines())
changed.update(subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], text=True).splitlines())
paths = sorted(p for p in changed if p.startswith(('SKM/', 'docs/')))
python_paths = [p for p in paths if p.endswith('.py')]
new_python = [p for p in python_paths if subprocess.run(['git', 'cat-file', '-e', 'HEAD:' + p], capture_output=True).returncode]
ruff = [sys.executable, '-m', 'ruff']
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--select', 'I,F401', '--fix', *python_paths], check=True)
subprocess.run([*ruff, 'format', '--config', 'SKM/backend/pyproject.toml', *new_python, 'SKM/backend/src/skillmind/effects/proposal.py'], check=True)
subprocess.run([*ruff, 'check', '--config', 'SKM/backend/pyproject.toml', '--select', 'F,I', *python_paths], check=True)
root = Path(os.environ['RUNNER_TEMP'])
(root/'inline-paths.json').write_text(json.dumps(paths))
(root/'source-fingerprint.json').write_text(json.dumps({p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}, sort_keys=True))
print('PREPARED_FILES', len(paths))
