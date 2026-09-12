"""FastAPI application の生成と lifecycle 管理を行う。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette._utils import get_route_path

from skillmind import __version__
from skillmind.api.health import router as health_router
from skillmind.api.login_protection import login_protection_problem
from skillmind.api.problems import (
    ProblemException,
    http_exception_handler,
    problem_exception_handler,
    validation_exception_handler,
)
from skillmind.api.routes import router as api_router
from skillmind.artifacts.service import ArtifactService
from skillmind.auth.login_protection import LoginProtectionUnavailableError, LoginRateLimitedError
from skillmind.auth.service import AuthService
from skillmind.compositions import CompositionService
from skillmind.core.logging import configure_logging, log_event
from skillmind.core.secret_crypto import load_secret_cipher
from skillmind.core.settings import get_settings
from skillmind.db.resources import (
    create_database_engine,
    create_redis_client,
    create_session_factory,
)
from skillmind.documents.library import (
    DOCUMENT_LIBRARY_PROVIDER,
    DOCUMENT_WRITE_CAPABILITY,
    configured_document_library,
)
from skillmind.documents.resource_catalog import DocumentResourceCatalog
from skillmind.documents.service import DocumentService
from skillmind.documents.snapshot import (
    DOCUMENT_CAPABILITIES,
    DOCUMENT_PROVIDER,
)
from skillmind.effects.reconciliation_request_service import ReconciliationRequestService
from skillmind.effects.release import configured_execution_features
from skillmind.effects.service import EffectService
from skillmind.evaluations import EvaluationService
from skillmind.integrations import (
    INSTALLED_PROVIDER_CAPABILITIES,
    IntegrationService,
)
from skillmind.integrations.resource_catalog import (
    CompositeProjectResourceCatalog,
    IntegrationResourceCatalog,
)
from skillmind.projects import ProjectService
from skillmind.runs.service import RunService
from skillmind.schedules import ScheduleService
from skillmind.skills import SkillService
from skillmind.skills.document_prerequisites import DOCUMENT_READINESS_CAPABILITY
from skillmind.skills.wiring import build_skill_interpreter
from skillmind.storage.factory import create_document_upload_limits, create_file_storage
from skillmind.users.service import UserService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application 全体で共有する DB/Redis resource を生成・破棄する。"""

    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings
    app.state.database_engine = create_database_engine(settings)
    app.state.database_session_factory = create_session_factory(app.state.database_engine)
    app.state.redis = create_redis_client(settings)
    app.state.auth_service = AuthService(
        app.state.database_session_factory,
        app.state.redis,
        login_csrf_ttl_seconds=settings.auth_login_csrf_ttl_seconds,
        session_idle_minutes=settings.auth_session_idle_minutes,
        session_absolute_hours=settings.auth_session_absolute_hours,
        admin_session_absolute_hours=settings.auth_admin_session_absolute_hours,
        login_attempts_per_minute=settings.auth_login_attempts_per_minute,
        login_account_attempts_per_minute=settings.auth_login_account_attempts_per_minute,
        login_source_requests_per_minute=settings.auth_login_source_requests_per_minute,
        login_protection_timeout_seconds=settings.auth_login_protection_timeout_seconds,
    )
    app.state.project_service = ProjectService(app.state.database_session_factory)
    app.state.user_service = UserService(app.state.database_session_factory)
    # MANAGED SecretReference 封入用の KEK cipher。未設定なら MANAGED 作成は fail closed。
    app.state.integration_service = IntegrationService(
        app.state.database_session_factory,
        secret_cipher=load_secret_cipher(settings.managed_secret_kek),
    )
    # 遅延接続の MinIO/S3 client。Skill upload と文書層が同じ保存先を共有する。
    app.state.file_storage = create_file_storage(settings)
    document_library_target = configured_document_library(
        app.state.file_storage, bucket=settings.object_storage_bucket
    )
    features = configured_execution_features(settings)
    app.state.run_service = RunService(
        app.state.database_session_factory,
        deferred_features_enabled=features.deferred,
        database_writes_enabled=features.database_writes,
        document_writes_enabled=features.document_writes,
        document_library_target=document_library_target,
    )
    app.state.artifact_service = ArtifactService(app.state.database_session_factory)
    app.state.evaluation_service = EvaluationService(app.state.database_session_factory)
    app.state.effect_service = EffectService(
        app.state.database_session_factory,
        document_library_target=document_library_target,
        execution_features=features,
    )
    app.state.reconciliation_requests = ReconciliationRequestService(
        app.state.database_session_factory, document_library_target=document_library_target,
    )
    interpreter, catalog, identity, default_model = build_skill_interpreter(settings)
    app.state.skill_service = SkillService(
        app.state.database_session_factory,
        settings.contracts_dir,
        interpreter=interpreter,
        capability_catalog=catalog,
        interpreter_identity=identity,
        default_model=default_model,
        file_storage=app.state.file_storage,
        storage_bucket=settings.object_storage_bucket,
        resource_catalog=CompositeProjectResourceCatalog(
            (
                DocumentResourceCatalog(
                    app.state.database_session_factory, library_target=document_library_target
                ),
                IntegrationResourceCatalog(app.state.database_session_factory),
            )
        ),
        registered_write_capabilities=features.write_capabilities,
        # Integration Provider の installed 索引に、Integration 外だが常に配線済みの
        # document Provider を合流させる (計画 §19 W1)。就緒度が候補の provider を実行可能性まで
        # 検査できるようにする唯一の注入点。
        installed_provider_capabilities={
            **INSTALLED_PROVIDER_CAPABILITIES,
            DOCUMENT_READINESS_CAPABILITY: frozenset({"platform"}),
            **{item: frozenset({DOCUMENT_PROVIDER}) for item in DOCUMENT_CAPABILITIES},
            **({DOCUMENT_WRITE_CAPABILITY: frozenset({DOCUMENT_LIBRARY_PROVIDER})}
               if features.document_writes and document_library_target is not None else {}),
        },
    )
    app.state.document_service = DocumentService(
        app.state.database_session_factory,
        file_storage=app.state.file_storage,
        limits=create_document_upload_limits(settings),
    )
    app.state.composition_service = CompositionService(app.state.database_session_factory)
    # 保存時検証は発火時と同じ SkillService/RunService を通す。API 側だけ緩い検証にすると
    # 「保存できたのに最初の発火で必ず落ちる」schedule が作れてしまう (計画 §22)。
    app.state.schedule_service = ScheduleService(
        app.state.database_session_factory,
        skill_service=app.state.skill_service,
        run_service=app.state.run_service,
    )
    yield
    # Application 終了時に共有 connection pool を閉じ、再配備時の resource leak を防ぐ。
    await app.state.redis.aclose()
    await app.state.database_engine.dispose()


