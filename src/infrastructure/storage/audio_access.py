"""Проксирование записи с одним запросом к хранилищу и закрытием потока."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.calls import AudioStatus
from src.application.dto.media_access import AudioQuery, RecordingStream
from src.application.errors.requests import ApplicationError, ErrorKind
from src.infrastructure.persistence.http_audit import record
from src.infrastructure.storage.recordings import (
    SpeechAudioStorage,
    StorageError,
    StorageNotConfiguredError,
)

_AUDIO_SQL = text(
    "SELECT session_id, audio_status, audio_object_key FROM speech_calls WHERE id = :call_id"
)


async def _audio_row(session: Any, call_id: uuid.UUID) -> dict[str, Any]:
    """Прочитать сведения о доступной записи звонка или вернуть отказ доступа."""
    row = (await session.execute(_AUDIO_SQL, {"call_id": call_id})).mappings().first()
    if row is None:
        raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail="not_found")
    if row["audio_status"] != AudioStatus.STORED.value or not row["audio_object_key"]:
        raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail=f"audio_{row['audio_status']}")
    return dict(row)


class StorageAudioReader:
    """Открыть запись и сохранить факт скачивания в общей сессии запроса."""

    def __init__(
        self, session: AsyncSession, storage_factory: Callable[[], SpeechAudioStorage]
    ) -> None:
        """Принять сессию и фабрику уже настроенного транспорта записи."""
        self._session = session
        self._storage_factory = storage_factory

    async def open(self, query: AudioQuery) -> RecordingStream:
        """Отдать запись потоком, транслируя `Range` (PLY-02).

        Диапазон передаётся хранилищу как есть и возвращается вместе с его
        `Content-Range`: разбирать его самим значило бы завести второй разборщик
        рядом с тем, что уже есть у S3, и однажды разойтись с ним на границе.

        Прослушивание разрешено с правом поиска. `download=1` дополнительно требует
        `download_audio` до чтения метаданных или обращения к хранилищу. Это право
        на явную загрузку, а не технический запрет сохранить поток проигрывателя.

        Тег `<audio>` запрашивает `src` сам и заголовка `Authorization` к нему не
        добавляет, поэтому на этом — и только на этом — пути токен принимается ещё и
        из куки `speech_audio`: её подставляет `AudioCookieMiddleware`. Кука несёт
        **тот же токен**, проверка роли остаётся той же; это способ доставки, а не
        второй путь в обход авторизации (SP-808).
        """
        session = self._session
        call_id = query.call_id
        principal = query.actor
        download = query.download
        row = await _audio_row(session, call_id)
        try:
            storage = self._storage_factory()
        except StorageNotConfiguredError as exc:
            raise ApplicationError(kind=ErrorKind.UNAVAILABLE, detail="not_configured") from exc
        if download:
            await record(
                session,
                principal,
                "audio.download",
                call_id=call_id,
                payload={"session_id": row["session_id"]},
            )
            await session.commit()
        byte_range = query.byte_range
        key = str(row["audio_object_key"])
        stack = AsyncExitStack()
        try:
            upstream = await stack.enter_async_context(storage.open(key, byte_range=byte_range))
        except StorageError as exc:
            await stack.aclose()
            if exc.reason == "not_found":
                raise ApplicationError(
                    kind=ErrorKind.NOT_FOUND, detail="audio_unavailable"
                ) from exc
            raise ApplicationError(
                kind=ErrorKind.DEPENDENCY, detail=f"storage_{exc.reason}"
            ) from exc
        upstream_status = upstream.status_code
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": upstream.headers.get("Content-Type", "audio/mpeg"),
        }
        for passthrough in ("Content-Range", "Content-Length"):
            if passthrough in upstream.headers:
                headers[passthrough] = upstream.headers[passthrough]

        async def _stream() -> AsyncIterator[bytes]:
            """Читать ответ внешнего сервиса порциями с гарантированным закрытием."""
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await stack.aclose()

        if download:
            headers["Content-Disposition"] = f'attachment; filename="{row["session_id"]}.mp3"'
        return RecordingStream(
            chunks=_stream(),
            close=stack.aclose,
            partial=upstream_status == 206,
            range_unsatisfiable=upstream_status == 416,
            content_type=headers["Content-Type"],
            content_range=headers.get("Content-Range"),
            content_length=int(headers["Content-Length"]) if "Content-Length" in headers else None,
            download_name=f"{row['session_id']}.mp3" if download else None,
        )
