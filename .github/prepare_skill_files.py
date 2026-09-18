"""確認済みの差分を適用し、同一 byte で回帰する。一時ファイルは最終 tree に残さない。"""
from pathlib import Path
import json
import os
import subprocess
import sys
import hashlib

assert os.environ['GITHUB_REPOSITORY'] == 'ZhouRR/SkillMind'
assert os.environ['GITHUB_REF'] == 'refs/heads/perf/frozen-skill-files'
S = Path('SKM/backend/src/skillmind')
changed = {'SKM/backend/src/skillmind/agent/skill_files.py', 'SKM/backend/tests/agent/test_skill_files.py'}


def edit(path, old, new):
    """一意な確認済み anchor だけを変更する。"""
    text = path.read_text(encoding='utf-8')
    assert text.count(old) == 1, (str(path), old[:60], text.count(old))
    path.write_text(text.replace(old, new, 1), encoding='utf-8')
    changed.add(str(path))


p = S/'agent/workspace_materializer.py'
edit(p, '物化対象は\n二路:', '資源の物化対象は\n二路:')
edit(p, 'file は manifest.skipped に必ず残す (「読めない」を「存在しない」と誤認させないため)。', 'file は manifest.skipped に必ず残す (「読めない」を「存在しない」と誤認させないため)。\n凍結 Skill text の副本は同じ封印世代へ任意で追加する。全体が既存予算に収まらない場合は\n副本を作らず、既存の完全原文経路を維持する。実在を確認した path だけを Brief へ渡す。')
edit(p, 'from skillmind.core.hashing import sha256_hex', 'from skillmind.agent.skill_files import frozen_file_contents, reused_skill_files\nfrom skillmind.core.hashing import sha256_hex')
edit(p, '    resources: tuple[MaterializedResource, ...]\n', '    resources: tuple[MaterializedResource, ...]\n    skill_files: tuple[InputFileSeal, ...] = ()\n')
edit(p, '        document_snapshots: Sequence[DocumentSnapshot] = (),\n', '        document_snapshots: Sequence[DocumentSnapshot] = (),\n        skill_documents: Sequence[Mapping[str, str]] = (),\n')
edit(p, '        snapshots = _validated_document_snapshots(blueprint, project_id, document_snapshots)', '        skill_contents = frozen_file_contents(claimed_run, skill_documents)\n        expected_skill_files = tuple(seal for seal, _ in skill_contents)\n        snapshots = _validated_document_snapshots(blueprint, project_id, document_snapshots)')
edit(p, '''            resources = await asyncio.to_thread(
                self._reuse, verified, project_id, run_id, snapshots, bindings,
                repository_metadata, deferred_ids,
            )
            return PreparedInput(verified, resources)''', '''            skill_files = reused_skill_files(verified, expected_skill_files)
            resources = await asyncio.to_thread(
                self._reuse, verified, project_id, run_id, snapshots, bindings,
                repository_metadata, deferred_ids, skill_files,
            )
            return PreparedInput(verified, resources, skill_files)''')
edit(p, '        sealed = tuple(sorted(files, key=lambda item: item.path))', '''        # 原文は Brief に完全に残る。任意の副本で既存 input の容量を超過させない。
        if skill_contents:
            try:
                self._ensure_total_budget((*files, *expected_skill_files))
            except MaterializationError:
                # 予算の事前検査だけを降級する。I/O・封印・取消の失敗は隠さない。
                expected_skill_files = ()
            else:
                prefix = input_relative(candidate)
                await asyncio.to_thread(
                    self._write_tree, candidate.root,
                    [_AcceptedFile(f"{prefix}/{seal.path}", data, seal.checksum)
                     for seal, data in skill_contents],
                )
                files.extend(expected_skill_files)
        sealed = tuple(sorted(files, key=lambda item: item.path))''')
edit(p, '        return PreparedInput(replace(candidate, input_files=sealed), tuple(materialized))', '''        return PreparedInput(
            replace(candidate, input_files=sealed), tuple(materialized), expected_skill_files
        )''')
edit(p, '        deferred_ids: frozenset[UUID],\n    ) -> tuple[MaterializedResource, ...]:', '        deferred_ids: frozenset[UUID],\n        skill_files: tuple[InputFileSeal, ...] = (),\n    ) -> tuple[MaterializedResource, ...]:')
edit(p, '        expected_roots = set(bindings)', '        expected_roots = set(bindings)\n        if skill_files:\n            expected_roots.add(_PLATFORM_DIRECTORY)')