def create_app() -> FastAPI:
    """設定済み router、middleware、exception handler を持つ API を構築する。"""

    settings = get_settings()
    app = FastAPI(
        title="Skillmind API",
        version=__version__,
        # Traefik が外部 context path を除去するため、URL 生成時だけ元の prefix を復元する。
        root_path=settings.context_path,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """信頼しない header を監査 identity にせず、Server の UUID を全応答で共有する。"""

        request_id = str(uuid4())
        request.state.request_id = request_id
        started = monotonic()
        is_login_entry = get_route_path(request.scope).rstrip("/") in login_entry_paths
        response: Response
        try:
            try:
                if is_login_entry:
                    # Body/Origin の検証前に数える。任意の forwarded header は読まない。
                    service: AuthService = request.app.state.auth_service
                    request.state.login_admission = await service.begin_login(
                        request.client.host if request.client is not None else "unknown",
                    )
            except (LoginRateLimitedError, LoginProtectionUnavailableError) as error:
                response = await problem_exception_handler(request, login_protection_problem(error))
            else:
                response = await call_next(request)
        except Exception:
            log_event(
                logger,
                logging.ERROR,
                "api.request.failed",
                trace_id=request_id,
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status_code=500,
                duration_ms=round((monotonic() - started) * 1000, 2),
            )
            raise
        response.headers["X-Request-ID"] = request_id
        matched_route = request.scope.get("route")
        if is_login_entry or (isinstance(matched_route, APIRoute) and "auth" in matched_route.tags):
            # 成功だけでなく、validation/認証拒否も例外処理後の同じ境界で cache を禁じる。
            response.headers["Cache-Control"] = "no-store"
        log_event(
            logger,
            logging.INFO,
            "api.request.completed",
            trace_id=request_id,
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((monotonic() - started) * 1000, 2),
        )
        return response

    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(ProblemException, problem_exception_handler)
    app.include_router(health_router)
    app.include_router(api_router)
    # include_router の内部表現に依存せず、公開の逆引きから内部 path を取得する。
    # 名前が失われたら起動を失敗させ、保護対象が空のまま受付を始めない。
    login_entry_paths = frozenset(
        str(app.url_path_for(name)) for name in ("login", "login_context", "change_own_password")
    )
    return app


app = create_app()
