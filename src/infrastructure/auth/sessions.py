"""Сессии браузера: кука снаружи, строка внутри.

Клиент Keycloak, выданный проекту, — конфиденциальный, и менять его не будут
(ADR-SPEECH-0002). Браузер поэтому не может обменять код на токен, и весь вход
переезжает на сервер: секрет держит сервис, браузеру достаётся непрозрачная
кука.

**Таблица, а не зашифрованная кука.** Кука не требует ни миграции, ни выборки
на запрос, но её нельзя отозвать: кто держит — ходит до истечения срока. За
этой дверью лежат записи разговоров реальных клиентов, и «подождите, пока
протухнет» тут не ответ. Строка гасится одним `UPDATE`.

**В `id` лежит хеш значения куки, а не само значение.** Утечка дампа тогда не
даёт живых сессий: по хешу куку не восстановить. Разница бесплатная — одно
хеширование на запрос.

**Токен доступа не хранится вовсе.** После входа он не нужен: роли разобраны и
лежат в строке. Хранится только `refresh_token` — им продлевается сессия и
перечитываются группы. Хранить ненужное — способ сделать базу интересной для
кражи.

SQL здесь сырой (`text()`), как и во всём Speech Analytics: гейт
`ownership-matrix` умеет читать эти запросы, а смешение двух стилей в одном
сервисе даёт два места, где одна и та же таблица описана по-разному.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

#: Имя куки сессии. Общее с фронтом — он её не читает (`HttpOnly`), но имя
#: попадает в документацию и в разбор инцидентов.
SESSION_COOKIE_NAME: Final[str] = "speech_session"

#: Путь — корень, а не `/api`. Тег `<audio>` запрашивает `src` сам, и куку с
#: путём `/api` браузер бы к нему приложил; но тот же корень нужен и фронту,
#: который живёт на `/`. Сузить путь — значит снова изобретать куку
#: проигрывателя (SP-808), от которой этот механизм как раз избавляет.
SESSION_COOKIE_PATH: Final[str] = "/"

#: Кука на время самого входа: `state`, `nonce`, `code_verifier`, `next`.
#: Отдельная и короткая, потому что посетитель на этом шаге ещё никто — давать
#: любому желающему писать строки в таблицу нельзя.
LOGIN_COOKIE_NAME: Final[str] = "speech_login"

#: Сколько живёт незавершённый вход. Больше минут пятнадцати не нужно: это
#: время ввода пароля, а не рабочий день.
LOGIN_COOKIE_MAX_AGE: Final[int] = 900

#: Длина случайного значения куки в байтах. 32 байта — 256 бит энтропии;
#: перебор бессмыслен, а строка остаётся короткой.
SESSION_TOKEN_BYTES: Final[int] = 32


def new_session_token() -> str:
    """Новое значение куки. `token_urlsafe` — CSPRNG, не `random`."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def token_hash(token: str) -> str:
    """Что кладётся в базу вместо значения куки.

    Обычный sha256 без соли намеренно: значение куки — 256 бит случайности, а
    не пароль, и словарь по нему не построить. Соль и растягивание защищают от
    перебора угадываемых значений, которых здесь нет, а стоили бы времени на
    каждом запросе.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionRecord(BaseModel):
    """Живая сессия, как она лежит в базе."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    id: str
    sub: str
    username: str | None
    email: str | None
    display_name: str | None
    roles: tuple[str, ...]
    refresh_token: str | None
    expires_at: datetime
    last_seen_at: datetime


_INSERT_SQL = text(
    """
    INSERT INTO speech_sessions (id, sub, username, email, display_name, roles,
                                 refresh_token, expires_at, created_at, last_seen_at)
    VALUES (:id, :sub, :username, :email, :display_name,
            CAST(:roles AS varchar(64)[]), :refresh_token,
            now() + make_interval(secs => :ttl_seconds), now(), now())
    """
)

#: Отбор идёт по живым строкам: погашенная и просроченная не отличаются от
#: несуществующей. Возвращать их и решать снаружи значило бы завести второе
#: место, где определяется «сессия действует».
_SELECT_SQL = text(
    """
    SELECT id, sub, username, email, display_name, roles, refresh_token,
           expires_at, last_seen_at
      FROM speech_sessions
     WHERE id = :id
       AND revoked_at IS NULL
       AND expires_at > now()
    """
)

