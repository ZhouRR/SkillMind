"""保存済み Artifact の原字節を、批准対象の object identity に固定する。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from uuid import UUID

from skillmind.artifacts.domain import MAX_ARTIFACT_BYTES, ArtifactContent
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.storage.blob import FileStorageError, StorageNamespace, sanitize_object_key

OBJECT_WRITE_PROTOCOL = "artifact-object-create/v1"
_CONTENT_TYPES = frozenset({"text/plain", "text/markdown", "application/json"})


class ObjectWriteConflictError(FileStorageError):
    """同じ key の object が原 Effect/内容の保存結果ではない。"""


class ObjectWriteUncertainError(FileStorageError):
    """応答・読取・実行権を確認できず、未書込や成功を断定できない。"""


@dataclass(frozen=True, slots=True)
class ObjectWriteCommand:
    """接続先を含まない原要求。生成しても PUT の実行権は与えない。"""

    effect_id: UUID
    project_id: UUID
    run_id: UUID
    artifact_ref: str
    namespace: StorageNamespace
    bucket: str
    object_key: str
    content_type: str
    content: bytes = field(repr=False)
    request_checksum: str

    @property
    def content_checksum(self) -> str:
        """Metadata の申告値ではなく、固定した実 byte から摘要を導出する。"""

        return f"sha256:{sha256_hex(self.content)}"

    @property
    def origin_metadata(self) -> dict[str, str]:
        """本文の同値だけで原成功を推測しないための、署名対象となる原 identity。"""

        return {
            "skm-protocol": OBJECT_WRITE_PROTOCOL,
            "skm-effect-id": str(self.effect_id),
            "skm-request-checksum": self.request_checksum,
            "skm-content-checksum": self.content_checksum,
        }

    def validate(self) -> None:
        """復旧入力の変更・不正 key・上限超過を I/O 前に拒否する。"""

        if (
            any(
                not isinstance(value, UUID) or value.int == 0
                for value in (
                    self.effect_id,
                    self.project_id,
                    self.run_id,
                )
            )
            or not isinstance(self.namespace, StorageNamespace)
            or not self.namespace.durable
            or type(self.content) is not bytes
            or len(self.content) > MAX_ARTIFACT_BYTES
            or self.content_type not in _CONTENT_TYPES
            or not isinstance(self.artifact_ref, str)
            or not self.artifact_ref.startswith("art_")
            or not 5 <= len(self.artifact_ref) <= 64
            or not all(c.isascii() and (c.isalnum() or c in "_-") for c in self.artifact_ref)
        ):
            raise ValueError("Object effect identity or content is invalid")
        _exact_key(self.object_key)
        self.content.decode("utf-8", errors="strict")
        if self.request_checksum != _checksum(self):
            raise ValueError("Object effect checksum does not match the original request")


@dataclass(frozen=True, slots=True)
class ObjectWriteReceipt:
    """原 metadata と実 byte を照合した読取事実。現在の存在や削除完了を保証しない。"""

    effect_id: UUID
    request_checksum: str
    object_key: str
    content_checksum: str
    size: int
    content_type: str
    etag: str
    version_id: str | None

    def __post_init__(self) -> None:
        """持続した回执も原 client と同じ opaque validator/size/摘要の形で検証する。"""

        if (
            not isinstance(self.effect_id, UUID)
            or self.effect_id.int == 0
            or type(self.size) is not int
            or not 0 <= self.size <= MAX_ARTIFACT_BYTES
            or self.content_type not in _CONTENT_TYPES
            or any(
                not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
                for value in (self.request_checksum, self.content_checksum)
            )
        ):
            raise ValueError("Object receipt is invalid")
        validate_object_etag(self.etag)
        if self.version_id is not None:
            validate_object_version(self.version_id)
        _exact_key(self.object_key)


def validate_object_etag(value: object) -> str:
    """wire と持続回执で同じ opaque な引用符付き validator を受け入れる。"""

    if not isinstance(value, str) or re.fullmatch(r'"[\x21\x23-\x7e]{1,256}"', value) is None:
        raise ValueError("Object ETag is invalid")
    return value


def validate_object_version(value: object) -> str:
    """固定版と未 version 化 marker を区別し、原 version を暗黙補正しない。"""

    if (
        not isinstance(value, str)
        or not value
        or value == "null"
        or len(value) > 1024
        or not value.isascii()
        or not value.isprintable()
    ):
        raise ValueError("Object version is invalid")
    return value


def build_object_write(
    *,
    effect_id: UUID,
    project_id: UUID,
    run_id: UUID,
    artifact: ArtifactContent,
    namespace: StorageNamespace,
    bucket: str,
    object_key: str,
    allowed_prefix: str,
    content_type: str,
) -> ObjectWriteCommand:
    """同 Run の保存済み Artifact と明示 prefix からだけ固定要求を作る。"""

    if (
        artifact.metadata.project_id != project_id
        or artifact.metadata.run_id != run_id
        or not allowed_prefix.endswith("/")
    ):
        raise ValueError("Object artifact or prefix is outside the approved project")
    _exact_key(allowed_prefix[:-1])
    _exact_key(object_key)
    if not object_key.startswith(allowed_prefix):
        raise ValueError("Object key is outside the approved prefix")
    # dataclass 自体を偽造した入力でも原字節/hash の整合を省略しない。
    ArtifactContent(artifact.metadata, artifact.content)
    command = ObjectWriteCommand(
        effect_id,
        project_id,
        run_id,
        artifact.metadata.artifact_ref,
        namespace,
        bucket,
        object_key,
        content_type,
        bytes(artifact.content),
        "",
    )
    command = replace(command, request_checksum=_checksum(command))
    command.validate()
    return command


def _exact_key(value: str) -> None:
    """別 key への暗黙正規化と UTF-8 の S3 key 上限超過を拒否する。"""

    if not isinstance(value, str):
        raise ValueError("Object key must be an exact canonical relative path")
    try:
        canonical = sanitize_object_key(value)
    except FileStorageError as error:
        raise ValueError("Object key must be an exact canonical relative path") from error
    if canonical != value:
        raise ValueError("Object key must be an exact canonical relative path")
    if len(value.encode("utf-8", errors="strict")) > 1024:
        raise ValueError("Object key is too long")


def _checksum(command: ObjectWriteCommand) -> str:
    """保存先世代、帰属、原 Artifact、key、MIME と内容を一つの原要求に束縛する。"""

    payload = {
        "protocol": OBJECT_WRITE_PROTOCOL,
        "effect_id": str(command.effect_id),
        "project_id": str(command.project_id),
        "run_id": str(command.run_id),
        "artifact_ref": command.artifact_ref,
        "namespace_id": str(command.namespace.namespace_id),
        "descriptor_checksum": command.namespace.descriptor_checksum,
        "bucket": command.bucket,
        "object_key": command.object_key,
        "content_type": command.content_type,
        "size": len(command.content),
        "content_checksum": command.content_checksum,
    }
    return f"sha256:{sha256_hex(canonical_json(payload))}"
