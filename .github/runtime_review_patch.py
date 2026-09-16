"""確認済みの差分だけを隔離 checkout へ適用し、検証後に Git tree を準備する。"""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

REPO = "ZhouRR/SkillMind"
BASE = "a9dbad6c1d47c1049787c0c4459fe4a781d0a6b4"
BASE_TREE = "429cac6a415552f8a735cdaef7d5d0746d4d64cc"
BRANCH = "refactor/runtime-stability-cleanup"
STATE = Path(os.environ["RUNNER_TEMP"]) / "runtime-review-paths.json"
PAYLOADS = {
    "SKM/backend/src/skillmind/core/pubsub.py": "21273007c79d54da5750ee99700df6a636cfe7bd",
    "SKM/backend/src/skillmind/skills/source_projection.py": "25865e3b319e12e94d5e42de22a18271953cd957",
    "SKM/backend/tests/core/test_pubsub.py": "bf800570cc0d4f9491d2f56b49418554a8964083",
    "SKM/backend/tests/skills/test_source_projection.py": "6249208d2241393f1d653b40fc7bc957c08b813a",
    "SKM/backend/tests/agent/test_continuation_lists.py": "f974105dd8bd7b0a0834f78438ae75a611c22310",
    "SKM/backend/tests/agent/test_database_observation_selection.py": "f710f0da17dc4157e13460c8edb13a007f925b21",
    "docs/design/skill-interpretation.md": "c3354f4006243ed9c534427135ab4919b1622005",
}
ORIGINALS = {
    "SKM/backend/src/skillmind/skills/candidate.py": "3dd9937a280f529de047e693de334190b83918be",
    "SKM/backend/src/skillmind/skills/direct_candidate.py": "e87fd36efec2e2bcdd37af84c016f45d8b5dc30d",
    "SKM/backend/src/skillmind/runs/realtime.py": "b06cca7b7a2f084d88ac430fbc992a188d3a181d",
    "SKM/backend/src/skillmind/skills/realtime.py": "e3c24b9deb1d4d454799b5c7e02e093709c9c5be",
    "SKM/backend/src/skillmind/agent/continuation_prompt.py": "41cc3c4f105fc88853e6ca95c1906e6089aeb47c",
    "SKM/backend/src/skillmind/agent/database_observations.py": "c825d3508b0dafa27ce63402fa3afc7e97abb13b",
    "SKM/backend/src/skillmind/agent/database_provider.py": "bf0e74d66e75bfe6b4bf59fe22f0b8980968f54b",
    "SKM/contracts/skills/interpreter/v2/candidate.schema.json": "41b1c8791c7f4c19c674e0a6786d81ad0b4dbb0d",
    "docs/design/skill-interpretation.md": "ccb10a3a55ad11c2f17b4a4d2a1d1268432798e0",
    "docs/design/skill-interpreter-stability.md": "68327ece6fa6880c373a3573f2e9d745e3c3a240",
    "docs/operations/backend-runtime-sync.md": "f27aa4fb38f1def5efa135128d73a37c2183a329",
    "docs/operations/deployment.md": "fb922560cfef9e61ede92f7243b04d44da2632c7",
}


