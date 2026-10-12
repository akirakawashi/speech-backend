"""Регистрация последней увиденной личности в общей транзакции запроса."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.authorization import Principal

_UPSERT_USER_SQL = text(
    """
    INSERT INTO speech_users (sub, username, email, display_name, roles_last_seen,
                              first_seen_at, last_seen_at)
    VALUES (:sub, :username, :email, :display_name, CAST(:roles AS varchar(64)[]),
            now(), now())
    ON CONFLICT (sub) DO UPDATE
       SET username = EXCLUDED.username,
           email = EXCLUDED.email,
           display_name = EXCLUDED.display_name,
           roles_last_seen = EXCLUDED.roles_last_seen,
           last_seen_at = now()
    """
)


async def remember_user(session: AsyncSession, principal: Principal) -> None:
    """Записать, кого сервис видел и с какими правами.

    Источник правды по ролям — Keycloak; здесь последнее увиденное значение,
    чтобы администратор мог понять, с какими правами человек заходил, не открывая
    Keycloak (модель `speech_users`).
    """
    await session.execute(
        _UPSERT_USER_SQL,
        {
            "sub": principal.sub,
            "username": principal.username,
            "email": principal.email,
            "display_name": principal.display_name,
            "roles": list(principal.roles),
        },
    )
