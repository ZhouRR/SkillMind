"""扇出子 Agent の能力集と予算切分を決める domain 規則 (計画 §23 D1/D4/D5)。

本 module が守る不変条件は一つに尽きる——**子 Agent は主 Agent の読み取り能力の真部分集合しか
持てない**。write / effect / interaction / dispatch を子へ渡さないことで、外部効果の帰属と
workspace 書き込みの相互上書きという二種類の衝突を**存在し得ない状態にする**。緩めた瞬間に
「どの子が提案したのか」「どちらの書き込みが残ったのか」を後から追えなくなる。

判定は allowlist ではなく **denylist + 明示 allowlist の二重**にしてある。片方だけだと、新しい
能力を追加したときに既定でどちらへ倒れるかが実装依存になる。
"""

from __future__ import annotations

from dataclasses import dataclass

from projectmind.effects.proposal import CHANGE_PROPOSE_CAPABILITY
from projectmind.runs.interaction import INTERACTION_REQUEST_CAPABILITY

# 扇出そのものを指す control capability。子には決して渡さない (D5: 入れ子禁止)。
SUBAGENT_DISPATCH_CAPABILITY = "subagent.dispatch/v1"

# 一回の dispatch で並行させてよい branch 数の上限 (D5)。contract 側の maxItems と一致させる。
MAX_SUBAGENT_BRANCHES = 4

# 子 Agent へ**決して**渡さない capability。ここは意味で選んである:
# - 外部効果の提案 (帰属が曖昧になる)
# - 利用者への問い合わせ (どの子が聞いたのか画面で表せない)
# - workspace への書き込み (並行する子同士が黙って上書きし合う)
# - 扇出自身 (入れ子で予算計算が破綻する)
FORBIDDEN_SUBAGENT_CAPABILITIES = frozenset(
    {
        CHANGE_PROPOSE_CAPABILITY,
        INTERACTION_REQUEST_CAPABILITY,
        SUBAGENT_DISPATCH_CAPABILITY,
        "workspace.write/v1",
    }
)


class SubagentCapabilityError(ValueError):
    """要求された子能力が主 Agent の読み取り部分集合に収まらないことを表す。"""

    def __init__(self, code: str, message: str) -> None:
        """Agent へ返してよい安定 code と message を保持する。"""

        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SubagentBudget:
    """一つの branch へ配分する実行予算 (計画 §23 D4)。

    Run 全体の上限を**切り分ける**のであって、branch ごとに与え直すのではない。後者だと
    「並行 4 路」がそのまま費用上限の 4 倍を意味してしまう。
    """

    turns_per_branch: int
    output_bytes_per_branch: int


def is_write_like(capability: str) -> bool:
    """capability 名から書き込み系かどうかを判定する。

    登録済み write capability を列挙で持つと、追加時にここへ足し忘れた分だけ子へ漏れる。
    `.write/` を含む識別子は一律に書き込み扱いにし、**知らない能力は拒否側へ倒す**。
    """

    return ".write/" in capability or capability.endswith(".update/v1")


def resolve_subagent_capabilities(
    requested: tuple[str, ...],
    *,
    parent_allowed: frozenset[str],
) -> tuple[str, ...]:
    """要求された子能力を検証し、実際に付与する集合を返す (計画 §23 D1)。

    空要求は「主 Agent の読み取り能力すべて」と解釈する。要求があるときは主の部分集合であることを
    確認したうえで、禁止集合と書き込み系を落とす。**落とすのではなく拒否する**のは、
    黙って減らすと Agent が「渡したはずの能力で動く」前提で分岐を書いてしまうため。
    """

    readable = frozenset(
        capability
        for capability in parent_allowed
        if capability not in FORBIDDEN_SUBAGENT_CAPABILITIES and not is_write_like(capability)
    )
    if not requested:
        return tuple(sorted(readable))
    for capability in requested:
        if capability in FORBIDDEN_SUBAGENT_CAPABILITIES or is_write_like(capability):
            raise SubagentCapabilityError(
                "capability_not_granted",
                f"Sub-agents cannot receive this capability: {capability}",
            )
        if capability not in readable:
            raise SubagentCapabilityError(
                "capability_not_granted",
                f"Capability is not held by the dispatching agent: {capability}",
            )
    return tuple(sorted(set(requested)))


def split_budget(
    *,
    branches: int,
    remaining_turns: int,
    remaining_output_bytes: int,
) -> SubagentBudget:
    """残予算を branch 数で割り、一 branch あたりの上限を決める (計画 §23 D4)。

    割り切れない分は切り捨てる。切り上げると合計が Run 上限を超え、「予算は切分であって
    相乗ではない」という決定が成り立たなくなる。
    """

    if branches < 1 or branches > MAX_SUBAGENT_BRANCHES:
        raise SubagentCapabilityError(
            "invalid_request",
            f"Sub-agent branch count must be between 1 and {MAX_SUBAGENT_BRANCHES}",
        )
    turns = remaining_turns // branches
    output_bytes = remaining_output_bytes // branches
    if turns < 1 or output_bytes < 1:
        raise SubagentCapabilityError(
            "budget_exhausted",
            "Remaining Run budget cannot be split across the requested branches",
        )
    return SubagentBudget(turns_per_branch=turns, output_bytes_per_branch=output_bytes)