_TOUCH_SQL = text("UPDATE speech_sessions SET last_seen_at = now() WHERE id = :id")

#: Продление скользящее: пока человек работает, сессия не обрывается посреди
#: разбора звонка. Потолок задаёт `SPEECH_SESSION_TTL_SECONDS` от текущего
#: момента, то есть «двенадцать часов бездействия», а не «двенадцать часов с
#: момента входа».
_REFRESH_SQL = text(
    """
    UPDATE speech_sessions
       SET roles = CAST(:roles AS varchar(64)[]),
           refresh_token = :refresh_token,
           last_seen_at = now(),
           expires_at = now() + make_interval(secs => :ttl_seconds)
     WHERE id = :id
       AND revoked_at IS NULL
    """
)

_REVOKE_SQL = text(
    "UPDATE speech_sessions SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"
)

#: «Выкинуть человека отовсюду» — вопрос про человека, а не про сессию: он мог
#: войти с трёх машин. Под это заведён индекс по `sub`.
_REVOKE_ALL_SQL = text(
    "UPDATE speech_sessions SET revoked_at = now() WHERE sub = :sub AND revoked_at IS NULL"
)


async def create_session(
    session: AsyncSession,
    *,
    token: str,
    sub: str,
    username: str | None,
    email: str | None,
    display_name: str | None,
    roles: tuple[str, ...],
    refresh_token: str | None,
    ttl_seconds: int,
) -> str:
    """Записать сессию и вернуть её идентификатор (хеш куки)."""
    session_id = token_hash(token)
    await session.execute(
        _INSERT_SQL,
        {
            "id": session_id,
            "sub": sub,
            "username": username,
            "email": email,
            "display_name": display_name,
            "roles": list(roles),
            "refresh_token": refresh_token,
            "ttl_seconds": ttl_seconds,
        },
    )
    return session_id


async def load_session(session: AsyncSession, token: str) -> SessionRecord | None:
    """Живая сессия по значению куки, иначе `None`.

    Погашенная и просроченная неотличимы от несуществующей намеренно: снаружи
    на все три случая один ответ — «войдите заново».
    """
    if not token:
        return None
    row = (await session.execute(_SELECT_SQL, {"id": token_hash(token)})).mappings().first()
    if row is None:
        return None
    return SessionRecord(
        id=row["id"],
        sub=row["sub"],
        username=row["username"],
        email=row["email"],
        display_name=row["display_name"],
        roles=tuple(row["roles"] or ()),
        refresh_token=row["refresh_token"],
        expires_at=row["expires_at"],
        last_seen_at=row["last_seen_at"],
    )


async def touch_session(session: AsyncSession, session_id: str) -> None:
    """Отметить, что сессией только что пользовались."""
    await session.execute(_TOUCH_SQL, {"id": session_id})


async def refresh_session(
    session: AsyncSession,
    session_id: str,
    *,
    roles: tuple[str, ...],
    refresh_token: str | None,
    ttl_seconds: int,
) -> None:
    """Переписать роли после перепроверки в Keycloak и продлить срок."""
    await session.execute(
        _REFRESH_SQL,
        {
            "id": session_id,
            "roles": list(roles),
            "refresh_token": refresh_token,
            "ttl_seconds": ttl_seconds,
        },
    )


async def revoke_session(session: AsyncSession, session_id: str) -> None:
    """Погасить одну сессию. Не удаление: администратору нужно видеть, что вход был."""
    await session.execute(_REVOKE_SQL, {"id": session_id})


async def revoke_sessions_for_sub(session: AsyncSession, sub: str) -> int:
    """Погасить все сессии человека. Возвращает, сколько погашено."""
    # `cast` к `CursorResult` — как в admin_routes: `Result` объявлен без
    # `rowcount`, хотя у выполненного `UPDATE` он есть всегда.
    result = cast(CursorResult[Any], await session.execute(_REVOKE_ALL_SQL, {"sub": sub}))
    return int(result.rowcount or 0)
