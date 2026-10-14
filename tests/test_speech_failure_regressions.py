"""Безопасные SQL-ошибки и контракт отказа MinIO без живой сети и базы."""

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import StatementError

from src.infrastructure.persistence import session as db_session
from src.infrastructure.storage.recordings import StorageError

RAW_TEXT = "SYNTHETIC_RAW_TRANSCRIPT_DO_NOT_LOG_68419"


@pytest.fixture(autouse=True)
def forbid_live_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline regression attempted a real HTTP or database connection")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(Engine, "raw_connection", forbidden)


def test_storage_error_defaults_remain_backward_compatible() -> None:
    error = StorageError("synthetic failure")
    assert str(error) == "synthetic failure"
    assert error.reason == "provider_error"
    assert error.status_code is None
    classified = StorageError("synthetic missing object", reason="not_found", status_code=404)
    assert classified.reason == "not_found"
    assert classified.status_code == 404


async def test_speech_engine_hides_bound_parameters_without_connecting(monkeypatch) -> None:
    settings = SimpleNamespace(
        db_pool_size=1,
        db_max_overflow=0,
        db_pool_timeout_seconds=1,
        db_pool_recycle_seconds=60,
        db_write_trace_enabled=False,
    )
    # Bypass only the cache: run the real factory without replacing a cached
    # engine or leaving a synthetic one behind for another test.
    engine = db_session._speech_ms_engine.__wrapped__(
        "postgresql+asyncpg://synthetic:synthetic@database.invalid/offline_speech",
        settings.db_pool_size,
        settings.db_max_overflow,
        settings.db_pool_timeout_seconds,
        settings.db_pool_recycle_seconds,
        settings.db_write_trace_enabled,
    )
    try:
        assert engine.sync_engine.hide_parameters is True
        error = StatementError(
            "synthetic failure",
            "INSERT INTO speech_transcript_segments (text_raw) VALUES (:text_raw)",
            {"text_raw": RAW_TEXT},
            RuntimeError("synthetic driver failure"),
            hide_parameters=engine.sync_engine.hide_parameters,
        )
        assert error.params == {"text_raw": RAW_TEXT}, "redaction must not alter stored data"
        assert RAW_TEXT not in str(error)
        assert RAW_TEXT not in repr(error)
        assert "SQL parameters hidden" in str(error)
    finally:
        await engine.dispose()
