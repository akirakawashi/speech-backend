"""Вход через BFF: что он обязан делать и чего обязан не делать.

Проверяется не «ручка отвечает 302», а те способы, которыми вход мог бы
открыть записи разговоров или увести сотрудника:

* **открытый перенаправитель.** `?next=` управляет тем, куда человек уедет
  после ввода пароля, и адресная строка до перехода выглядит безупречно —
  домен в ней наш;
* **подделка возврата.** Без сверки `state` посторонний сайт мог бы завершить
  вход своим кодом, и в браузере жертвы оказалась бы чужая сессия;
* **подмена ID-токена.** `nonce` связывает его с этой попыткой входа; пока его
  не сверяют, защиты от повтора нет вовсе;
* **вход без групп.** Токен настоящий, прав нет — пускать нельзя;
* **свойства куки.** `HttpOnly`, `SameSite` и `Secure` здесь не украшение: на
  них держится всё, что отличает куку сессии от токена в localStorage.

Живая база не нужна: сессия подменяется через `session_dep`, Keycloak — через
`oidc` и валидатор. Что маршруты вообще зарегистрированы и не требуют
аутентификации, проверяет `tests/test_speech_authz_matrix.py`.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.di.http import create_app
from src.di.requests import session_dep
from src.infrastructure.auth import sessions as sessions, speech_oidc as oidc
from src.presentation.http import auth_routes as auth_routes
from src.setting.config import Settings, get_settings

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nowhere"

PUBLIC_BASE = "https://speech.ic-group.ru"


def _settings(**overrides: Any) -> Settings:
    # `_env_file=None` обязателен: Settings иначе читает .env разработчика, и
    # тест начинает зависеть от машины, на которой запущен.
    base: dict[str, Any] = {
        "_env_file": None,
        "app_env": "local",
        "speech_auth_mode": "keycloak",
        "speech_ms_database_url": UNREACHABLE_DATABASE_URL,
        "speech_keycloak_issuer_url": "https://ssoc.example/realms/IC-GROUP",
        "speech_keycloak_jwks_url": "https://ssoc.example/realms/IC-GROUP/protocol/openid-connect/certs",
        "speech_keycloak_audience": "speech",
        "speech_keycloak_url": "https://ssoc.example",
        "speech_keycloak_realm": "IC-GROUP",
        "speech_keycloak_client_id": "speech",
        "speech_keycloak_client_secret": "s3cret",
        "speech_public_base_url": PUBLIC_BASE,
    }
    base.update(overrides)
    return Settings(**base)


class _FakeSession:
    """Заглушка сессии БД: запоминает запросы и отвечает пусто."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.statements.append(str(statement))

        class _Result:
            rowcount = 1

            def mappings(self) -> Any:
                class _M:
                    def first(self) -> None:
                        return None

                return _M()

        return _Result()

    async def commit(self) -> None:
        self.commits += 1


def _client(settings: Settings, db: _FakeSession | None = None) -> TestClient:
    """Приложение с подменёнными настройками и базой.

    Подмена `get_settings` обязательна, и на этом набор уже один раз
    покраснел целиком. `create_app(settings)` использует переданные настройки
    только для проверки на старте и для документации, а зависимости маршрутов
    объявлены как `Depends(get_settings)` — то есть берут **глобальный**
    закэшированный экземпляр, собранный из настоящего `.env` разработчика.

    Без подмены все проверки входа выполнялись бы в режиме `open` (именно он
    стоит в `.env`), то есть зеленели бы, ничего не проверив.
    """
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[session_dep] = lambda: db or _FakeSession()
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------------------- #
# Открытый перенаправитель
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example/phish",
        "//evil.example/phish",
        "/\\evil.example",
        "http://evil.example",
        "evil.example",
        "",
        None,
    ],
)
def test_next_outside_the_application_is_refused(raw: str | None) -> None:
    """Иначе ссылка с нашим доменом уводит сотрудника вводить пароль чужим.

    `//evil.example` отдельным случаем: для браузера это адрес с той же
    схемой, а не путь внутри сайта. Обратная косая — потому что часть браузеров
    нормализует её в прямую уже после проверки.
    """
    assert auth_routes.safe_next(raw) == "/"


