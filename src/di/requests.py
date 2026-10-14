"""Сборка сценариев в общей сессии каждого HTTP-запроса."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.admin.service import AdminService
from src.application.authentication.service import AuthenticationService
from src.application.calls.service import CallsService
from src.application.dashboard.service import DashboardService
from src.application.dto.authentication import CookieSettings
from src.application.errors.requests import ApplicationError
from src.application.http import HttpApplication
from src.application.media.service import MediaService
from src.application.quality_read.service import QualityReadService
from src.application.reports.service import ReportsService
from src.application.system.service import SystemService
from src.di.ingest_control import ingest_control_application
from src.di.transcription_delivery import transcription_delivery_application
from src.infrastructure.auth.identity import KeycloakIdentityGateway
from src.infrastructure.persistence.admin_operations import PostgresAdminRepository
from src.infrastructure.persistence.calls_operations import PostgresCallsRepository
from src.infrastructure.persistence.dashboard_operations import PostgresDashboardRepository
from src.infrastructure.persistence.export import PostgresExportRepository
from src.infrastructure.persistence.quality_read_operations import PostgresQualityReadRepository
from src.infrastructure.persistence.reports_operations import PostgresReportsRepository
from src.infrastructure.persistence.session import get_speech_ms_sessionmaker
from src.infrastructure.reports.csv import CsvExportRenderer
from src.infrastructure.runtime.system import PostgresSystemProbe
from src.infrastructure.storage.audio_access import StorageAudioReader
from src.infrastructure.storage.recordings import build_storage
from src.presentation.http.deps import system_dep, use_cases_dep
from src.presentation.http.ingest_control import ingest_control_dep
from src.presentation.http.responses import application_error_response
from src.presentation.http.transcription_results import transcription_delivery_dep
from src.presentation.http.validation import internal_validation_error_response
from src.setting.config import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def session_dep(settings: SettingsDep) -> AsyncIterator[AsyncSession]:
    """Сессия на запрос.

    Открывается и закрывается на каждый запрос: держать её дольше значит
    держать соединение из пула, которого у этого кластера считанные единицы.
    """
    async with get_speech_ms_sessionmaker(settings)() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(session_dep)]


def system_application(settings: SettingsDep) -> SystemService:
    """Собрать технические пробы с явной конфигурацией и ключом метрик."""
    return SystemService(PostgresSystemProbe(settings), metrics_key=settings.speech_metrics_key)


def request_application(session: SessionDep, settings: SettingsDep) -> HttpApplication:
    """Внедрить одну сессию во все порты сценариев; соединение закрывает зависимость DI."""
    return HttpApplication(
        media=MediaService(
            StorageAudioReader(session, lambda: build_storage(settings)),
            PostgresExportRepository(session),
            CsvExportRenderer(),
        ),
        authentication=AuthenticationService(
            KeycloakIdentityGateway(session, settings),
            CookieSettings(
                app_env=settings.app_env, session_cookie_secure=settings.session_cookie_secure
            ),
            open_mode=settings.speech_auth_mode.strip().lower() == "open",
        ),
        calls=CallsService(PostgresCallsRepository(session, settings)),
        admin=AdminService(PostgresAdminRepository(session, settings)),
        quality_read=QualityReadService(PostgresQualityReadRepository(session, settings)),
        reports=ReportsService(PostgresReportsRepository(session, settings)),
        dashboard=DashboardService(PostgresDashboardRepository(session, settings)),
    )


def install_dependencies(app: FastAPI) -> None:
    """Привязать абстрактную зависимость роутеров к сборке сценариев этого сервиса."""
    app.dependency_overrides[use_cases_dep] = request_application
    app.dependency_overrides[system_dep] = system_application
    app.dependency_overrides[transcription_delivery_dep] = transcription_delivery_application
    app.dependency_overrides[ingest_control_dep] = ingest_control_application
    app.exception_handler(ApplicationError)(application_error_response)
    app.exception_handler(RequestValidationError)(internal_validation_error_response)
