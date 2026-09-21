"""確認済み補修を同じ基準へ適用し、境界回帰も追加する一時処理。"""
from pathlib import Path
import ast

root = Path('.')

def edit(path, old, new):
    p = root / path
    text = p.read_text()
    assert text.count(old) == 1, (path, old[:70], text.count(old))
    p.write_text(text.replace(old, new, 1))

edit('SKM/backend/src/skillmind/agent/warm_codex.py', '''        request = asyncio.create_task(
            asyncio.to_thread(
                client.request,
                "thread/unsubscribe",
                {"threadId": session_id},
                response_model=ThreadUnsubscribeResponse,
            )
        )''', '''        def unsubscribe() -> ThreadUnsubscribeResponse:
            """SDK の generic keyword を同じ同期 call 内で束縛する。"""
            return client.request(
                "thread/unsubscribe", {"threadId": session_id},
                response_model=ThreadUnsubscribeResponse,
            )

        request = asyncio.create_task(asyncio.to_thread(unsubscribe))''')

# 新しい Provider を将来追加しても、未審査のまま序列を許可しない。
p = root / 'SKM/backend/src/skillmind/agent/tool_catalog.py'
s = p.read_text().replace('sequence_safe: bool = True', 'sequence_safe: bool = False')
lines = s.splitlines(keepends=True)
calls = (n for n in ast.walk(ast.parse(s)) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name) and n.func.id == '_tool_definition')
for node in sorted(calls, key=lambda n: n.lineno, reverse=True):
    if not {'sequence_safe', 'defer_execution'} & {k.arg for k in node.keywords}:
        keyword_line = lines[node.lineno]
        indent = keyword_line[:len(keyword_line)-len(keyword_line.lstrip())]
        lines.insert(node.lineno, indent + 'sequence_safe=True,\n')
p.write_text(''.join(lines))

edit('SKM/backend/src/skillmind/agent/tool_gateway.py', '''    async def register_authorized(
''', '''    def validate_request(self, tool_name: str, arguments: Mapping[str, Any]) -> None:
        """実行権を発行せず、凍結済み Tool の静的境界だけを検査する。"""
        self._policy.authorize(tool_name, arguments)

    async def register_authorized(
''')
edit('SKM/backend/src/skillmind/agent/tool_gateway.py',
     'self._coordinator._policy.authorize(name, arguments)',
     'self._coordinator.validate_request(name, arguments)')
edit('SKM/backend/src/skillmind/agent/tool_gateway.py', '''        if (parent.run_id != self._context.run_id
            or parent.run_attempt_id != self._context.run_attempt_id
            or parent.agent_session_id is None or parent.tool_call_id not in self._dispatched):''', '''        if (parent.run_id != self._context.run_id
            or parent.run_attempt_id != self._context.run_attempt_id
            or parent.project_id != self._context.project_id
            or parent.user_id != self._context.user_id
            or parent.tool.capability != "tool.sequence/v1"
            or parent.agent_session_id is None or parent.tool_call_id not in self._dispatched):''')
edit('SKM/backend/src/skillmind/agent/tool_gateway.py', '''        except PermissionError:
            return {"status": "error", "code": "scope_denied", "message": "Sequence authority or tool budget is unavailable", "retryable": False}''', '''        except PermissionError:
            await self._coordinator.register_denied(
                name, args, f"sequence:{parent.tool_call_id}:{position}",
                str(parent.agent_session_id), "Sequence authority or tool budget is unavailable",
            )
            return {"status": "error", "code": "scope_denied", "message": "Sequence authority or tool budget is unavailable", "retryable": False}''')
edit('SKM/backend/src/skillmind/runs/repository.py', '''                or proposal.run_id != run_id or proposal.run_attempt_id != previous.run_attempt_id
''', '''                or proposal.run_id != run_id or proposal.project_id != run.project_id
                or proposal.run_attempt_id != previous.run_attempt_id
                or proposal.run_segment_id != previous.run_segment_id
                or effect.proposal_id != proposal.id or effect.approval_id != approval.id
''')
p = root / 'SKM/backend/tests/runs/test_warm_claim.py'
s = p.read_text()
s = s.replace('''        id=uuid4(),
        run_id=run.id,
        run_attempt_id=previous.run_attempt_id,''', '''        id=uuid4(),
        run_id=run.id,
        project_id=run.project_id,
        run_segment_id=previous.run_segment_id,
        run_attempt_id=previous.run_attempt_id,''')
s = s.replace('''        "other_checksum",
''', '''        "other_checksum",
        "other_project",
        "other_segment",
        "other_effect_proposal",
        "other_effect_approval",
''')
s = s.replace('''    elif mutation == "other_checksum":''', '''    elif mutation == "other_project":
        proposal.project_id = uuid4()
    elif mutation == "other_segment":
        proposal.run_segment_id = uuid4()
    elif mutation == "other_effect_proposal":
        effect.proposal_id = uuid4()
    elif mutation == "other_effect_approval":
        effect.approval_id = uuid4()
    elif mutation == "other_checksum":''')
p.write_text(s)

p = root / 'SKM/backend/src/skillmind/agent/codex_engine.py'
s = p.read_text().replace('from skillmind.core.timing import observe_phase',
                         'from skillmind.core.timing import observe_phase, safe_observation')
s = s.replace('''            terminal_confirmed = False
''', '''            safe_observation(
                "run.performance.sdk_reuse", run_id=context.run_id,
                run_attempt_id=context.run_attempt_id,
                status="new" if needs_start else "reused",
            )
            terminal_confirmed = False
''')
old = '''                if parent is None:
                    thread = await asyncio.to_thread(client.thread_start, options)
                elif fork:
                    thread = await asyncio.to_thread(client.thread_fork, parent.session_id, options)
                else:
                    thread = await asyncio.to_thread(
                        client.thread_resume, parent.session_id, {**options, "excludeTurns": True}
                    )'''