@pytest.mark.parametrize("raw", ["/", "/calls/123", "/admin/users?page=2"])
def test_next_inside_the_application_is_kept(raw: str) -> None:
    assert auth_routes.safe_next(raw) == raw


# --------------------------------------------------------------------------- #
# Отправка на Keycloak
# --------------------------------------------------------------------------- #


def test_login_sends_the_browser_to_keycloak_with_pkce() -> None:
    """PKCE отправляется, хотя клиент конфиденциальный.

    Он ничего не стоит и закрывает перехват кода на участке до сервиса; а если
    в клиенте выставлено требование S256, без него вход просто не состоится —
    и выяснилось бы это на живом пользователе.
    """
    response = _client(_settings()).get("/api/v1/auth/login?next=/calls/42")

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("https://ssoc.example/realms/IC-GROUP/protocol/openid-connect/auth?")
    assert "code_challenge_method=S256" in location
    assert "code_challenge=" in location
    assert "client_id=speech" in location
    # Адрес возврата — ровно тот, что зарегистрирован в клиенте.
    assert "redirect_uri=https%3A%2F%2Fspeech.ic-group.ru%2Fapi%2Fv1%2Fauth%2Fcallback" in location
    # Секрет в браузер не уезжает ни при каких обстоятельствах.
    assert "s3cret" not in location


def test_login_cookie_is_lax_and_scoped_to_the_auth_path() -> None:
    """`strict` сломал бы вход на последнем шаге.

    Возврат от Keycloak — переход с чужого сайта, и куку со `SameSite=Strict`
    браузер к нему не приложил бы: `state` не с чем было бы сверить. Причём
    по-разному в разных браузерах, то есть «иногда не работает вход».
    """
    response = _client(_settings()).get("/api/v1/auth/login")

    raw = response.headers["set-cookie"]
    assert sessions.LOGIN_COOKIE_NAME in raw
    assert "HttpOnly" in raw
    assert "SameSite=lax" in raw.replace("SameSite=Lax", "SameSite=lax")
    assert "Path=/api/v1/auth" in raw


def test_login_cookie_carries_state_nonce_and_verifier() -> None:
    """Всё состояние входа — в куке, ни строки в базе.

    Посетитель на этом шаге ещё никто, и давать любому желающему писать строки
    в таблицу нельзя: это даровой способ её раздуть.
    """
    response = _client(_settings()).get("/api/v1/auth/login?next=/calls/7")

    cookie = response.cookies[sessions.LOGIN_COOKIE_NAME]
    payload = json.loads(base64.urlsafe_b64decode(cookie + "=" * (-len(cookie) % 4)))
    assert set(payload) == {"state", "nonce", "verifier", "next"}
    assert payload["next"] == "/calls/7"
    assert len(payload["verifier"]) >= 43  # RFC 7636


def test_login_in_open_mode_does_not_pretend_to_authenticate() -> None:
    """`open` — это отсутствие аутентификации, а не её имитация.

    Изобразив вход, мы сделали бы режим неотличимым от настроенного, и
    «почему у всех права администратора» выяснялось бы на приёмке.
    """
    response = _client(_settings(speech_auth_mode="open")).get("/api/v1/auth/login?next=/calls/1")

    assert response.status_code == 303
    assert response.headers["location"] == "/calls/1"
    assert sessions.LOGIN_COOKIE_NAME not in response.cookies


def test_login_cookie_is_secure_outside_local_without_any_header() -> None:
    """Находка пентеста COOKIE-SEC, речёвочная половина.

    `speech-edge.nginx.conf` слушает `listen 8080` без TLS и ставит
    `X-Forwarded-Proto $scheme`, то есть затирает значение корпоративного
    прокси на `http`. Флаг обязан стоять без единого заголовка: он выводится
    из объявленного окружения, а не из того, что прислало чужое звено.
    """
    response = _client(_settings(app_env="alpha")).get("/api/v1/auth/login")

    assert "Secure" in response.headers["set-cookie"]


