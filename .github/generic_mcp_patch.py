"""固定 base の確認済み差分だけを隔離 checkout へ適用し、検証後の tree を準備する。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = 'ZhouRR/SkillMind'
BASE = '24485544e697e2612793c55fe5266fee071b1f86'
BRANCH = 'perf/generic-mcp-execution'
ROOT = Path.cwd()
STATE = Path(os.environ['RUNNER_TEMP']) / 'generic-mcp-paths.json'
NEW = {
    'SKM/backend/src/skillmind/integrations/mcp_schema.py': 'ff54fee314e086af227e0d182e4880d4f38b74c5',
    'SKM/backend/src/skillmind/runs/realtime_buffer.py': 'df65f6a9e796cd9b3593743273d76151c8d7ebb3',
    'SKM/backend/tests/agent/test_mcp_hot_path.py': 'aa6a29ca6b0c51f89c1540ae78621b2746138d08',
    'SKM/backend/tests/runs/test_realtime_buffer.py': '88da2ba6430049e8dc9a335a89927033d84cf7d1',
    'SKM/backend/tests/runs/test_mcp_run_approval.py': '473aeb27e55ee520f74f4168403a8ee22c5ccb14',
}
changed = set()


def api(path, data=None):
    """同じ repository の Git API のみ使用し、認証情報をログへ出さない。"""
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + '/' + path,
        data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                 'User-Agent': 'skillmind-reviewed-mcp-patch'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def sha(content):
    """Git blob hash を byte 列で照合する。"""
    return hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()


def load(path, expected=None):
    """原 byte と一致した source にだけ、確認済みの置換を適用する。"""
    data = Path(path).read_bytes()
    if expected:
        assert sha(data) == expected, ('base differs', path)
    return data.decode('utf-8')


def put(path, text):
    """追加/変更 path を記録し、許可した領域以外を書かない。"""
    assert path.startswith(('SKM/backend/', 'SKM/web/', 'SKM/contracts/', 'docs/'))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')
    changed.add(path)


def once(text, old, new):
    """曖昧な置換や原文変更は停止し、現在 source を推測で上書きしない。"""
    assert text.count(old) == 1, ('ambiguous anchor', old[:120], text.count(old))
    return text.replace(old, new, 1)


def publish():
    """検証済み差分を tree にするだけで、branch/PR や本番環境を変更しない。"""
    paths = set(json.loads(STATE.read_text()))
    paths.add('docs/index.html')
    for path in subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines():
        if path.startswith('SKM/contracts/openapi/'):
            paths.add(path)
        else:
            assert path in paths, ('unexpected diff', path)
    base_commit = api('git/commits/' + BASE)
    original = api('git/trees/' + base_commit['tree']['sha'] + '?recursive=1')
    assert original['truncated'] is False
    files = {entry['path']: entry for entry in original['tree'] if entry['type'] != 'tree'}
    entries = []
    for path in sorted(paths):
        text = Path(path).read_text(encoding='utf-8')
        expected = sha(text.encode())
        if path in files and files[path]['sha'] == expected:
            continue
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'content': text})
        print('VERIFIED', path, expected)
        files[path] = {'path': path, 'mode': '100644', 'type': 'blob', 'sha': expected}
    tree = api('git/trees', {'base_tree': base_commit['tree']['sha'], 'tree': entries})
    actual = api('git/trees/' + tree['sha'] + '?recursive=1')
    assert actual['truncated'] is False
    shape = lambda data: {p: (v['mode'], v['type'], v['sha']) for p, v in data.items()}
    assert shape(files) == shape({e['path']: e for e in actual['tree'] if e['type'] != 'tree'})
    assert '.github/generic_mcp_patch.py' not in files
    assert '.github/workflows/generic-mcp-review.yml' not in files
    print('PREPARED_TREE=' + tree['sha'])


assert os.environ['GITHUB_REPOSITORY'] == REPO
assert os.environ['GITHUB_REF'] == 'refs/heads/' + BRANCH
if '--publish' in sys.argv:
    publish()
    raise SystemExit

for path, identity in NEW.items():
    assert not Path(path).exists(), ('new path exists', path)
    value = api('git/blobs/' + identity)
    data = base64.b64decode(value['content'])
    assert sha(data) == identity
    put(path, data.decode('utf-8'))

p = 'SKM/backend/src/skillmind/integrations/mcp_tools.py'
t = load(p, '43527c8082b9bf00163e08ed9a517d49c9a53c4d')
t = once(t, 'from collections.abc import Mapping\n', 'from collections.abc import Mapping\nfrom copy import deepcopy\nfrom functools import lru_cache\n')
t = once(t, 'from jsonschema import Draft202012Validator, FormatChecker\nfrom jsonschema.exceptions import SchemaError\nfrom referencing import Registry\n', 'from skillmind.integrations.mcp_schema import validate_schema, validate_value\n')
start, end = t.index('def _schema('), t.index('def normalize_catalog(')
t = t[:start] + t[end:]
t = once(t, 'def normalize_catalog(value: Any) -> dict[str, Any]:', '@lru_cache(maxsize=16)\ndef _normalized_catalog(encoded: str) -> dict[str, Any]:')
t = once(t, '    if not isinstance(value, dict) or set(value) != {"server", "tools"}:', '    value = json.loads(encoded)\n    if not isinstance(value, dict) or set(value) != {"server", "tools"}:')
t = t.replace('_schema(entry["input_schema"])', 'validate_schema(entry["input_schema"])').replace('_schema(entry["output_schema"])', 'validate_schema(entry["output_schema"])')
start, end = t.index('def configured_tool('), t.index('def tool_access(')
t = t[:start] + '''def _catalog(value: Any) -> dict[str, Any]:
    """契約の内容だけを有界に cache し、入力 dict の後続変更を検出する。"""
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > 262_144:
        raise ValueError("MCP tool catalog exceeds its limit")
    return _normalized_catalog(encoded)


def normalize_catalog(value: Any) -> dict[str, Any]:
    """公開する snapshot は私有 cache と分離し、呼出し側の変更を共有しない。"""
    return deepcopy(_catalog(value))


def configured_tool(config: Mapping[str, Any], scope: Mapping[str, Any], name: str) -> dict[str, Any]:
    """単一要求では対象工具だけを解決する。現在の権限は毎回検証する。"""
    if config.get("tool_profile") != PROFILE or name not in scope.get("tool_names", []):
        raise ValueError("MCP tool is outside the configured scope")
    if tool_access(config, name) not in {"read", "call"}:
        raise ValueError("MCP tool permission is missing")
    for entry in _catalog(config.get("tool_catalog"))["tools"]:
        if entry["name"] == name:
            return deepcopy(entry)
    raise ValueError("MCP tool is missing from the frozen catalog")


def configured_tools(config: Mapping[str, Any], scope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一覧公開だけ全 scope を一回走査し、工具数ごとの全 catalog 再検証を避ける。"""
    if config.get("tool_profile") != PROFILE:
        raise ValueError("MCP tool profile is invalid")
    by_name = {entry["name"]: entry for entry in _catalog(config.get("tool_catalog"))["tools"]}
    result = []
    for name in scope.get("tool_names", []):
        if name not in by_name or tool_access(config, name) not in {"read", "call"}:
            raise ValueError("MCP tool permission or contract is missing")
        result.append(deepcopy(by_name[name]))
    return result


