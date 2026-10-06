"""Run 内の検証済み画像を、監査 metadata とモデル向け画像 content に分ける。"""

from __future__ import annotations

import asyncio
import base64
import io
import warnings
from collections.abc import Mapping
from typing import Any

from PIL import Image, UnidentifiedImageError

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.agent.workspace_provider import _read_workspace_bytes, _resolve_workspace_path
from skillmind.core.hashing import sha256_hex

CAPABILITY = "workspace.image/v1"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
_MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def _inspect(data: bytes) -> dict[str, Any]:
    """decoder で形式・実画素・単一 frame を確認し、SVG や巨大画像を渡さない。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (
                    image.format not in _MIME_TYPES
                    or image.width * image.height > MAX_IMAGE_PIXELS
                    or image.width <= 0
                    or image.height <= 0
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError("Image format or dimensions are unsupported")
                descriptor = {
                    "mime_type": _MIME_TYPES[image.format],
                    "width": image.width,
                    "height": image.height,
                    "size_bytes": len(data),
                    "content_hash": "sha256:" + sha256_hex(data),
                }
                image.verify()
            # JPEG 等の verify が画素全体を decode しない場合も、破損を取得時に拒否する。
            with Image.open(io.BytesIO(data)) as image:
                image.load()
            return descriptor
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ToolProviderError(
            "invalid_request",
            "Image is invalid, unsupported or exceeds its pixel limit",
            retryable=False,
        ) from None


def read_image(
    context: RunToolContext, path: Any, expected_hash: Any
) -> tuple[bytes, dict[str, Any]]:
    """封存 input と安全な作業 file の既存境界で読み、原 byte hash を必須照合する。"""
    relative, target = _resolve_workspace_path(context, path, require_file=True)
    data = _read_workspace_bytes(context, relative, target, max_bytes=MAX_IMAGE_BYTES)
    if expected_hash != "sha256:" + sha256_hex(data):
        raise ToolProviderError("invalid_request", "Image file changed", retryable=False)
    return data, {"path": relative, **_inspect(data)}


class WorkspaceImageProvider:
    """返却前に Evidence を保存し、Base64 を通常の result/log に保存しない。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """ファイル名や MIME 宣言でなく実画像を検証し、短い descriptor だけ返す。"""
        _, descriptor = await asyncio.to_thread(
            read_image, context, arguments.get("path"), arguments.get("expected_hash")
        )
        return ProviderToolResult(
            response={"status": "success", "provider": "workspace", "image": descriptor},
            evidence=(
                EvidenceDraft(
                    evidence_type="workspace-image",
                    source_uri=f"workspace://runs/{context.run_id}/{descriptor['path']}",
                    source_locator={"path": descriptor["path"]},
                    content_hash=descriptor["content_hash"],
                    metadata={"image": descriptor},
                ),
            ),
        )


async def image_block(context: RunToolContext, response: Mapping[str, Any]) -> dict[str, Any]:
    """確定回执の hash/寸法と再照合してから、両 Engine 共通の MCP image を交付する。"""
    descriptor = response["image"]
    data, actual = await asyncio.to_thread(
        read_image, context, descriptor["path"], descriptor["content_hash"]
    )
    if actual != descriptor:
        raise ToolProviderError("invalid_request", "Image snapshot changed", retryable=False)
    return {
        "type": "image",
        "mimeType": actual["mime_type"],
        "data": base64.b64encode(data).decode("ascii"),
    }