def test_forwarded_proto_http_does_not_strip_secure_outside_local() -> None:
    """Регрессия на заголовок: раньше ровно это значение и снимало флаг."""
    response = _client(_settings(app_env="alpha")).get(
        "/api/v1/auth/login", headers={"X-Forwarded-Proto": "http"}
    )

    assert "Secure" in response.headers["set-cookie"]


def test_local_still_decides_by_the_request() -> None:
    """Послабление осталось ровно там, где оно нужно, и больше нигде.

    На `http://127.0.0.1` браузер куку с `Secure` выбрасывает без единого
    сообщения, поэтому в `local` схему всё ещё можно сообщить запросом или
    заголовком.
    """
    plain = _client(_settings()).get("/api/v1/auth/login")
    assert "Secure" not in plain.headers["set-cookie"]

    forwarded = _client(_settings()).get(
        "/api/v1/auth/login", headers={"X-Forwarded-Proto": "https"}
    )
    assert "Secure" in forwarded.headers["set-cookie"]


def test_always_forces_secure_even_in_local() -> None:
    """Оператор должен уметь включить флаг явно, не меняя `APP_ENV`."""
    response = _client(_settings(session_cookie_secure="always")).get("/api/v1/auth/login")

    assert "Secure" in response.headers["set-cookie"]


# --------------------------------------------------------------------------- #
# Возврат
# --------------------------------------------------------------------------- #


def test_callback_without_the_login_cookie_is_refused() -> None:
    """Без куки сверять `state` не с чем — значит вход не начинался здесь."""
    response = _client(_settings()).get("/api/v1/auth/callback?code=x&state=y")

    assert response.status_code == 303
    assert response.headers["location"] == "/?auth_error=state_mismatch"


def test_callback_with_a_foreign_state_is_refused() -> None:
    """Иначе посторонний сайт завершает вход своим кодом.

    В браузере жертвы оказалась бы сессия, заведённая на чужую учётную запись,
    и всё, что она сделает дальше, будет записано на неё.
    """
    client = _client(_settings())
    client.get("/api/v1/auth/login")

    # Значение латиницей намеренно: кириллица в строке запроса роняет сам
    # клиент тестов (`httpx` кодирует её как ASCII), то есть проверка падала бы
    # на своём инструменте, не дойдя до проверяемого кода.
    response = client.get("/api/v1/auth/callback?code=x&state=forged-state")

    assert response.status_code == 303
    assert response.headers["location"] == "/?auth_error=state_mismatch"


def test_callback_reports_a_provider_refusal_without_details() -> None:
    response = _client(_settings()).get("/api/v1/auth/callback?error=access_denied")

    assert response.headers["location"] == "/?auth_error=provider_error"


def _login_and_get_state(client: TestClient) -> dict[str, str]:
    client.get("/api/v1/auth/login?next=/calls/9")
    cookie = client.cookies[sessions.LOGIN_COOKIE_NAME]
    # Разбор приводится к объявленному типу, а не возвращается как `Any`:
    # `json.loads` не знает формы, и возврат `Any` из типизированной функции
    # гасит проверку у каждого вызывающего.
    payload = json.loads(base64.urlsafe_b64decode(cookie + "=" * (-len(cookie) % 4)))
    return {str(key): str(value) for key, value in payload.items()}


