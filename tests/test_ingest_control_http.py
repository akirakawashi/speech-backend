"""Служебный доступ ingest проверяется до базы, а ошибки не отражают локаторы записей."""

from uuid import uuid4

import httpx
import pytest

from src.di.http import create_app
from src.setting.config import Settings


@pytest.mark.parametrize("route", ["prepare", "resolve"])
@pytest.mark.parametrize(
    "token,authorization,status",
    [
        ("", None, 503),
        ("service-key", None, 401),
        ("service-key", "Bearer wrong", 401),
    ],
)
async def test_ingest_authorization_precedes_database_creation(route, token, authorization, status):
    """Настроенной базы нет; неверный ключ всё равно отклоняется без обращения к ней."""
    app = create_app(
        Settings(
            _env_file=None, app_env="local", speech_auth_mode="open", speech_worker_api_token=token
        )
    )
    claim = {
        "call_id": str(uuid4()),
        "job_id": str(uuid4()),
        "claim_token": str(uuid4()),
        "worker_id": "test-worker",
        "attempt": 1,
    }
    body = claim if route == "prepare" else {"claim": claim, "observation": {"outcome": "missing"}}
    assert not any(path.startswith("/internal/") for path in app.openapi()["paths"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as client:
        response = await client.post(
            f"/internal/v1/ingest/{route}",
            json=body,
            headers={"Authorization": authorization} if authorization else {},
        )
    assert response.status_code == status
    assert "service-key" not in response.text and "wrong" not in response.text
