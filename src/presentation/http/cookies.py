"""Атрибут Secure по публичной политике cookies и локальной схеме запроса."""

from fastapi import Request

from src.application.dto.authentication import CookieSettings


def cookie_secure(settings: CookieSettings, request: Request) -> bool:
    """Ставить ли `Secure` на сессионную куку.

    * `always` — да, всегда. Оператор форсирует флаг явно.
    * `never` — нет. Запрещено только в продуктиве: там процесс не стартует
      вовсе (см. `assert_cookie_security` выше). В `alpha` и прочих нежёстких
      окружениях это объявленный оператором отладочный режим, нужный для
      прямого входа по `http` мимо корпоративного прокси.
    * `auto` (умолчание) — да везде, кроме `APP_ENV=local`. В local остаётся
      прежнее мягкое поведение: на `http://127.0.0.1` браузер куку с `Secure`
      молча выбрасывает, и разработка сломалась бы без единого сообщения.
    """
    mode = str(settings.session_cookie_secure or "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    if settings.app_env.strip().lower() != "local":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return (forwarded or request.url.scheme) == "https"
