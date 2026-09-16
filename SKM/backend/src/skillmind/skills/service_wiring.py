"""API・実行 Worker・保守 Worker の SkillService 設定を一つの入口で組み立てる。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from skillmind.documents.library import DOCUMENT_LIBRARY_PROVIDER, DOCUMENT_WRITE_CAPABILITY
from skillmind.documents.resource_catalog import DocumentResourceCatalog
from skillmind.documents.snapshot import DOCUMENT_CAPABILITIES, DOCUMENT_PROVIDER
from skillmind.effects.release import configured_execution_features
from skillmind.integrations import INSTALLED_PROVIDER_CAPABILITIES
from skillmind.integrations.resource_catalog import (
    CompositeProjectResourceCatalog,
    IntegrationResourceCatalog,
)
from skillmind.skills.document_prerequisites import DOCUMENT_READINESS_CAPABILITY
from skillmind.skills.service import SkillService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from skillmind.core.settings import Settings
    from skillmind.documents.library import DocumentLibraryTarget
    from skillmind.skills.interpreter import (
        CapabilityCatalogSnapshot,
        InterpreterSystemSkillIdentity,
    )
    from skillmind.skills.interpreter_execution import SkillInterpreter
    from skillmind.storage import FileStorage


def build_skill_service(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    file_storage: FileStorage | None,
    document_library_target: DocumentLibraryTarget | None,
    interpreter_components: tuple[
        SkillInterpreter | None,
        CapabilityCatalogSnapshot | None,
        InterpreterSystemSkillIdentity | None,
        str | None,
    ] | None = None,
) -> SkillService:
    """同じ配備設定から同じ宣言・Provider 索引を注入し、外部接続は開始しない。

    Interpreter の未設定は導入/既発行 task の読取を止めない。保守 process はモデルを
    組み立てず、従来の公開版・Run 入力検証だけを使う。新しい実行門禁は追加しない。
    """

    features = configured_execution_features(settings)
    interpreter, catalog, identity, model = (
        interpreter_components if interpreter_components is not None else (None, None, None, None)
    )
    return SkillService(
        session_factory,
        settings.contracts_dir,
        interpreter=interpreter,
        capability_catalog=catalog,
        interpreter_identity=identity,
        default_model=model,
        file_storage=file_storage,
        storage_bucket=settings.object_storage_bucket,
        resource_catalog=CompositeProjectResourceCatalog(
            (
                DocumentResourceCatalog(session_factory, library_target=document_library_target),
                IntegrationResourceCatalog(session_factory),
            )
        ),
        registered_write_capabilities=features.write_capabilities,
        # API の就緒度と Worker の capability 集合を同じ実装から作る。登録は実行許可ではない。
        installed_provider_capabilities={
            **{
                capability: frozenset(
                    provider for provider in providers
                    if features.provider_enabled(capability, provider)
                )
                for capability, providers in INSTALLED_PROVIDER_CAPABILITIES.items()
            },
            DOCUMENT_READINESS_CAPABILITY: frozenset({"platform"}),
            **{item: frozenset({DOCUMENT_PROVIDER}) for item in DOCUMENT_CAPABILITIES},
            **(
                {DOCUMENT_WRITE_CAPABILITY: frozenset({DOCUMENT_LIBRARY_PROVIDER})}
                if features.document_writes and document_library_target is not None else {}
            ),
        },
    )
