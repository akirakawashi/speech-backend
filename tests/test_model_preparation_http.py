"""HTTP API не передаёт воркеру модельные запросы или управление оценкой."""

import httpx
import pytest
from pydantic import ValidationError

from src.application.dto.maintenance import MaintenanceRequest
from src.di.http import create_app
from src.setting.config import Settings


@pytest.mark.parametrize("route", ["model-requests", "quality-jobs", "quality-steps"])
@pytest.mark.parametrize("token", ["", "worker-secret"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer worker-secret"])
async def test_retired_model_routes_are_absent_without_database_access(route, token, authorization):
    """Даже корректный ключ не открывает LLM-контракт; вход и секреты не отражаются."""
    app = create_app(
        Settings(
            _env_file=None,
            app_env="local",
            speech_auth_mode="open",
            speech_worker_api_token=token,
            speech_ms_database_url="postgresql+asyncpg://unreachable.invalid/private-database",
        )
    )
    assert not any(path.startswith("/internal/") for path in app.openapi()["paths"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as client:
        response = await client.post(
            f"/internal/v1/{route}",
            json={"contract_version": "private-input", "inputs": {"text": "PRIVATE_TRANSCRIPT"}},
            headers={"Authorization": authorization} if authorization else {},
        )
    assert response.status_code == 404
    assert "PRIVATE_TRANSCRIPT" not in response.text
    assert "private-input" not in response.text and "worker-secret" not in response.text


def test_worker_plan_cannot_request_backend_quality_schedule():
    """Оценку ставит локальный сценарий бэкенда; воркер запрашивает только источники."""
    with pytest.raises(ValidationError):
        MaintenanceRequest(action="quality")
