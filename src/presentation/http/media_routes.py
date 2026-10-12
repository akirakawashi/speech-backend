"""HTTP-маршруты записи и экспорта; данные и права проверяют сценарии."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from src.application.dto.api import Envelope
from src.application.dto.calls import CallSource, Product, Segment
from src.application.dto.media_access import (
    CSV_BOM as CSV_BOM,
    CSV_DELIMITER as CSV_DELIMITER,
    AudioQuery,
    ExportQuery,
)
from src.presentation.http.audio_cookie import (
    AUDIO_COOKIE_MAX_AGE,
    AUDIO_COOKIE_NAME,
    AUDIO_COOKIE_PATH,
)
from src.presentation.http.auth import PERM_EXPORT, PERM_SEARCH, Principal, require
from src.presentation.http.cookies import cookie_secure
from src.presentation.http.deps import CasesDep
from src.presentation.http.responses import file_response

router = APIRouter(prefix="/api/v1")


@router.get("/calls/{call_id}/audio")
async def call_audio(
    call_id: uuid.UUID,
    request: Request,
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    download: Annotated[int, Query(ge=0, le=1)] = 0,
) -> Response:
    """Передать запрос записи сценарию и вернуть поток с метаданными диапазона."""
    stream = await cases.media.audio(
        AudioQuery(
            actor=principal,
            call_id=call_id,
            byte_range=request.headers.get("Range"),
            download=bool(download),
        )
    )
    headers = {"Accept-Ranges": "bytes", "Content-Type": stream.content_type}
    if stream.content_range is not None:
        headers["Content-Range"] = stream.content_range
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    if stream.download_name is not None:
        headers["Content-Disposition"] = f'attachment; filename="{stream.download_name}"'
    return StreamingResponse(
        stream.chunks,
        status_code=416 if stream.range_unsatisfiable else 206 if stream.partial else 200,
        headers=headers,
        background=BackgroundTask(stream.close),
    )


@router.get("/export")
async def export_csv(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_EXPORT)],
    q: Annotated[str, Query(max_length=500)] = "",
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query(max_length=8)] = None,
    operator_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    segment: Annotated[Segment | None, Query()] = None,
    product: Annotated[Product | None, Query()] = None,
    source: CallSource = CallSource.NCC,
) -> Response:
    """Выгрузить список результатов в CSV (SRC-09).

    Выгружается **тот же** поиск, что видит пользователь, а не отдельный запрос:
    выгрузка, собранная своим SQL, однажды разойдётся с экраном, и спорить будут
    с ней, а не с экраном. Поэтому и `source` здесь тот же, с тем же умолчанием
    `ncc` (S-35): без него выгрузка со вкладки Телфина принесла бы звонки NCC.

    Цитата в файл идёт та же, что на экране. Выгрузка не расширяет доступ: у
    неё своё право, но не свои данные.
    """
    result = await cases.media.export(
        ExportQuery(
            actor=principal,
            q=q,
            date_from=date_from,
            date_to=date_to,
            period=period,
            operator_id=operator_id,
            project_id=project_id,
            node_id=node_id,
            segment=segment,
            product=product,
            source=source,
        )
    )
    return file_response(result)


class AudioTicketResponse(Envelope):
    """Что фронт получает вместо самой куки: она `HttpOnly` и ему не видна."""

    expires_in: int
    issued: bool


@router.post("/session/audio", response_model=AudioTicketResponse)
async def issue_audio_ticket(
    cases: CasesDep,
    request: Request,
    response: Response,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    authorization: Annotated[str | None, Header()] = None,
) -> AudioTicketResponse:
    """Выдать куку, с которой тег `<audio>` дотянется до записи (SP-808).

    Существует ровно потому, что браузер запрашивает `src` сам и заголовков к
    нему не добавляет. В куку кладётся **тот самый токен**, который пришёл в
    заголовке: проверка роли на отдаче аудио остаётся прежней, а значит доступ
    не расширяется. Этим она отличается от подписанной ссылки, которая
    действует сама по себе, кем бы ни была открыта.

    Четыре ограничения, и каждое закрывает свой способ ею воспользоваться:

    * `HttpOnly` — скрипт на странице её не прочитает, то есть XSS не вынесет
      токен наружу тем же путём;
    * `SameSite=strict` — браузер не отправит её по запросу с чужого сайта,
      поэтому `<audio src="…">` на постороннем ресурсе не сработает;
    * `Path=/api/v1/calls` — даже утекшая, она не даст ни поиска, ни админки,
      ни раскрытия масок: браузер её туда просто не пошлёт;
    * короткий срок и никакого самопродления — фронт перевыпускает её вместе с
      обновлением токена.

    `Secure` ставится по объявленному окружению (`src/presentation/http/cookies.py`), а не
    по схеме запроса. До 2026-09-21 здесь стояло `request.url.scheme ==
    "https"`, и за обратным прокси эта кука не получала `Secure` НИКОГДА:
    `X-Forwarded-Proto` не читался вовсе, а до сервиса запрос всегда доходит
    по `http`. При умолчании `SESSION_COOKIE_SECURE=auto` послабление осталось
    только для `APP_ENV=local`: на `http://127.0.0.1:3000` браузер куку с
    `Secure` молча выбросит, и разработка сломалась бы без единого сообщения.
    Снять флаг вне local можно только объявив `SESSION_COOKIE_SECURE=never`, и
    в продуктиве с этим значением процесс не стартует вовсе.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return AudioTicketResponse(expires_in=0, issued=False)
    response.set_cookie(
        key=AUDIO_COOKIE_NAME,
        value=authorization.split(" ", 1)[1].strip(),
        max_age=AUDIO_COOKIE_MAX_AGE,
        path=AUDIO_COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(cases.authentication.cookies, request),
    )
    _ = principal
    return AudioTicketResponse(expires_in=AUDIO_COOKIE_MAX_AGE, issued=True)


@router.delete("/session/audio", response_model=AudioTicketResponse)
async def revoke_audio_ticket(response: Response) -> AudioTicketResponse:
    """Убрать куку при выходе.

    Без права: удалять собственную куку может кто угодно, а требовать для
    выхода действующий токен значит не дать выйти тому, у кого он протух, —
    то есть именно тому, кому это нужнее всего.

    `samesite` и `httponly` повторяют выдачу, чтобы два объявления одной куки
    не расходились на ровном месте. На само гашение они не влияют: кука
    опознаётся тройкой имя+домен+путь, и это проверено прогоном через клиент —
    `tests/test_speech_audio_cookie.py::test_the_cookie_actually_leaves_the_jar`.

    `Secure` здесь НЕ повторяется, и это не недосмотр: по RFC 6265bis куку с
    `Secure`, пришедшую с незащищённого источника, браузер отбрасывает.
    Поставив его, мы сделали бы гашение невыполнимым ровно там, где оно нужнее
    всего, — на отладочном входе по `http` мимо корпоративного прокси.
    """
    response.delete_cookie(
        key=AUDIO_COOKIE_NAME, path=AUDIO_COOKIE_PATH, httponly=True, samesite="strict"
    )
    return AudioTicketResponse(expires_in=0, issued=False)
