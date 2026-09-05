"""扇出子 Agent の能力集と予算切分を検証する (計画 §23 D1/D4/D5)。

§23 の安全性は「子 Agent は主 Agent の読み取り真部分集合しか持てない」の一点に乗っている。
ここが緩むと、外部効果の帰属と workspace 書き込みの相互上書きという二種類の衝突が**存在し得る**
状態に戻る——しかもどちらも起きてから気付く類の欠陥なので、境界そのものをテストで固定する。
"""

from __future__ import annotations

import pytest

from projectmind.agent.subagent import (
    FORBIDDEN_SUBAGENT_CAPABILITIES,
    MAX_SUBAGENT_BRANCHES,
    SubagentCapabilityError,
    is_write_like,
    resolve_subagent_capabilities,
    split_budget,
)

_PARENT = frozenset(
    {
        "workspace.read/v1",
        "workspace.search/v1",
        "workspace.write/v1",
        "issue.read/v1",
        "repository.read/v1",
        "repository.write/v1",
        "issue.update/v1",
        "change.propose/v1",
        "interaction.request/v1",
        "subagent.dispatch/v1",
    }
)


def test_empty_request_grants_only_the_parent_read_capabilities() -> None:
    """要求なしは「主 Agent の読み取り能力すべて」。書き込み・効果・交互・扇出は落ちる。"""

    granted = resolve_subagent_capabilities((), parent_allowed=_PARENT)

    assert set(granted) == {
        "workspace.read/v1",
        "workspace.search/v1",
        "issue.read/v1",
        "repository.read/v1",
    }


@pytest.mark.parametrize("capability", sorted(FORBIDDEN_SUBAGENT_CAPABILITIES))
def test_forbidden_capabilities_are_rejected_not_silently_dropped(capability: str) -> None:
    """禁止能力の明示要求は**拒否**する。黙って削ると Agent は渡った前提で分岐を書く。"""

    with pytest.raises(SubagentCapabilityError) as error:
        resolve_subagent_capabilities((capability,), parent_allowed=_PARENT)

    assert error.value.code == "capability_not_granted"


@pytest.mark.parametrize(
    "capability", ["repository.write/v1", "issue.update/v1", "workspace.write/v1"]
)
def test_write_capabilities_never_reach_a_sub_agent(capability: str) -> None:
    """登録済み write capability は主が持っていても子へ渡らない (§23 D1)。"""

    with pytest.raises(SubagentCapabilityError):
        resolve_subagent_capabilities((capability,), parent_allowed=_PARENT)
    assert capability not in resolve_subagent_capabilities((), parent_allowed=_PARENT)


def test_unknown_write_shaped_capability_is_refused_by_shape() -> None:
    """将来 write 能力が増えても、名前の形だけで拒否側へ倒れることを確認する。

    列挙で持つと追加時に足し忘れた分だけ子へ漏れる。知らない能力は拒否へ倒すのが要件。
    """

    assert is_write_like("document.write/v1") is True
    with pytest.raises(SubagentCapabilityError):
        resolve_subagent_capabilities(
            ("document.write/v1",),
            parent_allowed=_PARENT | {"document.write/v1"},
        )


def test_capability_not_held_by_the_parent_is_refused() -> None:
    """主が持たない能力は子にも渡らない。真部分集合であることの確認。"""

    with pytest.raises(SubagentCapabilityError) as error:
        resolve_subagent_capabilities(("document.read/v1",), parent_allowed=_PARENT)

    assert error.value.code == "capability_not_granted"


def test_explicit_subset_is_granted_as_requested() -> None:
    """正当な部分集合はそのまま付与される。"""

    granted = resolve_subagent_capabilities(
        ("workspace.read/v1", "issue.read/v1"), parent_allowed=_PARENT
    )

    assert granted == ("issue.read/v1", "workspace.read/v1")


def test_budget_is_split_across_branches_not_multiplied() -> None:
    """予算は分割する。branch ごとに与え直すと「並行 4 路」が費用上限の 4 倍になる。"""

    budget = split_budget(branches=4, remaining_turns=16, remaining_output_bytes=1_048_576)

    assert budget.turns_per_branch == 4
    assert budget.output_bytes_per_branch == 262_144
    # 合計が元の上限を超えないことが本質。
    assert budget.turns_per_branch * 4 <= 16
    assert budget.output_bytes_per_branch * 4 <= 1_048_576


def test_uneven_split_rounds_down_so_the_total_stays_within_the_run_limit() -> None:
    """割り切れない分は切り捨てる。切り上げると合計が Run 上限を超える。"""

    budget = split_budget(branches=3, remaining_turns=10, remaining_output_bytes=1000)

    assert budget.turns_per_branch == 3
    assert budget.turns_per_branch * 3 <= 10
    assert budget.output_bytes_per_branch * 3 <= 1000


def test_budget_that_cannot_be_split_is_refused() -> None:
    """一 branch 分すら残っていなければ扇出させない。"""

    with pytest.raises(SubagentCapabilityError) as error:
        split_budget(branches=4, remaining_turns=2, remaining_output_bytes=1_048_576)

    assert error.value.code == "budget_exhausted"


@pytest.mark.parametrize("branches", [0, MAX_SUBAGENT_BRANCHES + 1])
def test_branch_count_is_bounded(branches: int) -> None:
    """扇出は有界。上限は contract 側の maxItems と一致させる (§23 D5)。"""

    with pytest.raises(SubagentCapabilityError) as error:
        split_budget(branches=branches, remaining_turns=100, remaining_output_bytes=10_000_000)

    assert error.value.code == "invalid_request"