new = '''                with observe_phase(
                    "run.performance.sdk_thread", run_id=context.run_id,
                    run_attempt_id=context.run_attempt_id,
                    status="start" if parent is None else "fork" if fork else "resume",
                ):
''' + '\n'.join('    ' + line for line in old.splitlines())
assert s.count(old) == 1
s = s.replace(old, new)
old = '''                turn = await asyncio.to_thread(
                    client.turn_start,
                    session_id,
                    prompt,
                    {
                        "model": context.model,
                        "effort": self._configuration.effort,
                        "outputSchema": output.schema,
                    },
                )'''
new = '''                with observe_phase(
                    "run.performance.sdk_turn_start", run_id=context.run_id,
                    run_attempt_id=context.run_attempt_id,
                ):
''' + '\n'.join('    ' + line for line in old.splitlines())
assert s.count(old) == 1
p.write_text(s.replace(old, new))

edit('SKM/backend/src/skillmind/agent/tool_sequence.py', 'from copy import deepcopy\n',
     'from copy import deepcopy\nimport re\n')
edit('SKM/backend/src/skillmind/agent/tool_sequence.py', '''        pointer = check["pointer"]
        try:''', '''        pointer = check["pointer"]
        if not isinstance(pointer, str) or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", pointer) is None:
            return False
        try:''')
p = root / 'SKM/backend/tests/agent/test_tool_sequence.py'
s = p.read_text().replace('from dataclasses import replace\n',
                         'from dataclasses import replace\nfrom pathlib import Path\n')
s += '''


def test_new_tool_registrations_do_not_implicitly_allow_sequences():
    """新しい Provider は明示登録されるまで序列へ入れない。"""
    from skillmind.agent.contract_store import ContractStore
    from skillmind.agent.tool_catalog import _tool_definition

    definition = _tool_definition(
        ContractStore(Path(__file__).resolve().parents[3] / "contracts"),
        capability="issue.read/v1", description="fixture", providers={"csv": CsvIssueProvider()},
    )
    assert definition.sequence_safe is False


async def test_revoked_dispatch_stops_before_next_provider_call(tmp_path):
    """静的検査後の撤権は各子の dispatch 検査で止める。"""
    context, runtime, writer, provider = sequence_runtime(tmp_path)
    original = writer.verify_dispatch

    async def verify(lease):
        """第一子完了後の実権限失効を注入する。"""
        await original(lease)
        if provider.calls:
            raise PermissionError("revoked")

    writer.verify_dispatch = verify
    result = await invoke(context, runtime, [step(), step(), step()])
    assert provider.calls == 1
    assert result["outcome"] == "STOPPED" and result["not_run"] == [2]


async def test_unconfirmed_outer_result_never_replays_children(tmp_path):
    """外側の成功 commit 応答未知で同一要求が再来しても子を再送しない。"""
    context, runtime, writer, provider = sequence_runtime(tmp_path)
    original = writer.complete

    async def complete(lease, **kwargs):
        """子は確定し、外側だけ未確認のままにする。"""
        if lease.invocation.tool.capability == "tool.sequence/v1":
            raise OSError("unconfirmed")
        return await original(lease, **kwargs)

    writer.complete = complete
    session = str(uuid4())
    first = await invoke(context, runtime, [step(), step()], session=session)
    second = await invoke(context, runtime, [step(), step()], session=session)
    assert first["status"] == second["status"] == "error"
    assert provider.calls == 2 and len(writer.completed) == 2


async def test_identical_steps_are_distinct_operations_not_deduplicated(tmp_path):
    """同じ引数でも異なる position は別の原操作として記録する。"""
    context, runtime, writer, provider = sequence_runtime(tmp_path)
    result = await invoke(context, runtime, [step(), step(), step()])
    assert result["outcome"] == "COMPLETED" and provider.calls == 3
    assert len({str(c.tool_call_id) for c in provider.contexts}) == 3


@pytest.mark.parametrize("pointer", ["/x~2", "x", "/x~"])
def test_malformed_pointer_is_not_an_alternative_field_spelling(pointer):
    """不正な escape を実在 field の別表記として扱わない。"""
    assert not matches_checks({"x~2": True, "x~": True}, [{"pointer": pointer, "equals": True}])
'''
p.write_text(s)
p = root / 'docs/development/runtime-guide.md'
s = p.read_text().replace(
    '| 连续回执与短序列 | [局部实验](../../SKM/backend/tests/worker/test_receipt_sequence_experiment.py)未接生产 SDK、PostgreSQL 和 Outbox，不能当作正式运行模式。 |',
    '| 外部写操作序列 | [局部实验](../../SKM/backend/tests/worker/test_receipt_sequence_experiment.py)仍未接入生产。已接入的同 job SDK 复用和本地/只读短序列见下节；它们不等于取消逐次 Effect/Segment，也不执行批量外部写入。 |')
p.write_text(s)
p = root / 'docs/operations/run-performance.md'
p.write_text(p.read_text() + '\nSDK 生命周期还分别记录 `sdk_start`、`sdk_thread`、`sdk_turn_start`；`sdk_reuse` 的 `status` 区分 `new` / `reused`，不包含正文。它们属于 engine 等待内的子区间，不能与 `engine_wait` 重复相加。暖续行保留原 Segment/Attempt 和每步心跳，仅减少进程启动；不把一次复用写成少一次模型决策。\n')