''' + t[end:]
start, end = t.index('    _schema(schema)', t.index('def validate_tool_value')), t.index('\n\ndef parse_result')
t = t[:start] + '    validate_value(schema, value)\n' + t[end:]
put(p, t)

p = 'SKM/backend/src/skillmind/agent/mcp_tools_provider.py'
t = load(p, '3ff99acd4f21e9d097c6d7c7e45abb3920517e79')
t = once(t, '    configured_tool,\n', '    configured_tool,\n    configured_tools,\n')
t = once(t, '''            catalog = normalize_catalog(bound.integration.config.get("tool_catalog"))
            allowed = [
                configured_tool(bound.integration.config, bound.scope, name)
                for name in bound.scope.get("tool_names", [])
            ]
            if not allowed:
                raise ValueError("MCP tool permission is empty")
            if self._capability == "mcp.tools/v1":
''', '''            if self._capability == "mcp.tools/v1":
                catalog = normalize_catalog(bound.integration.config.get("tool_catalog"))
                allowed = configured_tools(bound.integration.config, bound.scope)
                if not allowed:
                    raise ValueError("MCP tool permission is empty")
''')
put(p,t)

p = 'SKM/backend/src/skillmind/integrations/mcp_readback.py'
t = load(p, '3fd10821c018748bf9a010ebe312ddf01c809357')
t = once(t, '    if not parts:\n', '''    if "$ref" in schema:
        # 部分射影だけでは元文書の reference を解決できない。ここで不可能と断定せず、
        # 実応答は Source の全 outputSchema と exact read-back の両方で検証する。
        return True
    if not parts:
