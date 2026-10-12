"""Контракт локального запуска API и миграций из docker-compose.yml.

Проверяем границы запуска, отдельные учётные записи базы и передачу настроек:
обычный up поднимает API, а миграции выполняются явной командой.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES: dict[str, Any] = COMPOSE["services"]


def test_backend_compose_owns_only_api_and_migrations() -> None:
    """Бэкенд не запускает соседние приложения и не создаёт хранилища записей."""
    assert set(SERVICES) == {"speech-ms", "migrate-speech"}
    assert "volumes" not in COMPOSE


def test_default_start_runs_only_api() -> None:
    """Обычный up запускает API; миграции требуют отдельного профиля."""
    default = {name for name, spec in SERVICES.items() if not spec.get("profiles")}
    assert default == {"speech-ms"}
    assert SERVICES["speech-ms"]["restart"] == "unless-stopped"
    assert SERVICES["migrate-speech"]["profiles"] == ["operations"]


def test_migrations_are_an_explicit_one_time_command() -> None:
    """Команда применяет дерево речи один раз и не перезапускается вместе с API."""
    spec = SERVICES["migrate-speech"]
    assert spec["command"] == ["alembic", "-c", "alembic.ini", "upgrade", "head"]
    assert "restart" not in spec
    assert "depends_on" not in SERVICES["speech-ms"]


def test_api_and_migrations_require_separate_database_credentials() -> None:
    """Настройка миграций не подставляет учётную запись API и не получает его секреты."""
    runtime_env = SERVICES["speech-ms"]["environment"]
    migration_env = SERVICES["migrate-speech"]["environment"]
    assert "AICISTENT_DOCKER_SPEECH_MS_DATABASE_URL:?" in runtime_env["SPEECH_MS_DATABASE_URL"]
    assert (
        "AICISTENT_DOCKER_SPEECH_MS_MIGRATE_DATABASE_URL:?"
        in migration_env["SPEECH_MS_DATABASE_URL"]
    )
    assert set(migration_env) == {"SPEECH_MS_DATABASE_URL"}


@pytest.mark.parametrize("service", ("speech-ms", "migrate-speech"))
def test_containers_receive_only_explicit_process_settings(service: str) -> None:
    """Настройки Whisper, источников и LLM не передаются процессам API и миграций."""
    spec = SERVICES[service]
    assert "env_file" not in spec
    worker_prefixes = (
        "SPEECH_WHISPER_",
        "SPEECH_DIFY_",
        "SPEECH_NCC_",
        "SPEECH_TELPHIN_",
        "SPEECH_QUALITY_LLM_",
        "TRANSCRIPTION_",
    )
    assert not any(name.startswith(worker_prefixes) for name in spec["environment"])


@pytest.mark.parametrize("service", ("speech-ms", "migrate-speech"))
def test_containers_keep_read_only_filesystems_and_drop_privileges(service: str) -> None:
    """Процессы пишут временные файлы в tmpfs и не получают дополнительные права."""
    spec = SERVICES[service]
    assert spec["read_only"] is True
    assert spec["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in spec["security_opt"]
    assert any(str(mount).startswith("/tmp") for mount in spec["tmpfs"])


def test_api_has_bounded_space_for_temporary_reports() -> None:
    """Файлы Excel не растут без ограничения в памяти Docker-хоста."""
    assert any("size=" in str(mount) for mount in SERVICES["speech-ms"]["tmpfs"])


def test_api_is_published_on_loopback_only() -> None:
    """Локальный порт API доступен хосту; публикация наружу принадлежит прокси."""
    ports = SERVICES["speech-ms"]["ports"]
    assert len(ports) == 1
    assert ports[0].startswith("127.0.0.1:")
    assert ports[0].endswith(":8096")


def test_readiness_checks_database_schema() -> None:
    """Ожидание готовности проверяет /ready вместо одной живости HTTP-процесса."""
    spec = SERVICES["speech-ms"]
    assert any("/ready" in str(part) for part in spec["healthcheck"]["test"])


def test_api_receives_authentication_storage_and_browser_settings() -> None:
    """Локальный env доходит до контейнера для входа, cookies, CORS и воспроизведения."""
    environment = SERVICES["speech-ms"]["environment"]
    for name in (
        "SPEECH_AUTH_MODE",
        "SPEECH_KEYCLOAK_CLIENT_SECRET",
        "SPEECH_PUBLIC_BASE_URL",
        "BACKEND_CORS_ORIGINS",
        "SPEECH_DISPLAY_TIMEZONE",
        "SPEECH_STORAGE_BASE_URL",
        "SPEECH_STORAGE_ACCESS_KEY_ID",
        "SPEECH_STORAGE_SECRET_KEY",
        "SPEECH_METRICS_KEY",
    ):
        assert name in environment, name
    assert environment["SESSION_COOKIE_SECURE"] == "${SESSION_COOKIE_SECURE:-auto}"
    assert environment["SPEECH_STORAGE_BASE_URL"] == "${SPEECH_STORAGE_BASE_URL:-}"


def test_api_can_reach_external_services_with_private_database_network() -> None:
    """Оба процесса видят PostgreSQL; API получает сетевой выход к MinIO и Keycloak."""
    assert COMPOSE["networks"]["data"]["external"] is True
    assert "SPEECH_DOCKER_NETWORK" in COMPOSE["networks"]["data"]["name"]
    assert set(SERVICES["speech-ms"]["networks"]) == {"default", "data"}
    assert SERVICES["migrate-speech"]["networks"] == ["data"]
    assert not COMPOSE["networks"].get("default", {}).get("internal", False)


def test_compose_configuration_is_documented_in_environment_template() -> None:
    """Каждая пользовательская переменная Compose объявлена в шаблоне окружения."""
    source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", source))
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.M))
    assert used - {"GIT_COMMIT", "BUILD_TIME"} <= declared
