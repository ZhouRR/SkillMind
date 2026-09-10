"""Model を呼ばない Skill 解釈 test の明示的な代替実装。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from skillmind.skills.interpreter_execution import InterpretProgressCallback


class FixtureSkillInterpreter:
    """固定 response を返し、変更可能な値を呼出し間で共有しない。"""

    def __init__(self, response: Mapping[str, Any]) -> None:
        """合成候補を防御的に複製する。"""

        self._response = deepcopy(dict(response))

    async def interpret(
        self,
        request: Mapping[str, Any],
        *,
        model: str,
        parameters: Mapping[str, Any],
        validation_feedback: str | None = None,
        on_event: InterpretProgressCallback | None = None,
    ) -> dict[str, Any]:
        """外部接続せず、各呼出しへ独立した候補を返す。"""

        del request, model, parameters, validation_feedback, on_event
        return deepcopy(self._response)