''')
put(p,t)

p = 'SKM/backend/src/skillmind/worker/executor.py'
t = load(p, '8a90517f913edf5ab8adcdad49b93c0a72d41f3c')
anchor='from skillmind.core.timing import '
lines=t.splitlines(keepends=True)
idx=next(i for i,line in enumerate(lines) if line.startswith(anchor))
lines.insert(idx, 'from skillmind.runs.realtime_buffer import BufferedRunRealtimePublisher\n')
t=''.join(lines)
t=once(t, '        deadline = loop.time() + context.limits.wall_timeout_seconds\n', '''        deadline = loop.time() + context.limits.wall_timeout_seconds
        realtime = (
            BufferedRunRealtimePublisher(self._realtime_publisher, timings)
            if self._realtime_publisher is not None else None
        )
''')
t=once(t, '''                    if self._realtime_publisher is not None:
                        with timings.measure("realtime_publish"):
                            await self._realtime_publisher.publish(event)
''', '''                    if realtime is not None:
                        realtime.enqueue(event)
''')
t=once(t, '''            finally:
                timings.emit(run_id=claimed.run_id, run_attempt_id=claimed.run_attempt_id)
''', '''            finally:
                try:
                    if realtime is not None:
                        await realtime.aclose(drain=not cancellation.is_set())
                finally:
                    timings.emit(run_id=claimed.run_id, run_attempt_id=claimed.run_attempt_id)
''')
put(p,t)

p='SKM/backend/src/skillmind/effects/catalog.py'
t=load(p, '7c662b5f0c24f332aeee95d0bfd3c827602c637d')
t=once(t, 'preauthorizable=False, validate=validate_mcp_proposal, requested_scope=mcp_call_scope,\n        staged_authorization=True,', 'preauthorizable=False, validate=validate_mcp_proposal, requested_scope=mcp_call_scope,\n        staged_authorization=True,\n        run_auto_approvable_providers=frozenset({"mcp"}),')
put(p,t)
p='SKM/backend/src/skillmind/effects/run_approval.py'
t=load(p, '2d140e1102c65efc62d178e2dc2feaf602f642d0')
t=once(t, '    return intent.actor_id if intent.auto_approve else None', '''    if capability_version == "mcp.call/v1" and not intent.auto_approve_mcp:
        return None
    return intent.actor_id if intent.auto_approve else None''')
put(p,t)
p='SKM/backend/src/skillmind/runs/creation_request.py'
t=load(p, '398877979ec257f0e428cdfbc4d718fcd731eb3e')
t=once(t, '    auto_approve_git: bool = False\n', '    auto_approve_git: bool = False\n    auto_approve_mcp: bool = False\n')
t=once(t, '        object.__setattr__(self, "input_json", deepcopy(self.input_json))', '''        if not isinstance(self.auto_approve_mcp, bool) or (
            self.auto_approve_mcp and not self.auto_approve
        ):
            raise ValueError("MCP approval requires Run automatic approval")
        object.__setattr__(self, "input_json", deepcopy(self.input_json))''')
t=once(t, '            "request_version": "v3"\n', '            "request_version": "v4"\n            if self.auto_approve_mcp\n            else "v3"\n')
t=once(t, '            **({"auto_approve_git": True} if self.auto_approve_git else {}),', '            **({"auto_approve_git": True} if self.auto_approve_git else {}),\n            **({"auto_approve_mcp": True} if self.auto_approve_mcp else {}),')
t=once(t, '''                and value.get("auto_approve_git") is True
            )
        ):
