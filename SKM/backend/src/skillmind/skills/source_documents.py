"""解釈要約と独立して、凍結された Skill 原文を検証・伝達する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from skillmind.core.hashing import sha256_hex
from skillmind.skills.domain import InlineSkillFile
from skillmind.skills.importer import SkillImportLimits


class SourceTraceLocationError(ValueError):
    """出典値を含めず、候補内の位置と固定した拒否理由を伝える。"""

    def __init__(
        self, code: Literal["source_trace_file_invalid", "source_trace_line_invalid"], path: str
    ) -> None:
        """修復 feedback と公開門禁で同じ安全な診断を使用する。"""

        super().__init__(
            "Source trace file must exist in the saved source package."
            if code == "source_trace_file_invalid"
            else "Source trace line must refer to an existing line in the saved source file."
        )
        self.code = code
        self.path = path


def validate_source_location(
    path: Any, line: Any, files: Mapping[str, str | None], *, pointer: str,
    path_field: str = "path",
) -> Literal["TEXT_SNAPSHOT", "SOURCE_INDEX"]:
    """解釈と発行で同じ実 file/行を検査し、推測で行を補正しない。"""

    if (
        not isinstance(path, str) or not 0 < len(path) <= 1024
        or not path.isprintable() or any(c in path for c in ("\\", ":"))
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or path not in files
    ):
        raise SourceTraceLocationError("source_trace_file_invalid", f"{pointer}/{path_field}")
    content = files[path]
    if line is not None and (
        type(line) is not int or line <= 0 or content is None
        or line > max(1, len(content.splitlines()))
    ):
        raise SourceTraceLocationError("source_trace_line_invalid", f"{pointer}/line")
    return "SOURCE_INDEX" if content is None else "TEXT_SNAPSHOT"


def validate_manifest_source_locations(
    manifest: Mapping[str, Any], files: Mapping[str, str | None],
) -> None:
    """Schema 検証後の候補にある Blueprint と生成契約の出典を全件照合する。"""

    traces = manifest.get("capability_blueprint", {}).get("source_traces", [])
    for index, trace in enumerate(traces):
        validate_source_location(
            trace["path"], trace.get("line"), files,
            pointer=f"/capability_blueprint/source_traces/{index}",
        )
    for task_index, task in enumerate(manifest["tasks"]):
        for index, trace in enumerate(task["contract_source_trace"]):
            validate_source_location(
                trace["source_path"], trace.get("line"), files,
                pointer=f"/tasks/{task_index}/contract_source_trace/{index}",
                path_field="source_path",
            )


def build_source_documents(files: Sequence[InlineSkillFile]) -> list[dict[str, str]]:
    """検証済み text snapshot を省略・翻訳せず、原 byte の hash とともに固定する。"""

    return validate_source_documents([
        {"path": item.path, "content": item.content,
         "sha256": f"sha256:{sha256_hex(item.content.encode('utf-8'))}"}
        for item in sorted(files, key=lambda item: item.path)
    ])


def validate_source_documents(value: Any) -> list[dict[str, str]]:
    """原文の破損・重複・上限超過を拒否し、内容を含まないエラーだけを返す。"""

    limits = SkillImportLimits()
    if not isinstance(value, list) or not 1 <= len(value) <= limits.max_files:
        raise ValueError("Frozen Skill source documents are invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "content", "sha256"}:
            raise ValueError("Frozen Skill source document is invalid")
        path, content, checksum = item["path"], item["content"], item["sha256"]
        if (
            not isinstance(path, str) or not 0 < len(path) <= 1024
            or not path.isprintable() or any(c in path for c in ("\\", ":"))
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in seen or not isinstance(content, str)
        ):
            raise ValueError("Frozen Skill source document is invalid")
        raw = content.encode("utf-8")
        total += len(raw)
        if (
            len(raw) > limits.max_file_bytes or total > limits.max_total_bytes
            or checksum != f"sha256:{sha256_hex(raw)}"
        ):
            raise ValueError("Frozen Skill source document integrity check failed")
        seen.add(path)
        result.append({"path": path, "content": content, "sha256": checksum})
    return result
