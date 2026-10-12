"""Кука для тега `<audio>` — ровно на одном пути (SP-808).

Браузер запрашивает `src` сам и заголовка `Authorization` к нему не добавляет,
поэтому в режиме `keycloak` прослушивание иначе невозможно вовсе. Отвергнутые
обходы и почему: подписанная ссылка действует сама по себе и тем обходит
проверку роли; токен в адресе уезжает в историю браузера и логи прокси;
загрузка через `fetch` в `blob:` убивает перемотку, ради которой сервис и
транслирует `Range`.

Куку подставляет **промежуточный слой**, а не отдельная зависимость маршрута, и
это выбор с историей. Первая редакция завела для аудио свою зависимость
`principal_for_media`. Тесты, подменявшие `current_principal`, перестали
подменять что-либо для аудио — и два из них молча начали проверять не то, что
написано в их названии: «читателю нельзя скачивать» отвечал `503` вместо `403`,
потому что подмена не действовала и маршрут шёл в настоящий разбор. Вторая
зависимость для того же вопроса — это второе место, где ответ может разойтись.

Здесь же зависимость по-прежнему одна на весь сервис: слой лишь дописывает
заголовок из куки, если заголовка нет. Проверка токена и роли — та же самая, а
значит доступ не расширяется. Это принципиальное отличие от подписанной ссылки.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

#: Имя куки. Общее с выдачей и с фронтом.
AUDIO_COOKIE_NAME: Final[str] = "speech_audio"

#: Путь куки. Браузер не отправит её никуда, кроме записей разговоров: даже
#: утекшая, она не даст ни поиска, ни админки, ни раскрытия масок.
AUDIO_COOKIE_PATH: Final[str] = "/api/v1/calls"

#: Потолок жизни куки. Меньше обычного срока токена и не продлевается сам:
#: фронт перевыпускает её вместе с обновлением токена.
AUDIO_COOKIE_MAX_AGE: Final[int] = 900

#: Единственный путь, на котором кука принимается. Не префикс `/api/v1/calls`:
#: под ним живёт и раскрытие масок, и карточка звонка, и принимать там куку
#: значило бы отдать по ней исходный текст разговора.
AUDIO_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^/api/v1/calls/[0-9a-fA-F-]{36}/audio/?$")


def accepts_audio_cookie(method: str, path: str) -> bool:
    """Тот ли это запрос, для которого кука вообще предназначена."""
    return method.upper() == "GET" and bool(AUDIO_PATH_PATTERN.match(path))


class AudioCookieMiddleware(BaseHTTPMiddleware):
    """Дописать `Authorization` из куки — только для отдачи записи.

    Заголовок сильнее куки: если приложение прислало токен явно, кука его не
    переопределяет. Обратный порядок означал бы, что просроченная кука отменяет
    свежий токен, и разлогин выглядел бы как случайный.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Обработать HTTP-запрос и передать управление следующему обработчику."""
        if (
            accepts_audio_cookie(request.method, request.url.path)
            and "authorization" not in request.headers
        ):
            ticket = request.cookies.get(AUDIO_COOKIE_NAME)
            if ticket:
                # Заголовки в ASGI — список пар байтов; правится копия области
                # запроса, а не глобальное состояние.
                headers = list(request.scope["headers"])
                headers.append((b"authorization", f"Bearer {ticket}".encode()))
                request.scope["headers"] = headers
        return await call_next(request)