''', '''                and value.get("auto_approve_git") is True
            )
            or (
                set(value) in (
                    _REQUEST_FIELDS | {"auto_approve", "auto_approve_mcp"},
                    _REQUEST_FIELDS | {"auto_approve", "auto_approve_git", "auto_approve_mcp"},
                )
                and value.get("request_version") == "v4"
                and value.get("auto_approve") is True
                and value.get("auto_approve_mcp") is True
                and ("auto_approve_git" not in value or value["auto_approve_git"] is True)
            )
        ):
''')
t=once(t, '            auto_approve_git=value.get("auto_approve_git", False),', '            auto_approve_git=value.get("auto_approve_git", False),\n            auto_approve_mcp=value.get("auto_approve_mcp", False),')
put(p,t)

p='SKM/backend/src/skillmind/runs/service.py'
t=load(p,'84d1c027b8f08ee8fef7d41cd9b728039c953366')
t,n=re.subn(r'(?m)^(\s*)auto_approve_git: bool = False,$', lambda m:m[0]+'\n'+m[1]+'auto_approve_mcp: bool = False,',t)
assert n >= 2
print('SERVICE_SIGNATURES',n)
t,n=re.subn(r'(?m)^(\s*)auto_approve_git=auto_approve_git,$', lambda m:m[0]+'\n'+m[1]+'auto_approve_mcp=auto_approve_mcp,',t)
assert n >= 2
print('SERVICE_FORWARDING',n)
put(p,t)
p='SKM/backend/src/skillmind/api/routes/runs.py'
t=load(p,'1e1b10d1993f59333f9cc89bc9113f2a4107f5e4')
t=once(t, '    auto_approve_git: bool = Field(default=False, strict=True)', '    auto_approve_git: bool = Field(default=False, strict=True)\n    auto_approve_mcp: bool = Field(default=False, strict=True)')
t=once(t, '            raise ValueError("Git approval requires Run automatic approval")\n        return self', '            raise ValueError("Git approval requires Run automatic approval")\n        if self.auto_approve_mcp and not self.auto_approve:\n            raise ValueError("MCP approval requires Run automatic approval")\n        return self')
t,n=re.subn(r'(?m)^(\s*)auto_approve_git=([a-zA-Z_]+)\.auto_approve_git,$', lambda m:m[0]+'\n'+m[1]+'auto_approve_mcp='+m[2]+'.auto_approve_mcp,',t)
assert n >= 1
print('API_FORWARDING',n)
put(p,t)

p='SKM/contracts/runs/task-create/v1/request.schema.json'
v=json.loads(load(p,'76ecbc4922106981b9dfda97ab093bb0dc34ff7c'))
v['properties']['auto_approve_mcp']={'type':'boolean','default':False,'description':'Include authorized MCP tool effects in this Run-start consent. Requires auto_approve=true; exact binding, tool permissions, approval records and read-back remain required.'}
v['allOf'].append({'if':{'required':['auto_approve_mcp'],'properties':{'auto_approve_mcp':{'const':True}}},'then':{'required':['auto_approve'],'properties':{'auto_approve':{'const':True}}}})
put(p,json.dumps(v,ensure_ascii=False,indent=2)+'\n')

p='SKM/web/src/api/runs.ts'
t=load(p,'4c1936a92f4fcadc94c955fc1b86216176f5473f')
t=once(t,'  auto_approve_git?: boolean\n','  auto_approve_git?: boolean\n  auto_approve_mcp?: boolean\n')
for line in t.splitlines():
    if 'auto_approve' in line: print('WEB_RUN_FIELD',line.strip())
put(p,t)
p='SKM/web/src/lib/runSubmission.ts'
t=load(p,'3f89ef95cfe870bbf26e5c46aa9e48b32baa732d')
t=once(t, 'auto_approve_git: draft.autoApprove }', 'auto_approve_git: draft.autoApprove, auto_approve_mcp: draft.autoApprove }')
put(p,t)
for language in ('zh','ja','en'):
    p=f'SKM/web/src/lib/i18n/{language}.ts'
    t=load(p)
    for line in t.splitlines():
        if 'autoApprove' in line or ('Git' in line and ('承认' in line or '承認' in line or '批准' in line or 'approv' in line.lower())):
            print('CONSENT_UI',p,line.strip())
    # 後続の小さな確認済み patch で正確な label を更新する。

p='SKM/backend/tests/runs/test_run_start_approval.py'
t=load(p,'20d6d0ecb448001e58fad6326e37db9c8eeab351')
t=once(t, 'def freeze_consent(run, *, enabled=True, git=False):','def freeze_consent(run, *, enabled=True, git=False, mcp=False):')
t=once(t, '        auto_approve_git=git,', '        auto_approve_git=git,\n        auto_approve_mcp=mcp,')
t=once(t, '@pytest.mark.parametrize("provider", ["postgres", "git"])','@pytest.mark.parametrize("provider", ["postgres", "git", "mcp"])')
t=once(t,'freeze_consent(run, enabled=automatic, git=automatic and provider == "git")','freeze_consent(run, enabled=automatic, git=automatic and provider == "git", mcp=automatic and provider == "mcp")')
t=once(t,'capability_version="repository.write/v1" if provider == "git" else "database.write/v1",','capability_version="mcp.call/v1" if provider == "mcp" else "repository.write/v1" if provider == "git" else "database.write/v1",')
t=once(t,'operation="commit" if provider == "git" else "INSERT",','operation="call" if provider == "mcp" else "commit" if provider == "git" else "INSERT",')
t=once(t,'execution_features=ExecutionFeatures(database_writes=True, git_writes=True)','execution_features=ExecutionFeatures(database_writes=True, git_writes=True, mcp_tools=True)')
put(p,t)

p='docs/design/repository-effects.md'
t=load(p)
t=once(t,'逐次精确批准与显式只读回读；不继承 DB/文档/Git 自动批准','逐次精确批准或本 Run 明确的 MCP 启动同意，并执行显式只读回读；不从旧 DB/文档/Git 同意推导 MCP 权限')
a=t.index('手动启动页面提供默认勾选的')
b=t.index('\n\n平台在提案通过',a)
t=t[:a]+'''手动启动页使用同一个自动批准 checkbox，明确涵盖数据库、文档、Git 与 MCP 操作。新提交同时冻结 `auto_approve`、`auto_approve_git`、`auto_approve_mcp`；MCP 同意由 `TaskRunIntent` 的 v4 身份记录，API 省略时为 false，不回填在途 Run，不从旧同意推导新权限。未勾选时仍逐次人工批准；需要业务判断的问题仍由用户回答。所有额外同意均要求 `auto_approve=true`，幂等重放使用原请求全文。MCP 只允许已配置的 `mcp` Provider 与冻结工具范围，服务端 `readOnlyHint` 不是授权，SVN/Redmine 沿原规则。''' + t[b:]
put(p,t)
p='docs/operations/run-performance.md'
t=load(p)
t+='''\n## MCP 和实时通知的执行开销\n\nMCP catalog 和 Schema 的结构检查按完整 canonical 内容做有界进程内复用；真实调用参数和结果仍逐次验证，权限、凭据、业务响应不缓存。单工具 query 只解析目标工具，discovery 才投影全授权列表。相同 Schema 不再在每个子节点和每个工具上重复 meta-schema 校验。外部工具目录仍在真实调用前核对，不以缓存代替远端契约检查。\n\n每个 Attempt 的 TEXT_DELTA 通过独立有界队列配送：最多 32 条、每条 16,384 字符，正常结束最多等待 100ms，取消时直接清理。仅过载的即时文字可丢弃，完整消息、结果、批准和持久事件不进入该队列。`realtime_publish` 现在与引擎消费并行，不能再与 `engine_wait` 相加解释总耗时；原授权、事件顺序和终态保存不减项。此项减少显示通道的反压，不宣称消除模型思考或 Effect 续行开销。\n'''
put(p,t)

STATE.write_text(json.dumps(sorted(changed)),encoding='utf-8')
print('PATCH_PATHS',json.dumps(sorted(changed)))
