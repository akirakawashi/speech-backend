from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.application.dto.transcription import TranscriptionError
from src.di.transcription import transcribe_audio
from src.presentation.uploads.audio import AudioUpload
from src.setting.config import Settings


def make_settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "_env_file": None,
        "transcription_enabled": True,
        "transcription_provider": "openai_compatible",
        "transcription_base_url": "http://host.docker.internal:8011/v1",
        "transcription_model": "openai/whisper-large-v3",
        "transcription_timeout_seconds": 5.0,
    }
    payload.update(overrides)
    return Settings(**payload)  # type: ignore[arg-type]


def make_audio(**overrides: object) -> AudioUpload:
    payload: dict[str, object] = {
        "filename": "call.mp3",
        "mime_type": "audio/mpeg",
        "size_bytes": 11,
        "content": b"voice-bytes",
        "audio_format": "mp3",
    }
    payload.update(overrides)
    return AudioUpload(**payload)  # type: ignore[arg-type]


def make_client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_transcribe_audio_returns_result_and_normalized_audit_payload() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "привет мир", "duration": 4.0})

    settings = make_settings()
    async with make_client(handler) as client:
        result, outcome = await transcribe_audio(
            settings=settings,
            audio=make_audio(),
            language="ru",
            request_id="req-1",
            http_client=client,
        )

    assert result.text == "привет мир"
    assert outcome.status == "success"

    payload = outcome.audit_payload()
    assert payload["adapter"] == "speech_transcription"
    assert payload["status"] == "success"
    assert payload["request_id"] == "req-1"
    assert payload["request_attempts"] == 1
    assert payload["retry_count"] == 0
    assert payload["max_attempts_per_request"] == 1
    assert payload["total_timeout_ms"] == 5000
    assert payload["transcription_model"] == "openai/whisper-large-v3"
    assert payload["target"]["configured"] is True
    assert payload["target"]["network_zone"] == "self_hosted"

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "host.docker.internal" not in serialized
    assert "привет мир" not in serialized
    assert "voice-bytes" not in serialized


async def test_transcribe_audio_extra_adds_caller_fields_without_colliding() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "ok"})

    async with make_client(handler) as client:
        _, outcome = await transcribe_audio(
            settings=make_settings(),
            audio=make_audio(),
            language="ru",
            http_client=client,
        )

    payload = outcome.audit_payload(
        extra={"source_agent_id": "voice-ticket-draft", "mime_type": "audio/mpeg", "size_bytes": 11}
    )
    assert payload["source_agent_id"] == "voice-ticket-draft"

    with pytest.raises(ValueError):
        outcome.audit_payload(extra={"adapter": "not-allowed"})


async def test_transcribe_audio_raises_not_configured_without_touching_the_network() -> None:
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"text": "ok"})

    settings = make_settings(transcription_enabled=False)
    async with make_client(handler) as client:
        with pytest.raises(TranscriptionError) as exc_info:
            await transcribe_audio(
                settings=settings,
                audio=make_audio(),
                language="ru",
                http_client=client,
            )

    assert called is False
    assert exc_info.value.reason == "not_configured"
    assert exc_info.value.outcome is not None
    assert exc_info.value.outcome.audit_payload()["target"]["configured"] is False


async def test_transcribe_audio_normalizes_provider_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with make_client(handler) as client:
        with pytest.raises(TranscriptionError) as exc_info:
            await transcribe_audio(
                settings=make_settings(),
                audio=make_audio(),
                language="ru",
                http_client=client,
            )

    outcome = exc_info.value.outcome
    assert outcome is not None
    assert outcome.status == "error"
    assert outcome.reason == "provider_error"
    payload = outcome.audit_payload()
    assert payload["provider_status_code"] == 500
    assert "boom" not in json.dumps(payload)


async def test_transcribe_audio_telemetry_reflects_a_single_attempt() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "ok"})

    async with make_client(handler) as client:
        _, outcome = await transcribe_audio(
            settings=make_settings(transcription_timeout_seconds=12.0),
            audio=make_audio(),
            language="ru",
            http_client=client,
        )

    assert outcome.telemetry.request_attempts == 1
    assert outcome.telemetry.retry_count == 0
    assert outcome.telemetry.max_attempts == 1
    assert outcome.telemetry.total_timeout_ms == 12000


async def test_transcribe_audio_times_out_when_budget_is_exhausted() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, json={"text": "ok"})

    async with make_client(handler) as client:
        with pytest.raises(TranscriptionError) as exc_info:
            await transcribe_audio(
                settings=make_settings(transcription_timeout_seconds=0.05),
                audio=make_audio(),
                language="ru",
                http_client=client,
            )

    assert exc_info.value.reason == "timeout"


async def test_transcribe_audio_carries_truncation_check_into_audit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "ok", "duration": 29.9})

    async with make_client(handler) as client:
        result, outcome = await transcribe_audio(
            settings=make_settings(transcription_audio_window_seconds=30.0),
            audio=make_audio(),
            language="ru",
            http_client=client,
        )

    assert result.truncation_check == "suspected"
    assert outcome.audit_payload()["truncation_check"] == "suspected"
