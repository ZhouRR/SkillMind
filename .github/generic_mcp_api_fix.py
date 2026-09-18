"""API test double を実 signature と同期し、MCP 同意の転送を実 route で検証する。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

state = Path(os.environ['RUNNER_TEMP']) / 'generic-mcp-paths.json'
paths = set(json.loads(state.read_text()))


def load(path: str, expected: str) -> str:
    """原基準の test byte を検証し、既存変更を上書きしない。"""
    data = Path(path).read_bytes()
    actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    assert actual == expected, path
    return data.decode('utf-8')


def once(text: str, old: str, new: str) -> str:
    """対象の一意な既存 assertion/引数だけを変更する。"""
    assert text.count(old) == 1, (old[:80], text.count(old))
    return text.replace(old, new, 1)


p = 'SKM/backend/tests/api/fakes.py'
t = load(p, 'abb35912462b16e41ac70ca93665ea9373ae0190')
old = '        auto_approve_git: bool = False,\n'
assert t.count(old) == 2
t = t.replace(old, old + '        auto_approve_mcp: bool = False,\n')
Path(p).write_text(t, encoding='utf-8')
paths.add(p)

p = 'SKM/backend/tests/api/test_run_creation_authorization_api.py'
t = load(p, '7fd19321896627460374d4a9ed33ee08cdd81940')
t = once(t, '        "auto_approve_git",\n', '        "auto_approve_git",\n        "auto_approve_mcp",\n')
t = once(t, '    body["auto_approve_git"] = enabled\n', '    body["auto_approve_git"] = enabled\n    body["auto_approve_mcp"] = enabled\n')
t = once(t, '    assert create.await_args.kwargs["auto_approve_git"] is enabled\n', '    assert create.await_args.kwargs["auto_approve_git"] is enabled\n    assert lookup.await_args.kwargs["auto_approve_mcp"] is enabled\n    assert create.await_args.kwargs["auto_approve_mcp"] is enabled\n')
t += '''\n\n@pytest.mark.parametrize("value", [True, "true", 1, None])
def test_mcp_consent_rejects_missing_general_consent_or_non_boolean(client, monkeypatch, value):
    """MCP 単独同意や暗黙変換は、原要求確認・新規作成の前に拒否する。"""
    lookup, create = install_creation(client, monkeypatch)
    url, body, headers = creation_request()
    body["auto_approve_mcp"] = value
    auth = application(client).state.auth_service
    response = client.post(url, json=body, headers={**headers, "X-CSRF-Token": auth.csrf_token})
    assert response.status_code == 422
    lookup.assert_not_awaited()
    create.assert_not_awaited()


def test_omitted_mcp_consent_remains_false_on_api_replay_and_creation(client, monkeypatch):
    """既存 DB/Git 同意から MCP の新規権限を推定しない。"""
    lookup, create = install_creation(client, monkeypatch)
    url, body, headers = creation_request()
    body.update(auto_approve=True, auto_approve_git=True)
    auth = application(client).state.auth_service
    response = client.post(url, json=body, headers={**headers, "X-CSRF-Token": auth.csrf_token})
    assert response.status_code == 201
    assert lookup.await_args.kwargs["auto_approve_mcp"] is False
    assert create.await_args.kwargs["auto_approve_mcp"] is False
'''
Path(p).write_text(t, encoding='utf-8')
paths.add(p)
state.write_text(json.dumps(sorted(paths)), encoding='utf-8')
print('API test signatures and consent assertions synchronized.')