"""Запросы и результаты входа без HTTP, SQL и сырых токенов в repr."""

from pydantic import BaseModel, ConfigDict, Field

SESSION_COOKIE_NAME = "speech_session"
SESSION_COOKIE_PATH = "/"
LOGIN_COOKIE_NAME = "speech_login"
LOGIN_COOKIE_MAX_AGE = 900


class CookieSettings(BaseModel):
    """Только публичные параметры политики браузерных cookies."""

    model_config = ConfigDict(frozen=True)
    app_env: str
    session_cookie_secure: str


class Credentials(BaseModel):
    """Доставленные клиентом идентификаторы текущего пользователя."""

    model_config = ConfigDict(frozen=True)
    session_token: str = Field(default="", repr=False)
    authorization: str | None = Field(default=None, repr=False)


class LoginAttempt(BaseModel):
    """Значения связывания одной попытки входа с ответом провайдера."""

    model_config = ConfigDict(frozen=True)
    state: str
    nonce: str
    verifier: str = Field(repr=False)
    next: str = "/"


class BeginLogin(BaseModel):
    """Адрес входа и параметры незавершённой попытки, если они нужны."""

    model_config = ConfigDict(frozen=True)
    destination: str
    attempt: LoginAttempt | None = None
    error: str | None = None


class LoginCallbackQuery(BaseModel):
    """Параметры возврата провайдера и сохранённая клиентом попытка входа."""

    model_config = ConfigDict(frozen=True)
    code: str | None = Field(default=None, repr=False)
    state: str | None = None
    error: str | None = None
    attempt: LoginAttempt | None = None


class LoginCompletion(BaseModel):
    """Результат входа: адрес возврата или отказ и новая непрозрачная сессия."""

    model_config = ConfigDict(frozen=True)
    destination: str = "/"
    token: str | None = Field(default=None, repr=False)
    ttl_seconds: int = 0
    error: str | None = None


class LogoutResult(BaseModel):
    """Адрес завершения внешнего входа после погашения внутренней сессии."""

    model_config = ConfigDict(frozen=True)
    logout_url: str = ""
