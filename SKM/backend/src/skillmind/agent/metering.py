"""SDK の用量観測を表示 event から分離する。予算報告や停止証明は生成しない。"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from skillmind.core.hashing import canonical_json, sha256_hex


def _fields(value: object, expected: set[str]) -> dict[str, Any]:
    """保存形式の追加 field や欠落を暗黙に補正しない。"""

    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Unsupported invocation observation shape")
    return value


def _checksum(value: str) -> None:
    """本文や任意 key を checksum として保存させない。"""

    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Invalid invocation checksum")


def _session(value: str | None) -> None:
    """SDK Session identity は元表記を維持し、UUID の形だけを確認する。"""

    if value is not None:
        if not isinstance(value, str):
            raise ValueError("Invalid invocation session identity")
        UUID(value)


class UsageValueKind(StrEnum):
    """欠測・不正値・二進浮動小数を正確な整数計量と区別する。"""

    MISSING = "MISSING"
    INTEGER = "INTEGER"
    BINARY64 = "BINARY64"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class UsageValue:
    """型と観測値のみを保持し、本文や不正な入力値を監査先へ転送しない。"""

    kind: UsageValueKind
    value: int | str | None = None

    def __post_init__(self) -> None:
        """有限な原値以外をゼロ扱いせず、観測の保存形を固定する。"""

        if not isinstance(self.kind, UsageValueKind):
            raise ValueError("Unknown SDK usage observation kind")
        if self.kind in {UsageValueKind.MISSING, UsageValueKind.INVALID}:
            valid = self.value is None
        elif self.kind is UsageValueKind.INTEGER:
            valid = type(self.value) is int and 0 <= self.value <= 2**63 - 1
        elif self.kind is UsageValueKind.BINARY64 and isinstance(self.value, str):
            try:
                number = float.fromhex(self.value)
                valid = math.isfinite(number) and number >= 0 and number.hex() == self.value
            except (ValueError, OverflowError):
                valid = False
        else:
            valid = False
        if not valid:
            raise ValueError("Invalid SDK usage observation")

    @classmethod
    def capture(cls, value: object, *, allow_binary64: bool = False) -> UsageValue:
        """SDK dataclass の注釈を信用せず、bool や数値文字列を強制変換しない。"""

        if value is None:
            return cls(UsageValueKind.MISSING)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            return cls(UsageValueKind.INTEGER, value)
        if allow_binary64 and type(value) is float and math.isfinite(value) and value >= 0:
            # hex は既に丸められた binary64 の保存形。元の USD 字句や nano-USD ではない。
            return cls(UsageValueKind.BINARY64, value.hex())
        return cls(UsageValueKind.INVALID)

    def to_json(self) -> dict[str, Any]:
        """JSON 自体に非有限 float や任意 SDK object を含めない。"""

        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_json(cls, value: object) -> UsageValue:
        """数値の型分類を保持して復元し、旧 float を正確な整数へ昇格させない。"""

        data = _fields(value, {"kind", "value"})
        return cls(UsageValueKind(data["kind"]), data["value"])


class AgentInvocationMode(StrEnum):
    """SDK 呼出し方式を表す。新規 Session を業務上の replace と推測しない。"""

    INITIAL = "INITIAL"
    RESUME = "RESUME"
    FORK = "FORK"


@dataclass(frozen=True, slots=True)
class InvocationOptions:
    """実 client options の計量関連 allowlist。全 options の監査 checksum ではない。"""

    model: str | None
    max_turns: int | None
    max_budget_usd: UsageValue
    session_id: str | None
    resume: str | None
    fork_session: bool
    continue_conversation: bool
    output_format_checksum: str
    cli_checksum: str | None = None

    def __post_init__(self) -> None:
        """履歴の暗黙継続・無効な局部 turns を持つ実行記述子を保存させない。"""

        if (
            type(self.max_turns) is not int
            or not 1 <= self.max_turns <= 2**63 - 1
            or self.continue_conversation is not False
            or type(self.fork_session) is not bool
            or not isinstance(self.max_budget_usd, UsageValue)
            or (
                self.model is not None
                and (
                    not isinstance(self.model, str)
                    or not 1 <= len(self.model) <= 256
                    or any(ord(character) < 32 for character in self.model)
                )
            )
        ):
            raise ValueError("Invalid metered Claude invocation options")
        _session(self.session_id)
        _session(self.resume)
        _checksum(self.output_format_checksum)
        if self.cli_checksum is not None:
            _checksum(self.cli_checksum)

    def to_json(self) -> dict[str, Any]:
        """Credential、prompt、hook、MCP instance や任意設定を含めず比較する。"""

        result = {
            "model": self.model,
            "max_turns": self.max_turns,
            "max_budget_usd": self.max_budget_usd.to_json(),
            "session_id": self.session_id,
            "resume": self.resume,
            "fork_session": self.fork_session,
            "continue_conversation": self.continue_conversation,
            "output_format_checksum": self.output_format_checksum,
        }
        # 旧保存値には null を後付けせず、従来 checksum のまま読戻せるようにする。
        if self.cli_checksum is not None:
            result["cli_checksum"] = self.cli_checksum
        return result

    @property
    def checksum(self) -> str:
        """この白名单だけの比較値を共通 canonical JSON/hash で算出する。"""

        return sha256_hex(canonical_json(self.to_json()))

    @classmethod
    def from_json(cls, value: object) -> InvocationOptions:
        """保存済み allowlist だけを復元し、任意の SDK options を展開しない。"""

        data = _fields(
            value,
            {
                "model",
                "max_turns",
                "max_budget_usd",
                "session_id",
                "resume",
                "fork_session",
                "continue_conversation",
                "output_format_checksum",
            } | (
                {"cli_checksum"} if isinstance(value, dict) and "cli_checksum" in value else set()
            ),
        )
        if "cli_checksum" in data:
            _checksum(data["cli_checksum"])
        return cls(
            model=data["model"],
            max_turns=data["max_turns"],
            max_budget_usd=UsageValue.from_json(data["max_budget_usd"]),
            session_id=data["session_id"],
            resume=data["resume"],
            fork_session=data["fork_session"],
            continue_conversation=data["continue_conversation"],
            output_format_checksum=data["output_format_checksum"],
            cli_checksum=data.get("cli_checksum"),
        )


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    """一回の adapter 呼出しを識別する。持久予算の execution/reservation ID ではない。"""

    invocation_id: UUID
    project_id: UUID
    run_id: UUID
    run_attempt_id: UUID
    user_id: UUID
    session_id: str
    mode: AgentInvocationMode
    parent_session_id: str | None
    prompt_checksum: str
    options: InvocationOptions
    sdk_version: str
    cli_version: str

    def __post_init__(self) -> None:
        """実 options と方式が一致した完全な原 identity だけを束縛できるようにする。"""

        if any(
            not isinstance(value, UUID)
            for value in (
                self.invocation_id,
                self.project_id,
                self.run_id,
                self.run_attempt_id,
                self.user_id,
            )
        ) or not isinstance(self.options, InvocationOptions):
            raise ValueError("Invalid invocation identity")
        if not isinstance(self.session_id, str):
            raise ValueError("Invocation session is required")
        _session(self.session_id)
        _session(self.parent_session_id)
        _checksum(self.prompt_checksum)
        for version in (self.sdk_version, self.cli_version):
            if (
                not isinstance(version, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", version) is None
            ):
                raise ValueError("Invalid invocation build version")
        options = self.options
        if self.mode is AgentInvocationMode.INITIAL:
            valid = (
                options.session_id == self.session_id
                and options.resume is None
                and not options.fork_session
            )
        elif self.mode is AgentInvocationMode.RESUME:
            valid = (
                options.session_id is None
                and options.resume == self.session_id
                and not options.fork_session
            )
        elif self.mode is AgentInvocationMode.FORK:
            valid = (
                options.session_id == self.session_id
                and options.resume is not None
                and options.resume != self.session_id
                and options.fork_session
            )
        else:
            valid = False
        if not valid or self.parent_session_id != options.resume:
            raise ValueError("Claude invocation mode does not match its actual options")

    def to_json(self) -> dict[str, Any]:
        """原 invocation の内部保存版。予算 group/request/policy の旧 hash は変更しない。"""

        return {
            "version": "agent-invocation/v1",
            "invocation_id": str(self.invocation_id),
            "project_id": str(self.project_id),
            "run_id": str(self.run_id),
            "run_attempt_id": str(self.run_attempt_id),
            "user_id": str(self.user_id),
            "session_id": self.session_id,
            "mode": self.mode.value,
            "parent_session_id": self.parent_session_id,
            "prompt_checksum": self.prompt_checksum,
            "options": self.options.to_json(),
            "sdk_version": self.sdk_version,
            "cli_version": self.cli_version,
        }

    @property
    def checksum(self) -> str:
        """起動許可と原 Result が同じ記述子を指すことを全 field で照合する。"""

        return sha256_hex(canonical_json(self.to_json()))

    @classmethod
    def from_json(cls, value: object) -> AgentInvocation:
        """未知版・壊れた原 identity を別実行として再構成しない。"""

        data = _fields(
            value,
            {
                "version",
                "invocation_id",
                "project_id",
                "run_id",
                "run_attempt_id",
                "user_id",
                "session_id",
                "mode",
                "parent_session_id",
                "prompt_checksum",
                "options",
                "sdk_version",
                "cli_version",
            },
        )
        if data["version"] != "agent-invocation/v1":
            raise ValueError("Unsupported invocation version")
        identities = {}
        for key in ("invocation_id", "project_id", "run_id", "run_attempt_id", "user_id"):
            if not isinstance(data[key], str):
                raise ValueError("Invalid stored invocation identity")
            identities[key] = UUID(data[key])
        return cls(
            **identities,
            session_id=data["session_id"],
            mode=AgentInvocationMode(data["mode"]),
            parent_session_id=data["parent_session_id"],
            prompt_checksum=data["prompt_checksum"],
            options=InvocationOptions.from_json(data["options"]),
            sdk_version=data["sdk_version"],
            cli_version=data["cli_version"],
        )


@dataclass(frozen=True, slots=True)
class ResultUsageObservation:
    """原 SDK Result の限定観測。累積範囲・最終用量・停止を自己宣言させない。"""

    invocation: AgentInvocation
    turns: UsageValue
    cost_usd: UsageValue

    def __post_init__(self) -> None:
        """原観測に任意 object や float turns を直接混ぜる入口を閉じる。"""

        if (
            not isinstance(self.invocation, AgentInvocation)
            or not isinstance(self.turns, UsageValue)
            or not isinstance(self.cost_usd, UsageValue)
            or self.turns.kind is UsageValueKind.BINARY64
        ):
            raise ValueError("Invalid Result usage observation")

    @property
    def observation_key(self) -> str:
        """同じ呼出しの Result は同じ slot。内容が変わっても別鍵へ逃がさない。"""

        return f"claude-result/{self.invocation.invocation_id}"

    def to_json(self) -> dict[str, Any]:
        """独立保存できる原観測だけを返し、final や停止回执を追加しない。"""

        return {
            "version": "sdk-result-observation/v1",
            "invocation": self.invocation.to_json(),
            "turns": self.turns.to_json(),
            "cost_usd": self.cost_usd.to_json(),
        }


# サービス組立て専用の port。Tool 引数、RunEvent、公開 API から呼出し権を与えない。
ExecutionUsageObserver = Callable[[ResultUsageObservation], Awaitable[None]]

# 受信サービスが原束縛と初回 START_INTENT commit を検証する。True 以外は起動しない。
BeforeInvocationConnect = Callable[[AgentInvocation], Awaitable[bool]]


@runtime_checkable
class InvocationControlledEngine(Protocol):
    """予算装配の取り違えを SDK 型なしで確認する adapter port。"""

    def has_invocation_callbacks(
        self, before_connect: BeforeInvocationConnect, observer: ExecutionUsageObserver
    ) -> bool:
        """実 client の起動と観測へ、指定された同じ callback が装配されているか返す。"""
        ...
