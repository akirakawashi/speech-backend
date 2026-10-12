"""Служебный HTTP-маршрут работает без БД и скрыт от публичного контракта."""

import pytest
from fastapi.testclient import TestClient

from src.application.dto.model_execution import PreparedModelRequest
from src.di.http import create_app
from src.setting.config import Settings
from tests.test_model_preparation import _inputs

HEADERS = {"Authorization": "Bearer worker-secret"}
COMMAND = {"contract_version": "1.0", "scenario": "analysis", "inputs": _inputs("analysis")}


def _client(token: str = "worker-secret") -> TestClient:
    """Создать настоящее приложение с недоступной БД: подготовка её не использует."""
    return TestClient(
        create_app(
            Settings(
                _env_file=None,
                app_env="local",
                speech_auth_mode="open",
                speech_ms_database_url="postgresql+asyncpg://unreachable/unreachable",
                speech_worker_api_token=token,
            )
        )
    )


def test_private_route_returns_versioned_model_request_without_database():
    """Корректный Bearer открывает подготовку DTO, соединение с БД не требуется."""
    with _client() as client:
        response = client.post("/internal/v1/model-requests", json=COMMAND, headers=HEADERS)
    assert response.status_code == 200
    result = PreparedModelRequest.model_validate(response.json())
    assert (result.contract_version, result.scenario, result.model) == (
        "1.0",
        "analysis",
        "qwen3.8-27b-fast",
    )
    assert "worker-secret" not in response.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_private_route_rejects_missing_or_incorrect_token(headers):
    """Даже режим open для браузера не отключает служебную авторизацию."""
    with _client() as client:
        response = client.post("/internal/v1/model-requests", json=COMMAND, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_unconfigured_private_route_returns_503():
    """Пустой секрет требует настройки владельцем и не открывает маршрут."""
    with _client("") as client:
        assert client.post("/internal/v1/model-requests", json=COMMAND).status_code == 503


@pytest.mark.parametrize(
    "change",
    [
        {"contract_version": "2.0"},
        {"scenario": "unknown"},
        {"extra": "sensitive transcript"},
        {"inputs": "sensitive transcript"},
        {"inputs": {"segments": "sensitive transcript"}},
    ],
)
def test_invalid_request_never_echoes_conversation(change):
    """Ошибки Pydantic и прикладной проверки сохраняют только описание отказа."""
    with _client() as client:
        response = client.post(
            "/internal/v1/model-requests", json={**COMMAND, **change}, headers=HEADERS
        )
    assert response.status_code == 422
    assert "sensitive transcript" not in response.text


def test_private_route_is_absent_from_browser_openapi():
    """Контракт фронтенда не получает служебный маршрут и его DTO."""
    schema = _client().app.openapi()
    assert not any(path.startswith("/internal/") for path in schema["paths"])
    assert "PreparedModelRequest" not in schema["components"]["schemas"]
