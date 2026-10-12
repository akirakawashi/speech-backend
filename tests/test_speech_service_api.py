"""Служебная поверхность `speech-ms` и правила её запуска.

Проверяется не «эндпойнт отвечает 200», а четыре способа, которыми этот сервис
мог бы соврать оператору или открыть записи разговоров:

* режим `open` отключает аутентификацию целиком — он обязан быть невозможен
  вне local/alpha, и именно на старте процесса, а не при первом запросе;
* режим `keycloak` без настроек проверки токена принимал бы любой токен —
  такая конфигурация тоже обязана останавливать процесс;
* готовность, не смотрящая в базу, зелена у сервиса, который не может ответить
  ни на один запрос;
* `/version` — единственное, чем выкатка отличается от невыкатки: `/health`
  одинаков у старого и нового образа.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from src.di.http import create_app
from src.domain.calls.contracts import SPEECH_API_CONTRACT_VERSION
from src.infrastructure.persistence.session import SPEECH_REQUIRED_SCHEMA_REVISION
from src.setting.config import Settings, get_settings
from tests.speech_db_support import speech_test_database_url

#: Адрес, по которому заведомо никто не отвечает: порт 1 на петле. Соединение
#: отклоняется мгновенно — это важно, недостижимость через ожидание таймаута
#: растянула бы набор на минуты.
#:
#: Нужен он вот зачем. Проверка «база недоступна» раньше не задавала адреса
#: вовсе и опиралась на то, что на порту по умолчанию никто не слушает. Это не
#: свойство кода, а свойство машины: достаточно открыть на том порту тоннель к
#: базе — и «недостижимая» база отвечает, а тест краснеет по верной причине в
#: неверном месте. Заодно ни один тест этого набора больше не может случайно
#: подключиться к настоящей базе разработчика.
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nowhere"


def _settings(**overrides: object) -> Settings:
    # `_env_file=None` обязателен: Settings иначе читает .env разработчика, и
    # тест начинает зависеть от машины, на которой запущен.
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "local",
        "speech_auth_mode": "open",
        "speech_ms_database_url": UNREACHABLE_DATABASE_URL,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture()
def client() -> Iterator[TestClient]:
    settings = _settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health_is_public_and_checks_nothing(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_names_this_service_and_its_contract(client: TestClient) -> None:
    payload = client.get("/version").json()
    assert payload["service"] == "speech-ms"
    # Версия берётся из той же константы, которую сериализует код: захардкоженное
    # здесь значение разъехалось бы с поведением молча.
    assert payload["contracts"] == {"speech.api": SPEECH_API_CONTRACT_VERSION}
    # Не собран релизным конвейером — так и сказано, а не выдумано.
    assert payload["commit"] == "unknown"


def test_ready_fails_closed_when_database_is_unreachable(client: TestClient) -> None:
    """Готовность обязана смотреть в базу, а не отвечать из процесса.

    База недостижима не «по умолчанию», а по названному адресу — см.
    `UNREACHABLE_DATABASE_URL`.
    """
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "database unavailable"


def test_ready_refuses_a_reachable_but_stale_schema(client: TestClient, monkeypatch) -> None:
    """Достижимая база с чужой схемой — не «готов».

    Самый неприятный отказ этого сервиса: процесс отвечает `ready`, а падает на
    каждом запросе. Причина в ответе названа отдельно, чтобы оператор не искал
    её в логах.
    """
    from src.infrastructure.runtime import system as speech_api

    class _Session:
        """База отвечает: соединение есть, `SELECT 1` проходит."""

        async def execute(self, *_a: object, **_k: object) -> None:
            return None

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    async def _schema_not_applied(_session: object) -> bool:
        return False

    monkeypatch.setattr(
        speech_api, "get_speech_ms_sessionmaker", lambda _settings: lambda: _Session()
    )
    monkeypatch.setattr(speech_api, "speech_schema_is_ready", _schema_not_applied)

    response = client.get("/ready")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "migration_required" in detail
    # Ревизия названа в ответе: оператор узнаёт, что применять, не открывая код.
    assert SPEECH_REQUIRED_SCHEMA_REVISION in detail


@pytest.mark.parametrize("env", ["production", "prod", "staging"])
def test_open_auth_mode_refuses_to_start_outside_local_and_alpha(env: str) -> None:
    settings = _settings(app_env=env, speech_auth_mode="open")
    with pytest.raises(RuntimeError, match="SPEECH_AUTH_MODE=open"):
        create_app(settings)


@pytest.mark.parametrize("env", ["local", "alpha"])
def test_open_auth_mode_is_allowed_on_local_and_alpha(env: str) -> None:
    assert create_app(_settings(app_env=env, speech_auth_mode="open")) is not None


def test_keycloak_mode_requires_token_validation_settings() -> None:
    settings = _settings(app_env="production", speech_auth_mode="keycloak")
    with pytest.raises(RuntimeError, match="SPEECH_KEYCLOAK_ISSUER_URL"):
        create_app(settings)


def test_keycloak_mode_starts_when_fully_configured() -> None:
    """Настроек теперь восемь, а не три.

    Первые три нужны, чтобы **проверить** токен; остальные пять — чтобы его
    вообще получить: клиент `speech` конфиденциальный, и обмен кода делает
    сервис (ADR-SPEECH-0002).
    """
    settings = _settings(
        app_env="production",
        speech_auth_mode="keycloak",
        speech_keycloak_issuer_url="https://keycloak.example/realms/speech",
        speech_keycloak_jwks_url="https://keycloak.example/realms/speech/protocol/openid-connect/certs",
        speech_keycloak_audience="speech-front",
        speech_keycloak_url="https://keycloak.example",
        speech_keycloak_realm="speech",
        speech_keycloak_client_id="speech",
        speech_keycloak_client_secret="s3cret",
        speech_public_base_url="https://speech.example",
    )
    assert create_app(settings) is not None


def test_keycloak_mode_without_the_client_secret_refuses_to_start() -> None:
    """Без секрета войти не сможет никто, и узнать об этом на экране входа
    первого пользователя — поздно: отказ приходит после правильно введённого
    пароля, то есть выглядит как «не тот пароль»."""
    with pytest.raises(RuntimeError, match="SPEECH_KEYCLOAK_CLIENT_SECRET"):
        create_app(
            _settings(
                app_env="production",
                speech_auth_mode="keycloak",
                speech_keycloak_issuer_url="https://keycloak.example/realms/speech",
                speech_keycloak_jwks_url="https://keycloak.example/realms/speech/certs",
                speech_keycloak_audience="speech-front",
                speech_keycloak_url="https://keycloak.example",
                speech_keycloak_realm="speech",
                speech_keycloak_client_id="speech",
                speech_public_base_url="https://speech.example",
            )
        )


def test_unknown_auth_mode_is_refused() -> None:
    with pytest.raises(RuntimeError, match="is not a valid mode"):
        create_app(_settings(speech_auth_mode="disabled"))


def test_openapi_is_closed_outside_local_and_alpha() -> None:
    """Фронт генерирует типы из openapi.json на стенде; наружу его не публикуем."""
    exposed = create_app(_settings(app_env="alpha"))
    assert exposed.openapi_url == "/openapi.json"

    closed = create_app(
        _settings(
            app_env="production",
            speech_auth_mode="keycloak",
            speech_keycloak_issuer_url="https://keycloak.example/realms/speech",
            speech_keycloak_jwks_url="https://keycloak.example/realms/speech/certs",
            speech_keycloak_audience="speech-front",
            speech_keycloak_url="https://keycloak.example",
            speech_keycloak_realm="speech",
            speech_keycloak_client_id="speech",
            speech_keycloak_client_secret="s3cret",
            speech_public_base_url="https://speech.example",
        )
    )
    assert closed.openapi_url is None
    assert closed.docs_url is None


# --------------------------------------------------------------------------- #
# Готовность с зелёной стороны
# --------------------------------------------------------------------------- #

_LIVE_DATABASE_URL = speech_test_database_url()


@pytest.mark.skipif(
    not _LIVE_DATABASE_URL,
    reason="SPEECH_POSTGRES_TEST_URL is required: зелёную готовность нечем доказать",
)
async def test_ready_is_green_against_a_real_database_with_the_right_schema() -> None:
    """Единственная проверка, которая доказывает, что `/ready` **бывает** зелёным.

    Три соседних теста проверяют отказ: база недостижима, схема не та, ключа
    нет. Все три пройдут и на сервисе, который отвечает `503` всегда и на всё —
    например, если проверка ревизии однажды разъедется с миграциями. Тогда
    выкатка встанет намертво, а набор останется зелёным: он проверяет только то,
    что сервис умеет отказывать.

    Живая база тут обязательна по существу: проверяется чтение `alembic_version`
    настоящим запросом к настоящей схеме. Мок ответил бы то, что в него положили.
    """
    settings = _settings(speech_ms_database_url=_LIVE_DATABASE_URL)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        response = await client.get("/ready")
    from src.infrastructure.persistence.session import get_speech_ms_engine

    await get_speech_ms_engine(settings).dispose()
    app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ready"}
