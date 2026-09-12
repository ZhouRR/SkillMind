"""凍結集合の directory 選択と storage 更新日の時区間を定義する。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from skillmind.documents.snapshot import FrozenDocument


@dataclass(frozen=True, slots=True)
class DirectoryQuery:
    """I/O 前に解決済みの path 条件と、UTC に写像した暦日の半開区間。"""

    parameters: dict[str, Any]
    starts_at: datetime | None
    ends_at: datetime | None
    not_after: datetime | None

    def candidates(self, documents: Sequence[FrozenDocument]) -> tuple[FrozenDocument, ...]:
        """directory 境界・再帰・拡張子・除外を適用し、安定した path 順に並べる。"""

        directory = self.parameters["directory"]
        recursive = self.parameters["recursive"]
        extensions = self.parameters["extensions"]
        excluded = self.parameters["exclude_directories"]
        prefixes = self.parameters["exclude_name_prefixes"]
        return tuple(
            sorted(
                (
                    item
                    for item in documents
                    if (
                        not directory
                        or item.folder == directory
                        or (recursive and item.folder.startswith(directory + "/"))
                    )
                    and (recursive or item.folder == directory)
                    and (
                        not extensions or any(item.name.lower().endswith(ext) for ext in extensions)
                    )
                    and not any(item.name.startswith(prefix) for prefix in prefixes)
                    and not any(
                        item.folder == path or item.folder.startswith(path + "/")
                        for path in excluded
                    )
                ),
                key=lambda item: item.path,
            )
        )

    def matches_time(self, last_modified: datetime) -> bool:
        """暦日の長さを 24 時間と仮定せず、抽出 cutoff の等値は含める。"""

        return self.starts_at is None or (
            self.ends_at is not None
            and self.not_after is not None
            and self.starts_at <= last_modified < self.ends_at
            and last_modified <= self.not_after
        )


def parse_directory_query(arguments: Mapping[str, Any]) -> DirectoryQuery:
    """不明な path/date を補完せず、全 page で再利用できる正規化済み条件を作る。"""

    directory = _directory(arguments.get("directory"))
    recursive = arguments.get("recursive", True)
    if type(recursive) is not bool:
        raise ValueError("Recursive selection is invalid")
    extensions = _strings(arguments.get("extensions", []), 16)
    if any(re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", item) is None for item in extensions):
        raise ValueError("Document extensions are invalid")
    excluded = tuple(
        _directory(item) for item in _strings(arguments.get("exclude_directories", []), 16)
    )
    if any(not item for item in excluded):
        raise ValueError("Excluded directory is invalid")
    prefixes = _strings(arguments.get("exclude_name_prefixes", []), 16)
    if any(
        len(item) > 128 or not item.isprintable() or "/" in item or "\\" in item
        for item in prefixes
    ):
        raise ValueError("Excluded name prefix is invalid")
    parameters: dict[str, Any] = {
        "directory": directory,
        "recursive": recursive,
        "extensions": sorted({item.lower() for item in extensions}),
        "exclude_directories": sorted(set(excluded)),
        "exclude_name_prefixes": sorted(set(prefixes)),
        "modified_on": None,
    }
    modified = arguments.get("modified_on")
    if modified is None:
        if "modified_on" in arguments:
            raise ValueError("Modification date filter is invalid")
        return DirectoryQuery(parameters, None, None, None)
    if not isinstance(modified, Mapping) or set(modified) != {"date", "timezone", "not_after"}:
        raise ValueError("Modification date filter is invalid")
    value, timezone, cutoff = (modified[key] for key in ("date", "timezone", "not_after"))
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None
        or not isinstance(timezone, str)
        or not 1 <= len(timezone) <= 128
        or not isinstance(cutoff, str)
        or "T" not in cutoff
        or len(cutoff) > 64
    ):
        raise ValueError("Modification date filter is invalid")
    try:
        day = date.fromisoformat(value)
        zone = ZoneInfo(timezone)
        starts = datetime.combine(day, time.min, zone).astimezone(UTC)
        ends = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
        not_after = datetime.fromisoformat(cutoff)
        if not_after.tzinfo is None:
            raise ValueError("Cutoff requires a timezone")
        not_after = not_after.astimezone(UTC)
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as error:
        raise ValueError("Modification date filter is invalid") from error
    parameters["modified_on"] = {
        "date": value,
        "timezone": timezone,
        "not_after": not_after.isoformat(),
    }
    return DirectoryQuery(parameters, starts, ends, not_after)


def _directory(value: object) -> str:
    """空を Project root とし、prefix の末尾一つの斜線だけを許容する。"""

    if not isinstance(value, str) or len(value) > 512 or value.startswith("/") or "\\" in value:
        raise ValueError("Document directory is invalid")
    if value == "":
        return value
    relative = value.removesuffix("/")
    if any(
        part in {"", ".", "..", ".skillmind"} or not part.isprintable()
        for part in relative.split("/")
    ):
        raise ValueError("Document directory is invalid")
    return relative


def _strings(value: object, limit: int) -> tuple[str, ...]:
    """空文字・重複・非文字列を含む無制限の条件列を受理しない。"""

    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ValueError("Directory query values are invalid")
    if len(set(value)) != len(value):
        raise ValueError("Directory query values are duplicated")
    return tuple(value)