p = S/'agent/context_builder.py'
edit(p, 'from skillmind.agent.runtime_policy import uses_modern_runtime', 'from skillmind.agent.runtime_policy import uses_modern_runtime\nfrom skillmind.agent.skill_files import SKILL_FILE_CAPABILITIES')
edit(p, 'from skillmind.runs.domain import ClaimedRun', 'from skillmind.runs.domain import ClaimedRun\nfrom skillmind.runs.input_snapshot import InputFileSeal')
edit(p, '        materialized: tuple[MaterializedResource, ...] = ()', '        materialized: tuple[MaterializedResource, ...] = ()\n        skill_files: tuple[InputFileSeal, ...] = ()')
edit(p, '                document_snapshots=document_snapshots,\n            )', '''                document_snapshots=document_snapshots,
                skill_documents=(manifest.get("source_documents", ())
                    if any(tool.capability in SKILL_FILE_CAPABILITIES for tool in tools) else ()),
            )''')
edit(p, '            materialized = prepared_input.resources', '            materialized = prepared_input.resources\n            skill_files = prepared_input.skill_files')
edit(p, '            materialized=materialized,\n        )', '            materialized=materialized,\n            skill_files=skill_files,\n        )')

p = S/'agent/task_brief.py'
edit(p, 'from skillmind.agent.runtime_policy import runtime_policy', 'from skillmind.agent.runtime_policy import runtime_policy\nfrom skillmind.agent.skill_files import append_skill_file_guidance, skill_file_locations')
edit(p, 'from skillmind.skills.source_documents import validate_source_documents', 'from skillmind.skills.source_documents import validate_source_documents\nfrom skillmind.runs.input_snapshot import InputFileSeal')
edit(p, '    materialized: Sequence[MaterializedResource] = (),\n    database_observations:', '    materialized: Sequence[MaterializedResource] = (),\n    skill_files: Sequence[InputFileSeal] = (),\n    database_observations:')
edit(p, '        _runtime_metadata(direct_brief, task_snapshot, model)', '''        if skill_files:
            direct_brief["skill_files"] = skill_file_locations(
                direct_brief["source_documents"], skill_files
            )
        _runtime_metadata(direct_brief, task_snapshot, model)''')
edit(p, '    if "document_prerequisites" in blueprint_task:', '''    if skill_files:
        brief["skill_files"] = skill_file_locations(brief.get("source_documents", ()), skill_files)
    if "document_prerequisites" in blueprint_task:''')
edit(p, '        _append_materialization(sections, brief["resources"], brief["allowed_tools"])', '        append_skill_file_guidance(sections, brief)\n        _append_materialization(sections, brief["resources"], brief["allowed_tools"])')
t = p.read_text(); needle = '    return _finish_task_prompt(sections, brief, input_json, output_schema)'
assert t.count(needle) == 2
pos = t.rfind(needle); p.write_text(t[:pos] + '    append_skill_file_guidance(sections, brief)\n' + t[pos:])

field = {'type':'array', 'minItems':1, 'maxItems':1000,
 'description':'Verified read-only locations of the frozen Skill text files; absent when not materialized. Does not grant tool or script execution permission.',
 'items':{'type':'object','additionalProperties':False,'required':['source_path','path','sha256'],
 'properties':{'source_path':{'type':'string','minLength':1,'maxLength':1024},
 'path':{'type':'string','pattern':r'^input/\.skillmind/skill/[^\\]+$','maxLength':1050},
 'sha256':{'$ref':'#/$defs/sha256'}}}}
for version in ('v1','v2'):
    p=Path(f'SKM/contracts/agent-task-brief/{version}.schema.json')
    value=json.loads(p.read_text()); assert 'skill_files' not in value['properties']
    value['properties']['skill_files']=field
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n'); changed.add(str(p))
p=Path('SKM/contracts/examples/agent-task-brief.v2.json');value=json.loads(p.read_text())
value['allowed_tools']=[{'capability':'workspace.read/v1','provider':'workspace','read_only':True}]
value['skill_files']=[{'source_path':s['path'],'path':'input/.skillmind/skill/'+s['path'],'sha256':s['sha256']}
                     for s in sorted(value['source_documents'],key=lambda s:s['path'])]
p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n'); changed.add(str(p))