class _Claims:
    """Ответ валидатора такой, какой он есть на самом деле.

    Группы лежат в `raw`, а `roles` пустые: валидатор складывает туда
    только то, что нашлось в таблице псевдонимов платформы, а групп
    `whisper-icg-*` там нет. Прежняя заглушка клала группы в `roles` — то есть
    проверяла поведение, которого нет, и зеленела на входе, который в
    продакшене не работал ни для кого (найдено 2026-09-16 на живом узле).
    """

    def __init__(self, groups: list[str], nonce: str | None = None) -> None:
        self.keycloak_sub = "sub-1"
        self.username = "ivanov"
        self.email = "ivanov@example.ru"
        self.display_name = "Иванов"
        self.roles: list[str] = []
        self.raw: dict[str, Any] = {"groups": list(groups)}
        if nonce is not None:
            self.raw["nonce"] = nonce


def _patch_keycloak(
    monkeypatch: pytest.MonkeyPatch, *, groups: list[str], nonce: str | None, id_token: str = "id"
) -> None:
    async def _exchange(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"access_token": "at", "id_token": id_token, "refresh_token": "rt"}

    monkeypatch.setattr(oidc, "exchange_code", _exchange)

    class _Validator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def validate_token(self, token: str, *, access_token: str | None = None) -> _Claims:
            # ID-токен обязан проверяться вместе с access-токеном: в нём лежит
            # `at_hash`, и без пары `jose` отвергает токен целиком. Заглушка
            # требует того же — иначе набор зеленел бы на входе, который в
            # продакшене не работает вовсе (найдено 2026-09-16 на живом узле).
            if token == id_token and access_token != "at":
                raise AssertionError("ID-токен проверен без access-токена")
            return _Claims(groups, nonce if token == id_token else None)

    monkeypatch.setattr("src.infrastructure.auth.keycloak.KeycloakTokenValidator", _Validator)


