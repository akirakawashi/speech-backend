"""Точка входа `speech-ms`.

Пользовательские маршруты обслуживают браузер через `speech-edge`; вход
проверяет Keycloak. Служебная подготовка заданий для воркера имеет отдельный
Bearer-токен и не использует пользовательскую сессию.

Документация Swagger включена только вне продуктива: фронтенд генерирует типы из
`openapi.json`, и разработчику удобно открыть `/docs` на стенде, но публиковать
описание API наружу незачем.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.presentation.http.admin_routes import router as admin_router
from src.presentation.http.api import router
from src.presentation.http.audio_cookie import AudioCookieMiddleware
from src.presentation.http.auth_routes import router as auth_router
from src.presentation.http.dashboard_routes import router as dashboard_router
from src.presentation.http.media_routes import router as media_router
from src.presentation.http.model_requests import router as model_requests_router
from src.presentation.http.quality_routes import router as quality_router
from src.presentation.http.report_routes import router as report_router
from src.presentation.http.routes import router as api_v1_router
from src.presentation.middleware.request_id import RequestIdMiddleware
from src.setting.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Собрать ASGI-приложение по настройкам, подключить роутеры и зависимости."""
    resolved = settings or get_settings()
    # Отказ на старте, а не при первом запросе: режим `open` вне `local`/`alpha`
    # — это открытый доступ к записям разговоров, и обнаруживать это по
    # факту обращения поздно.
    resolved.validate_speech_runtime()

    docs_enabled = resolved.app_env.strip().lower() in {"local", "alpha"}
    app = FastAPI(
        title="Aicistent Speech Analytics",
        version="1.0.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.add_middleware(RequestIdMiddleware)
    # Только для отдачи записи: тег `<audio>` заголовков не шлёт (SP-808).
    app.add_middleware(AudioCookieMiddleware)
    if resolved.backend_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.backend_cors_origins),
            allow_credentials=False,
            allow_methods=list(resolved.cors_allow_methods),
            allow_headers=list(resolved.cors_allow_headers),
        )
    from src.di.requests import install_dependencies

    install_dependencies(app)
    app.dependency_overrides[get_settings] = lambda: resolved
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(api_v1_router)
    app.include_router(media_router)
    app.include_router(admin_router)
    app.include_router(report_router)
    app.include_router(quality_router)
    app.include_router(dashboard_router)
    app.include_router(model_requests_router)
    return app