p=Path('SKM/backend/tests/agent/test_skill_files.py')
edit(p, '''async def test_skill_files_count_towards_existing_input_budgets(tmp_path, limits):
    """元からある単根・全根の制限を使い、未完成のパスは公開しない。"""
    claim, store, _, _, builder = _case(tmp_path, **limits)
    with pytest.raises(MaterializationError):
        await builder.build(claim, sequence_start=1)
    assert store.complete_calls == 0
    assert store.record.status is InputSnapshotStatus.PREPARING
''', '''async def test_optional_skill_files_never_exceed_input_budgets_or_block_run(tmp_path, limits):
    """容量不足は全副本を省略して完全原文を維持する。途中の file は公開しない。"""
    claim, store, _, _, builder = _case(tmp_path, **limits)
    context = await builder.build(claim, sequence_start=1)
    assert context.task_brief["source_documents"] == _sources()
    assert "skill_files" not in context.task_brief
    assert not (context.workspace.input_dir / ".skillmind").exists()
    assert store.record.status is InputSnapshotStatus.READY
    assert store.record.files == ()
    resumed = await builder.build(claim, sequence_start=10)
    assert resumed.task_brief == context.task_brief
    assert store.complete_calls == 1
''')
with p.open('a',encoding='utf-8') as f:
    f.write('''\n\nasync def test_valid_import_larger_than_workspace_file_limit_keeps_original_prompt(tmp_path):
    """CLI で許可される大きな原文を、新しい副本の上限で実行不能にしない。"""
    text = "x" * 1_048_577
    docs = [{"path": "references/large.txt", "content": text,
             "sha256": "sha256:" + sha256_hex(text)}]
    claim, store, _, _, builder = _case(tmp_path, documents=docs)
    context = await builder.build(claim, sequence_start=1)
    assert "skill_files" not in context.task_brief
    assert context.task_brief["source_documents"] == docs
    assert store.record.status is InputSnapshotStatus.READY
    assert store.record.files == ()
''')

p=Path('docs/design/resource-snapshots.md')
edit(p, 'input/（只读）\n├── documents/', 'input/（只读）\n├── .skillmind/skill/         冻结 Skill 文本文件（保留包内路径）\n├── documents/')
edit(p, 'workspace.write 只写 workspace/output；', '''冻结 Skill 文本不由模型复制：ContextBuilder 在原 Manifest 验证及工具授权后，将已有 `source_documents` 交给同一输入准备器。只有本 Run 已有 workspace read/search 或 JSON Schema 校验能力时才物化；不新增能力、用户参数、资源绑定或外部请求。冻结文本（包括 Schema、参考资料及脚本文本）保留原 UTF-8 字节、包内路径与 hash，写到保留的 `input/.skillmind/skill/<source_path>`。不重排 JSON、转换换行、去掉说明、执行脚本，未冻结的二进制文件也不冒充已提供。

文件与普通输入共享世代、现有单文件/单根/总量上限和 PREPARING → READY 事务。若整个 Skill 副本无法放入现有容量，全部不物化且不发布路径，继续原来的完整原文方式，不截断或让原任务因辅助副本超限而失败；I/O、取消和完整性失败仍正常传播。Brief 的可选 `skill_files` 只在完成后列出原路径、实际逻辑路径和 hash，并参与 Brief checksum。校验工具直接使用该路径作为 `schema_path`，无需模型重新输出 Schema；路径不是新权限，外部引用处理不变。重试/续行核对原回执、完整树和原文哈希后复用，不重复写文件。既有 READY 未含 Skill 文件时不改写，继续原文方式且不发布不存在的位置；缺失或被篡改的已封存文件仍按原完整性规则拒绝。无物化器或无相应能力时也不猜路径。原 Skill 的业务执行、审批和成果保存规则不变。

workspace.write 只写 workspace/output；''')
p=Path('docs/planning/roadmap.md')
edit(p, '任务内 JSON Schema 校验已验。MCP 按整体访问权限', '任务内 JSON Schema 校验已验；冻结 Skill 文本已接入原输入封存与工具路径交付，免去模型复制 Schema，真实耗时收益待复测。MCP 按整体访问权限')
python_paths=sorted(p for p in changed if p.endswith('.py'))
subprocess.run([sys.executable,'-m','ruff','check','--config','SKM/backend/pyproject.toml','--no-cache','--select','I,F','--fix',*python_paths],check=True)
subprocess.run([sys.executable,'-m','ruff','format','--config','SKM/backend/pyproject.toml','--no-cache','SKM/backend/src/skillmind/agent/skill_files.py','SKM/backend/tests/agent/test_skill_files.py'],check=True)
subprocess.run([sys.executable,'-m','ruff','check','--config','SKM/backend/pyproject.toml','--no-cache',*python_paths],check=True)
state=Path(os.environ['RUNNER_TEMP'])
(state/'skill-files-paths.json').write_text(json.dumps(sorted(changed)))
fingerprint={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sorted(changed)}
(state/'source-fingerprint.json').write_text(json.dumps(fingerprint,sort_keys=True))
print('Prepared exact changed source:',len(changed),'files')