def test_callback_refuses_an_id_token_from_another_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nonce` затем и выдавался: без сверки защиты от повтора нет вовсе."""
    client = _client(_settings())
    _login_and_get_state(client)
    _patch_keycloak(monkeypatch, groups=["whisper-icg-readers"], nonce="чужой-nonce")

    response = client.get("/api/v1/auth/callback?code=good&state=" + _current_state(client))

    assert response.headers["location"] == "/?auth_error=nonce_mismatch"
    assert sessions.SESSION_COOKIE_NAME not in response.cookies


def _current_state(client: TestClient) -> str:
    cookie = client.cookies[sessions.LOGIN_COOKIE_NAME]
    return str(json.loads(base64.urlsafe_b64decode(cookie + "=" * (-len(cookie) % 4)))["state"])


def test_callback_refuses_a_valid_token_without_our_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Токен настоящий, групп нет — сессию заводить нельзя.

    Человеку надо сказать «доступ не выдан», а не «войдите ещё раз»: второе
    отправляет его по кругу через форму, которая отработает безупречно.
    """
    client = _client(_settings())
    state = _login_and_get_state(client)
    _patch_keycloak(monkeypatch, groups=["какая-то-чужая-группа"], nonce=state["nonce"])

    response = client.get(f"/api/v1/auth/callback?code=good&state={state['state']}")

    assert response.headers["location"] == "/?auth_error=no_groups"
    assert sessions.SESSION_COOKIE_NAME not in response.cookies


def test_callback_creates_a_session_and_returns_where_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeSession()
    client = _client(_settings(), db)
    state = _login_and_get_state(client)
    _patch_keycloak(monkeypatch, groups=["whisper-icg-editors"], nonce=state["nonce"])

    response = client.get(f"/api/v1/auth/callback?code=good&state={state['state']}")

    assert response.status_code == 303
    assert response.headers["location"] == "/calls/9"

    raw = response.headers["set-cookie"]
    assert sessions.SESSION_COOKIE_NAME in raw
    assert "HttpOnly" in raw
    assert "Path=/" in raw
    # Сессия записана, и кука входа погашена — второй раз тем же кодом не войти.
    assert any("INSERT INTO speech_sessions" in s for s in db.statements)
    assert db.commits >= 1


def test_the_session_cookie_value_is_not_what_lies_in_the_database() -> None:
    """В базе хеш, а не значение куки: утечка дампа не даёт живых сессий."""
    token = sessions.new_session_token()

    assert sessions.token_hash(token) != token
    assert len(sessions.token_hash(token)) == 64
    assert sessions.new_session_token() != token


# --------------------------------------------------------------------------- #
# Выход
# --------------------------------------------------------------------------- #


def test_logout_works_without_a_live_session() -> None:
    """Требовать для выхода действующую сессию — значит не дать выйти тому,
    у кого она протухла. Именно ему это нужнее всего."""
    response = _client(_settings()).post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert "logout_url" in response.json()


def test_logout_revokes_the_session_and_clears_the_cookie() -> None:
    db = _FakeSession()
    client = _client(_settings(), db)
    client.cookies.set(sessions.SESSION_COOKIE_NAME, "opaque-cookie-value")

    response = client.post("/api/v1/auth/logout")

    assert any("UPDATE speech_sessions" in s and "revoked_at" in s for s in db.statements)
    # Кука гасится, а не просто перезаписывается пустой: проверяется признак
    # немедленного истечения, а не форма записи — её Starlette пишет
    # по-разному в разных версиях.
    cleared = response.headers["set-cookie"]
    assert sessions.SESSION_COOKIE_NAME in cleared
    assert "Max-Age=0" in cleared or "01 Jan 1970" in cleared
    assert response.json()["logout_url"].startswith(
        "https://ssoc.example/realms/IC-GROUP/protocol/openid-connect/logout?"
    )


def test_both_speech_cookies_actually_leave_the_jar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Гашение проверяется по СОДЕРЖИМОМУ банки, а не по виду заголовка.

    Повод сомневаться был: `set_cookie` ставит `Secure` (вне `local`), а
    `delete_cookie` его не ставит — атрибуты двух объявлений одной куки
    расходятся. По RFC 6265bis кука опознаётся тройкой имя+домен+путь, и
    `Secure`/`SameSite` в неё не входят, поэтому гашение обязано доходить.

    Куки здесь кладёт САМ сервис, а не тест: `client.cookies.set(domain=...)`
    для этого не годится и молча даёт ложный результат. `http.cookiejar`
    хранит куку без атрибута `Domain` под «эффективным» хостом, а для имени без
    точки это `testserver.local` (`eff_request_host`), тогда как ручная
    установка кладёт её под `testserver` — две разные полки.
    """
    db = _FakeSession()
    client = _client(_settings(app_env="alpha"), db)
    state = _login_and_get_state(client)
    assert sessions.LOGIN_COOKIE_NAME in client.cookies
    packed = client.cookies[sessions.LOGIN_COOKIE_NAME]
    _patch_keycloak(monkeypatch, groups=["whisper-icg-editors"], nonce=state["nonce"])

    # Куку входа приходится приложить руками: с `Secure` клиент по `http` её не
    # отправит — ровно как настоящий браузер. Этот возврат её и гасит.
    client.get(
        f"/api/v1/auth/callback?code=good&state={state['state']}",
        headers={"Cookie": f"{sessions.LOGIN_COOKIE_NAME}={packed}"},
    )

    assert sessions.LOGIN_COOKIE_NAME not in client.cookies
    assert sessions.SESSION_COOKIE_NAME in client.cookies, "иначе гасить нечего и тест пуст"

    client.post("/api/v1/auth/logout")

    assert sessions.SESSION_COOKIE_NAME not in client.cookies


def test_logout_in_open_mode_has_nowhere_to_send_anyone() -> None:
    response = _client(_settings(speech_auth_mode="open")).post("/api/v1/auth/logout")

    assert response.json()["logout_url"] == ""


# --------------------------------------------------------------------------- #
# Сборка адресов
# --------------------------------------------------------------------------- #


def test_realm_urls_are_built_in_one_place() -> None:
    """Собранные по месту, они разошлись бы молча — например, завершающей
    косой чертой в одном месте и без неё в другом."""
    client = oidc.OidcClient(
        base_url="https://ssoc.example/", realm="IC-GROUP", client_id="speech", client_secret="x"
    )

    assert client.token_endpoint == (
        "https://ssoc.example/realms/IC-GROUP/protocol/openid-connect/token"
    )
    assert client.authorize_endpoint.endswith("/protocol/openid-connect/auth")
    assert client.end_session_endpoint.endswith("/protocol/openid-connect/logout")


def test_an_unconfigured_client_is_not_considered_configured() -> None:
    """Пустой секрет — это «войти не сможет никто», и признавать такую
    настройку рабочей значит отложить отказ до экрана входа."""
    assert not oidc.OidcClient(
        base_url="https://ssoc.example", realm="IC-GROUP", client_id="speech", client_secret=""
    ).is_configured


def test_pkce_challenge_is_the_sha256_of_the_verifier() -> None:
    verifier = oidc.new_pkce_verifier()
    challenge = oidc.pkce_challenge(verifier)

    assert "=" not in challenge and "+" not in challenge and "/" not in challenge
    assert challenge != verifier
    assert oidc.pkce_challenge(verifier) == challenge


def test_a_damaged_login_cookie_is_emptiness_and_not_a_crash() -> None:
    """Испорченная кука — это «вход не начинался», а не 500 на возврате."""
    assert auth_routes._unpack("не-base64") == {}
    assert auth_routes._unpack(base64.urlsafe_b64encode(b"[1,2,3]").decode()) == {}


def _fake_jwt(header: dict[str, Any], payload: dict[str, Any]) -> str:
    def part(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{part(header)}.{part(payload)}.signature-part"


def _patch_rejecting_validator(
    monkeypatch: pytest.MonkeyPatch, *, access_token: str, reason: Exception
) -> None:
    async def _exchange(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"access_token": access_token, "id_token": "", "refresh_token": "rt"}

    monkeypatch.setattr(oidc, "exchange_code", _exchange)

    class _Validator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def validate_token(self, token: str, *, access_token: str | None = None) -> Any:
            raise reason

    monkeypatch.setattr("src.infrastructure.auth.keycloak.KeycloakTokenValidator", _Validator)


def test_a_refused_token_leaves_the_reason_in_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Отказ входа обязан оставлять след, иначе разбирать его нечем.

    Наружу уходит одно слово, и это правильно. Но токен живёт минуты, второй
    раз его не выдадут, а воспроизвести вход посторонними средствами нельзя —
    клиент конфиденциальный. Если причина не записана, единственный способ
    узнать её — выкатить новый образ, что однажды уже стоило дня.
    """
    from fastapi import HTTPException

    token = _fake_jwt(
        {"alg": "RS256", "kid": "JqyGp3PKabcdef"},
        {"iss": "https://ssoc.example/realms/IC-GROUP", "aud": "account", "azp": "other-client"},
    )
    cause = ValueError("Invalid audience")
    refusal = HTTPException(status_code=401, detail="Token validation failed")
    refusal.__cause__ = cause
    client = _client(_settings())
    state = _login_and_get_state(client)
    _patch_rejecting_validator(monkeypatch, access_token=token, reason=refusal)

    with caplog.at_level("WARNING"):
        response = client.get(f"/api/v1/auth/callback?code=good&state={state['state']}")

    assert response.headers["location"] == "/?auth_error=token_invalid"
    assert "access_token rejected" in caplog.text
    assert "Token validation failed" in caplog.text
    assert "Invalid audience" in caplog.text
    assert "alg=RS256" in caplog.text
    assert "azp=other-client" in caplog.text
    assert "expected aud=speech" in caplog.text
    # Сам токен в журнал не идёт: причину он не объясняет, а живёт запись
    # дольше сессии.
    assert token not in caplog.text
    assert "signature-part" not in caplog.text


def test_the_id_token_is_validated_together_with_the_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В ID-токене кода-флоу всегда есть `at_hash` — отпечаток access-токена.

    `jose` сверяет клейм сам, но без исходного access-токена отвергает токен
    целиком: «No access_token provided to compare against at_hash claim».
    На этом вход в продакшене и стоял (2026-09-16): access-токен проходил,
    ID-токен нет, наружу уходило одно `token_invalid`. На стенде это не
    ловилось — там `SPEECH_AUTH_MODE=open`, и колбэк ничего не проверяет.
    """
    seen: list[tuple[str, str | None]] = []

    async def _exchange(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"access_token": "at", "id_token": "id", "refresh_token": "rt"}

    monkeypatch.setattr(oidc, "exchange_code", _exchange)

    client = _client(_settings())
    state = _login_and_get_state(client)

    class _Validator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def validate_token(self, token: str, *, access_token: str | None = None) -> _Claims:
            seen.append((token, access_token))
            return _Claims(["whisper-icg-readers"], state["nonce"] if token == "id" else None)

    monkeypatch.setattr("src.infrastructure.auth.keycloak.KeycloakTokenValidator", _Validator)

    response = client.get(f"/api/v1/auth/callback?code=good&state={state['state']}")

    assert response.headers["location"] == "/calls/9"
    assert seen == [("at", None), ("id", "at")]


def test_an_encrypted_token_is_named_as_such_in_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Пять частей — это JWE, и лечится он настройкой клиента, а не кодом.

    Отличать этот случай от несошедшейся подписи обязательно: по сообщению
    `jose` они неразличимы, а делать с ними надо прямо противоположное.
    """
    client = _client(_settings())
    state = _login_and_get_state(client)
    _patch_rejecting_validator(monkeypatch, access_token="a.b.c.d.e", reason=ValueError("no kid"))

    with caplog.at_level("WARNING"):
        response = client.get(f"/api/v1/auth/callback?code=good&state={state['state']}")

    assert response.headers["location"] == "/?auth_error=token_invalid"
    assert "shape=encrypted(JWE)" in caplog.text


async def test_groups_are_read_from_the_token_and_not_from_mapped_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TokenClaims.roles` для речёвки всегда пустые — и это не ошибка валидатора.

    Он складывает туда только то, что нашлось в таблице псевдонимов платформы, а
    групп заказчика `whisper-icg-*` там нет. Пока сервис читал `roles`, ролей не
    получал **никто**, с какими бы группами человек ни пришёл, и выглядело это
    как «вам не выдали доступ». Проверяется путь Bearer — тот, которым ходят
    служебные вызовы.
    """
    from src.infrastructure.auth.identity import resolve_principal

    class _Validator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def validate_token(self, token: str, *, access_token: str | None = None) -> Any:
            return _Claims(["/whisper-icg-admins"])

    monkeypatch.setattr("src.infrastructure.auth.keycloak.KeycloakTokenValidator", _Validator)

    principal = await resolve_principal(_settings(), "Bearer any-token")

    assert principal.roles == ("speech_reader", "speech_editor", "speech_admin")
    assert principal.auth_mode == "keycloak"


def test_a_login_without_groups_says_what_came_in_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """«Доступ не выдан» — три разные причины, и различить их можно только так.

    Маппер групп не настроен в клиенте, группы называются иначе, членства нет —
    наружу все три выглядят одинаково, а токена, чтобы посмотреть, ни у кого
    нет: он живёт минуты и второй раз не выдаётся.
    """
    client = _client(_settings())
    state = _login_and_get_state(client)
    _patch_keycloak(monkeypatch, groups=["/IC-GROUP/sales"], nonce=state["nonce"])

    with caplog.at_level("WARNING"):
        response = client.get(f"/api/v1/auth/callback?code=good&state={state['state']}")

    assert response.headers["location"] == "/?auth_error=no_groups"
    assert "no groups matched" in caplog.text
    assert "/IC-GROUP/sales" in caplog.text
    assert "whisper-icg-readers" in caplog.text
    # Имя и почта не персональные данные журнала: причину они не объясняют.
    assert "ivanov" not in caplog.text
