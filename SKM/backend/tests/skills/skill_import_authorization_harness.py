"""実 Import/User repository と逐 PUT の資格境界を局部 SQL で検証する。

行集合の rollback は合成であり、PostgreSQL の競争や object store の停止を証明しない。
外部 PUT の既存 bytes は DB rollback から除外し、原資格だけを本番 authorizer で判定する。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self, cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.db.models import AuthSession, SkillInterpretation, SkillSource, User
from skillmind.skills.domain import InlineSkillFile, StoredSkillPreview, UploadSkillFile
from skillmind.skills.service import SkillService
from skillmind.storage import FileStorage, InMemoryFileStorage
from skillmind.storage.blob import StoredBlob
from tests.skills.skill_lifecycle_harness import POSTGRESQL_DIALECT
from tests.skills.test_skill_publication_authorization import (
    AuthorizationSession,
    ControlledTransaction,
)
from tests.skills.test_skill_publication_validation import CONTRACTS, Model, PublicationResult

ImportKind = Literal["inline", "upload"]
KINDS: tuple[ImportKind, ...] = ("inline", "upload")
SKILL_TEXT = "---\nname: Synthetic Import\nallowed-tools: [Read]\n---\n# Synthetic Import\n"
RULE_TEXT = "# Rules\nPreserve the original evidence.\n"
INLINE_FILES = (
    InlineSkillFile(path="SKILL.md", content=SKILL_TEXT),
    InlineSkillFile(path="references/rules.md", content=RULE_TEXT),
)
UPLOAD_FILES = tuple(
    UploadSkillFile(path=file.path, data=file.content.encode("utf-8"), content_type="text/markdown")
    for file in INLINE_FILES
)


class ImportTransaction(ControlledTransaction):
    """基底の原行復元に、親 flush 集合と短 transaction の生存観測を足す。"""

    def __init__(self, owner: ImportSession) -> None:
        """原会話や blob を自分の rollback 対象へ混ぜない。"""
        super().__init__(owner)
        self.import_owner = owner
        self.persisted: set[UUID] = set()
        self.pending: list[SkillSource | SkillInterpretation] = []

    async def __aenter__(self) -> Self:
        """重複 transaction を拒否し、今回の pending 行だけを記録する。"""
        assert not self.import_owner.transaction_active
        self.import_owner.transaction_active = True
        self.persisted = set(self.import_owner.persisted)
        self.pending = list(self.import_owner.pending)
        await super().__aenter__()
        return self

    def restore(self) -> None:
        """合成 DB 行を戻しても外部 storage に書いた bytes は削除しない。"""
        super().restore()
        self.import_owner.persisted = self.persisted
        self.import_owner.pending = self.pending

    async def __aexit__(self, kind: object, error: object, traceback: object) -> None:
        """commit 例外でも現在 transaction の終了を必ず観測する。"""
        try:
            await super().__aexit__(kind, error, traceback)
        finally:
            self.import_owner.transaction_active = False


class ImportStorage(InMemoryFileStorage):
    """本番 memory storage の前後へ待機注入を加え、実 Provider を呼ばない。"""

    def __init__(self, owner: ImportSession) -> None:
        """bytes の保存と API 側待機の取消を別々に観測できるようにする。"""
        super().__init__()
        self.owner = owner
        self.attempts: list[tuple[str, bytes, str]] = []
        self.deletions: list[str] = []
        self.transform: Callable[[StoredBlob], StoredBlob] | None = None

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """PUT 中は DB lock が無いことと、受付記録と実 bytes の違いを検証する。"""
        assert not self.owner.transaction_active
        self.attempts.append((key, bytes(data), content_type))
        self.owner.emit("put-start")
        result = await super().put(key, data, content_type=content_type)
        self.owner.emit("put-done")
        assert not self.owner.transaction_active
        return self.transform(result) if self.transform is not None else result

    async def delete(self, key: str) -> None:
        """失敗補償を新規に実装した場合を検出し、既存 bytes の消去を隠さない。"""
        self.deletions.append(key)
        await super().delete(key)


class ImportSession(AuthorizationSession):
    """保存済み Source/Interpretation と実資格 SQL を提供する、API 再利用可能な seam。"""

    def __init__(self) -> None:
        """資格は API の現在時計でも有効にし、初期の import 資産集合だけを空にする。"""
        super().__init__()
        self.rows = ()
        self.original_rows = ()
        now = datetime.now(UTC)
        self.auth_session.idle_expires_at = now + timedelta(minutes=30)
        self.auth_session.absolute_expires_at = now + timedelta(hours=8)
        self.persisted: set[UUID] = set()
        self.pending: list[SkillSource | SkillInterpretation] = []
        self.added: list[SkillSource | SkillInterpretation] = []
        self.transaction_active = False
        self.storage = ImportStorage(self)

    @property
    def sources(self) -> list[SkillSource]:
        """別の fixture の原 Source を補造せず、現在の保存集合だけを返す。"""
        return [row for row in self.rows if isinstance(row, SkillSource)]

    @property
    def interpretations(self) -> list[SkillInterpretation]:
        """同一 source/checksum の復用と追加を観測する。"""
        return [row for row in self.rows if isinstance(row, SkillInterpretation)]

    def begin(self) -> ImportTransaction:
        """短い原資格 transaction ごとに独立した rollback snapshot を取る。"""
        return ImportTransaction(self)

    async def scalars(self, statement: Select[tuple[Any, ...]]) -> PublicationResult:
        """実 WHERE の構造一致と値を検査し、単に全検索へ None を返す fake にしない。"""
        entity = statement.column_descriptions[0]["entity"]
        if entity in {User, AuthSession}:
            return await super().scalars(statement)
        parameters = statement.compile().params
        matches: list[Model]
        if entity is SkillSource:
            expected_source = select(SkillSource).where(
                SkillSource.organization_id == parameters["organization_id_1"],
                SkillSource.content_hash == parameters["content_hash_1"],
            )
            assert statement.compare(expected_source)
            matches = [
                row
                for row in self.sources
                if row.organization_id == parameters["organization_id_1"]
                and row.content_hash == parameters["content_hash_1"]
            ]
            stage = "source"
        elif entity is SkillInterpretation:
            expected_interpretation = select(SkillInterpretation).where(
                SkillInterpretation.skill_source_id == parameters["skill_source_id_1"],
                SkillInterpretation.interpreter_version == parameters["interpreter_version_1"],
                SkillInterpretation.checksum == parameters["checksum_1"],
            )
            assert statement.compare(expected_interpretation)
            matches = [
                row
                for row in self.interpretations
                if row.skill_source_id == parameters["skill_source_id_1"]
                and row.interpreter_version == parameters["interpreter_version_1"]
                and row.checksum == parameters["checksum_1"]
            ]
            stage = "interpretation"
        else:
            raise AssertionError(f"Unexpected import model: {entity}")
        assert len(matches) <= 1
        result = PublicationResult(matches[0] if matches else None)
        self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
        self.emit(stage)
        return result

    def add(self, model: SkillSource | SkillInterpretation) -> None:
        """Interpretation 追加より先に FK 親を明示 flush したことを検査する。"""
        if isinstance(model, SkillInterpretation):
            assert model.skill_source_id in self.persisted
        self.rows += (model,)
        self.pending.append(model)
        self.added.append(model)

    async def flush(self) -> None:
        """親/子を保存集合へ移し、固有 row の衝突を成功として隠さない。"""
        source_keys = [(row.organization_id, row.content_hash) for row in self.sources]
        interpretation_keys = [
            (row.skill_source_id, row.interpreter_version, row.checksum)
            for row in self.interpretations
        ]
        assert len(source_keys) == len(set(source_keys))
        assert len(interpretation_keys) == len(set(interpretation_keys))
        self.persisted.update(row.id for row in self.pending)
        self.pending.clear()
        await super().flush()

    def frozen_values(self) -> dict[str, object]:
        """同じ ORM 型の複数行も失わず、資格/外部 bytes を含めずに比較する。"""
        return {
            "rows": deepcopy(
                [
                    (
                        type(row).__name__,
                        {column.key: getattr(row, column.key) for column in row.__table__.columns},
                    )
                    for row in self.rows
                ]
            )
        }

    def service(
        self,
        storage: FileStorage | None = None,
        *,
        storage_configured: bool = True,
    ) -> SkillService:
        """既定は観測 storage。未配線も明示でき、本番 parser/repository は置換しない。"""
        return SkillService(
            cast(async_sessionmaker[AsyncSession], lambda: self),
            CONTRACTS,
            file_storage=(storage or self.storage) if storage_configured else None,
            storage_bucket="skillmind",
        )

    async def operate(self, kind: ImportKind) -> StoredSkillPreview:
        """同じ原入力で同期 inline/upload を呼び、自動 retry を補造しない。"""
        service = self.service(self.storage)
        if kind == "inline":
            return await service.save_inline(access=self.access, files=INLINE_FILES)
        return await service.save_upload(access=self.access, files=UPLOAD_FILES)

    def reset_observations(self) -> None:
        """保存資産と実 storage bytes を残し、次の原要求の観測だけを消す。"""
        super().reset_observations()
        self.added.clear()
        self.storage.attempts.clear()
        self.storage.deletions.clear()
