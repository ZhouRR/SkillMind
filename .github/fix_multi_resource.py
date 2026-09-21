"""原認可 object を維持し、SDK 用 Schema だけを複写する。既存断言は変更しない。"""
from pathlib import Path

path = Path('SKM/backend/src/skillmind/agent/tool_routing.py')
text = path.read_text()
old = '            group.append(deepcopy(tool))\n'
assert text.count(old) == 1
text = text.replace(old, '            # 認可は元の frozen RegisteredTool を返す。SDK view の Schema だけ別途複写する。\n            group.append(tool)\n', 1)
path.write_text(text)

path = Path('SKM/backend/tests/agent/test_resource_tool_routing.py')
text = path.read_text()
assert 'test_resource_selection_preserves_original_registered_object' not in text
text += '''


def test_resource_selection_preserves_original_registered_object(tmp_path):
    """同類の binding を選んでも認可 object を置き換えず、SDK view は元契約を変更しない。"""
    context, _, _, _ = routed_runtime(tmp_path)
    original = deepcopy(context.tools)
    policy = ToolExecutionPolicy(context.tools)
    for tool in context.tools[:2]:
        args = {"resource_key": tool.resource_key, "table": "public.items", "purpose": "fixture"}
        assert policy.authorize(tool.sdk_name, args) is tool
    public = policy.sdk_tools[0]
    public.input_schema["properties"]["resource_key"]["enum"].append("not-authorized")
    assert context.tools == original
    with pytest.raises(ToolPolicyViolation, match="resource_key"):
        policy.authorize(context.tools[0].sdk_name, {
            "resource_key": "not-authorized", "table": "public.items", "purpose": "fixture",
        })


def test_unbound_proposal_resource_key_and_identity_are_not_consumed():
    """提案の business selector は原 proposal validator に渡し、外部 write 権を作らない。"""
    from tests.agent.test_tool_policy import _database_proposal

    args, _, tool, policy = _database_proposal()
    original = deepcopy(args)
    assert policy.authorize(tool.sdk_name, args) is tool
    assert provider_arguments(tool, args) == original
    assert policy.registered(tool.sdk_name) is tool
    assert args == original
'''
path.write_text(text)