def api(path, data=None):
    assert not path.startswith(("http", "/"))
    request = urllib.request.Request(
        "https://api.github.com/repos/" + REPO + "/" + path,
        data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def digest(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def read(path):
    return Path(path).read_text(encoding="utf-8")


changed = set()


def write(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")
    changed.add(path)


def replace_once(text, old, new):
    assert text.count(old) == 1, "Non-unique patch anchor: " + old[:70]
    return text.replace(old, new, 1)


def replace_region(text, start, end, replacement):
    assert text.count(start) == 1 and text.count(end) == 1
    left, right = text.index(start), text.index(end)
    assert left < right
    return text[:left] + replacement + text[right:]


def prepare():
    assert os.environ["GITHUB_REPOSITORY"] == REPO
    assert os.environ["GITHUB_REF"] == "refs/heads/" + BRANCH
    for path, sha in ORIGINALS.items():
        assert digest(Path(path).read_bytes()) == sha, "Changed baseline: " + path
    docs_before = list(Path("docs").rglob("*.md"))
    before_size = sum(p.stat().st_size for p in docs_before)
    for path, sha in PAYLOADS.items():
        assert path in ORIGINALS or not Path(path).exists()
        blob = api("git/blobs/" + sha)
        raw = base64.b64decode(blob["content"])
        assert digest(raw) == sha
        write(path, raw.decode("utf-8"))

    path = "SKM/backend/src/skillmind/skills/candidate.py"
    text = read(path)
    text = replace_once(text,
        "from skillmind.skills.source_documents import validate_source_documents, validate_source_location",
        "from skillmind.skills.source_projection import (\n"
        "    SourceLocations,\n    model_request as model_request,\n"
        "    omit_optional_nulls as _omit_optional_nulls,\n    source_index as source_index,\n)")
    text = replace_region(text, "def source_index(", "def compile_candidate(", "")
    text = replace_region(text, "    sources = source_index(request)\n", "    value = _omit_optional_nulls(",
                          "    location = SourceLocations(source_index(request)).resolve\n\n")
    text = text.replace('.read_text()', '.read_text(encoding="utf-8")')
    write(path, text)

    path = "SKM/backend/src/skillmind/skills/direct_candidate.py"
    text = read(path)
    text = replace_once(text, "from skillmind.skills.candidate import _omit_optional_nulls, source_index\n", "")
    text = replace_once(text, "from skillmind.skills.source_documents import validate_source_location",
        "from skillmind.skills.source_projection import (\n"
        "    SourceLocations,\n    omit_optional_nulls as _omit_optional_nulls,\n    source_index,\n)")
    text = replace_region(text, "    sources = source_index(request)\n", "    value = _omit_optional_nulls(",
                          "    location = SourceLocations(source_index(request)).resolve\n\n")
    text = replace_once(text, 'location(resource.pop("source_ref"))',
                        'location(resource.pop("source_ref"), f"/resource_requirements/{i}/source_ref")')
    text = replace_once(text, 'location(d.get("source_ref"))',
                        'location(d.get("source_ref"), f"/diagnostics/{i}/source_ref")')
    text = replace_once(text, 'for d in value["diagnostics"]', 'for i, d in enumerate(value["diagnostics"])')
    text = replace_once(text, 'location(value["input_source_ref"])',
                        'location(value["input_source_ref"], "/input_source_ref")')
    text = text.replace('.read_text()', '.read_text(encoding="utf-8")')
    write(path, text)

    path = "SKM/backend/src/skillmind/skills/model_interpreter.py"
    text = replace_once(read(path),
        "from skillmind.skills.candidate import CANDIDATE_SCHEMA_ID, model_request",
        "from skillmind.skills.candidate import CANDIDATE_SCHEMA_ID\n"
        "from skillmind.skills.source_projection import model_request")
    write(path, text)

    path = "SKM/contracts/skills/interpreter/v2/candidate.schema.json"
    value = json.loads(read(path))
    properties = value["properties"]["resource_requirements"]["items"]["properties"]
    capabilities = properties["capabilities"]["anyOf"][0]
    assert capabilities["type"] == "array"
    properties["capabilities"] = {**capabilities, "minItems": 1}
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    path = "SKM/backend/src/skillmind/runs/realtime.py"
    text = read(path).replace("import json\n", "").replace("from redis.exceptions import RedisError\n", "")
    text = replace_region(text, "class RedisPublisher(Protocol):", "class RunRealtimePublisher(Protocol):", "")
    text = replace_once(text, "from skillmind.agent.domain import AgentEvent, AgentEventType",
        "from skillmind.agent.domain import AgentEvent, AgentEventType\n"
        "from skillmind.core.pubsub import BestEffortPublisher, RedisPublisher as RedisPublisher")
    text = replace_once(text, "        self._redis = redis", "        self._publisher = BestEffortPublisher(redis)")
    text = replace_region(text, "        try:\n", "\n\ndef run_realtime_channel", 
        "        await self._publisher.publish(\n"
        "            run_realtime_channel(event.run_id), realtime_event_data(event)\n        )")
    write(path, text)

    path = "SKM/backend/src/skillmind/skills/realtime.py"
    text = read(path).replace("import json\n", "").replace("from redis.exceptions import RedisError\n", "")
    text = replace_once(text, "from skillmind.runs.realtime import RedisPublisher",
                        "from skillmind.core.pubsub import BestEffortPublisher, RedisPublisher")
    text = replace_once(text, "        self._redis = redis", "        self._publisher = BestEffortPublisher(redis)")
    start = text.index("        try:\n")
    text = text[:start] + "        await self._publisher.publish(interpret_channel(execution_key), message)\n"
    write(path, text)

    path = "SKM/backend/src/skillmind/agent/continuation_prompt.py"
    text = replace_once(read(path), "            prior = set(old_hashes[key])\n            if not prior.issubset(hashes[key]):",
                        "            prior = old_hashes[key]\n            if hashes[key][:len(prior)] != prior:")
    text = replace_once(text,
        "                # 旧 checkpoint が要約し直した配列は、全 Skill の再投入でなく明示置換する。",
        "                # 並べ替え・削除・途中挿入は置換。完全な prefix 一致だけを追加とする。")
    text = replace_once(text, "                delta[key] = [item for item in value if _hash(item) not in prior]",
                        "                delta[key] = value[len(prior):]")
    write(path, text)

    path = "SKM/backend/src/skillmind/agent/database_observations.py"
    text = replace_once(read(path), "from sqlalchemy import select, true", "from sqlalchemy import func, select, true")
    assert text.count('ToolCall.result_json["table_schema"].is_not(None)') == 2
    text = text.replace('ToolCall.result_json["table_schema"].is_not(None)',
                        'func.jsonb_typeof(ToolCall.result_json["table_schema"]) == "object"')
    text = replace_once(text, '.order_by(Evidence.created_at.desc())',
                        '.order_by(Evidence.created_at.desc(), Evidence.id.desc())')
    text = replace_once(text, '        async with self._sessions() as session:\n            names = await session.scalars(\n                select(ToolCall.result_json["table"].as_string())',
        '        table_name = ToolCall.result_json["table"].as_string()\n'
        '        async with self._sessions() as session:\n            names = await session.scalars(\n                select(table_name)')
    text = replace_once(text, '                .order_by(ToolCall.created_at.desc())\n                .limit(100)',
        '                .group_by(table_name)\n'
        '                .order_by(func.max(ToolCall.created_at).desc(), table_name)\n                .limit(20)')
    text = replace_once(text, '            return list(dict.fromkeys(name for name in names if isinstance(name, str)))[:20]',
                        '            return [name for name in names if isinstance(name, str)]')
    write(path, text)

    path = "SKM/backend/src/skillmind/agent/database_provider.py"
    text = read(path)
    old = '''        current, current_password = await self._bound(context)
        if current != bound or current_password != password:
            raise ToolProviderError(
                "unavailable", "Database binding changed during read", retryable=False
            )
        table_schema = None'''
    text = replace_once(text, old, '        await self._same_bound(context, bound, password)\n        table_schema = None')
    old = '''        if not runtime_policy(claimed.task_snapshot_json) or claimed.run_segment_id is None:
            return []
        try:
            frozen, before = await self._observations.segment_index('''
    new = '''        if not runtime_policy(claimed.task_snapshot_json) or claimed.run_segment_id is None:
            return []
        schema_tools = [tool for tool in tools if tool.capability == "database.describe/v1"]
        if not schema_tools:
            return []
        try:
            frozen, before = await self._observations.segment_index('''
    text = replace_once(text, old, new)
    text = replace_once(text, '            for tool in tools:\n                if tool.capability != "database.describe/v1":\n                    continue',
                        '            for tool in schema_tools:')
    write(path, text)

    path = "docs/operations/deployment.md"
    text = read(path)
    text = replace_once(text, '| api-web / worker | 等 API/Web health，再启动 Worker/Maintenance |',
        '| backend / runtime-check / web | 同批重建 API/Worker/Maintenance，核对镜像与配置后恢复 Web |')
    text = replace_once(text, '再初始化基建、迁移并启动 API/Web/Worker/Maintenance',
                        '再初始化基建、迁移，同批启动 API/Worker/Maintenance，核对一致性后恢复 Web')
    text = replace_region(text, "## 迁移与回退审查\n", "## 会话协议切换检查\n", '''## 同批更新与检查

API、执行 Worker 和维护 Worker 共用 `skills/service_wiring.py`；维护侧不加载解释器。`make deploy` 使用同批镜像与配置强制重建全部后端，等待健康检查、执行一致性核对后恢复 Web。此流程是协调重启，不是零停机；不删除数据卷或 Codex 登录数据，也不重跑未知业务。

独立诊断使用 `make runtime-check`，检查所有后端副本的实际 image ID、解释身份、能力目录、队列和功能开关。诊断不调用模型、不创建 Run、不查询业务数据；解释器一致地未配置时仅警告，不把可选解释器变成确定性导入的新前提。

独立检查失败只返回非零，不自动停止或修复容器。部署中的检查失败会停止后续步骤，但已经启动的后端可能继续运行；失败不是全局停写证明。相同 fingerprint 只证明当前容器配置一致，不证明登录、外部网络或业务结果正确。

需要查看脱敏配置时运行 `docker compose --env-file .env exec -T api python -m skillmind.ops.runtime_identity`，对 Worker 将服务名改为 `worker`。它是短命诊断进程，不是实际模型进程的状态探针。

## 迁移与回退审查

只审查本次真实涉及的数据库和公开协议变更；精确 upgrade/downgrade 条件以 [migration 实现](../../SKM/backend/migrations/versions/)为准，不在发布文档复制逐版本目录。当前运行策略的诊断、结构工具和报告消费者应同批更新。

变更前明确恢复点，回退按 [备份恢复](backup-recovery.md#应用版本回退)处理。不能 stamp、删除审计/快照、清空队列或重放 UNKNOWN 来绕过迁移失败。单纯文档、显示或局部代码优化不额外引入业务批准、重新导入或历史格式转换流程。

''')
    write(path, text)

    deleted = ["docs/design/skill-interpreter-stability.md", "docs/operations/backend-runtime-sync.md"]
    for path in deleted:
        Path(path).unlink()
        changed.add(path)
    docs = list(Path("docs").rglob("*.md")) + [Path("SKM/README.md"), Path("SKM/AGENTS.md"), Path("README.md")]
    for path in docs:
        if not path.is_file() or path.name == "SKILL.md":
            continue
        old = path.read_text(encoding="utf-8")
        new = old.replace("skill-interpreter-stability.md", "skill-interpretation.md")
        new = new.replace("backend-runtime-sync.md", "deployment.md")
        new = new.replace("skill-interpretation.md#旧-capabilityblueprint-兼容", "skill-interpretation.md#过程重表达")
        for anchor in ["正常调用保持不变", "同步更新", "独立检查", "历史请求与回归"]:
            new = new.replace("deployment.md#" + anchor, "deployment.md#同批更新与检查")
        if new != old:
            write(str(path), new)
    for path in changed:
        if path.endswith(".py"):
            ast.parse(read(path), filename=path)
    docs_after = list(Path("docs").rglob("*.md"))
    print("DOCS_BEFORE", len(docs_before), before_size)
    print("DOCS_AFTER", len(docs_after), sum(p.stat().st_size for p in docs_after))
    STATE.write_text(json.dumps(sorted(changed)), encoding="utf-8")
    print("PATCH_PATHS", json.dumps(sorted(changed)))


def publish():
    assert os.environ["GITHUB_REPOSITORY"] == REPO
    assert os.environ["GITHUB_REF"] == "refs/heads/" + BRANCH
    assert api("git/ref/heads/" + BRANCH)["object"]["sha"] == os.environ["GITHUB_SHA"]
    paths = set(json.loads(STATE.read_text())) | {"docs/index.html"}
    current = subprocess.check_output(["git", "diff", "--name-only"], text=True).splitlines()
    assert set(current) <= paths, "Unexpected modified files"
    entries = []
    for path in sorted(paths):
        assert path.startswith(("SKM/backend/", "SKM/contracts/", "docs/")) or path in {"README.md", "SKM/README.md", "SKM/AGENTS.md"}
        if Path(path).exists():
            raw = Path(path).read_bytes()
            entries.append({"path": path, "mode": "100644", "type": "blob", "content": raw.decode("utf-8")})
            print("VERIFIED", path, digest(raw))
        else:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    tree = api("git/trees", {"base_tree": BASE_TREE, "tree": entries})
    full = api("git/trees/" + tree["sha"] + "?recursive=1")
    assert full["truncated"] is False
    indexed = {item["path"]: item for item in full["tree"] if item["type"] == "blob"}
    assert ".github/runtime_review_patch.py" not in indexed
    assert ".github/workflows/runtime-review.yml" not in indexed
    for path in paths:
        if Path(path).is_file():
            assert indexed[path]["sha"] == digest(Path(path).read_bytes())
        else:
            assert path not in indexed
    print("PREPARED_TREE=" + tree["sha"])
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write("Validated runtime/document patch. PREPARED_TREE=" + tree["sha"] + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--publish"]:
        publish()
    elif not sys.argv[1:]:
        prepare()
    else:
        raise SystemExit("Unexpected arguments")
