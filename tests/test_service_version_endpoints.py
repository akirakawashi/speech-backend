"""the deployable can say what it is and what it speaks.

These are not smoke tests for a getter. Each assertion below corresponds to a
way a rolling deployment lies to its operator:

* a service that reports no commit is indistinguishable from a stale one, so an
  unset build arg must read `unknown` rather than being omitted or invented;
* a service that reports the *wrong* name lets a mislabelled image hide behind a
  correct commit, so the app is checked against its own name;
* a contract version that is hardcoded in the response can drift from the one
  the code serialises, so the expected value is imported from the same constant
  the wire format uses -- if someone bumps the contract and forgets the
  endpoint, that is a failure here, which is the point.

Carried over from the monorepo, where one module checked every service of the
platform; only the `speech-ms` case lives here.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.domain.calls.contracts import SPEECH_API_CONTRACT_VERSION
from src.infrastructure.runtime.build_info import BUILD_TIME_ENV, COMMIT_ENV, UNKNOWN
from src.setting.config import Settings

SERVICE = "speech-ms"


def _app() -> FastAPI:
    from src.di.http import create_app

    return create_app(Settings())


async def _version(app: FastAPI) -> dict[str, Any]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/version")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def test_version_reports_its_own_service_and_contracts(monkeypatch) -> None:
    monkeypatch.setenv(COMMIT_ENV, "abc1234")
    monkeypatch.setenv(BUILD_TIME_ENV, "2026-08-17T12:00:00Z")

    payload = await _version(_app())

    assert payload["service"] == SERVICE
    assert payload["commit"] == "abc1234"
    assert payload["build_time"] == "2026-08-17T12:00:00Z"
    assert payload["contracts"] == {"speech.api": SPEECH_API_CONTRACT_VERSION}


async def test_version_says_unknown_rather_than_guessing(monkeypatch) -> None:
    """An image built outside the release pipeline must admit it."""
    monkeypatch.delenv(COMMIT_ENV, raising=False)
    monkeypatch.delenv(BUILD_TIME_ENV, raising=False)

    payload = await _version(_app())

    assert payload["commit"] == UNKNOWN
    assert payload["build_time"] == UNKNOWN
