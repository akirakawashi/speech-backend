"""Контракт поддерживаемых настроек бэкенда и шаблона окружения."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from src.setting.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _clear_setting_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Удалить внешние значения известных полей на время одного теста."""
    fields = set(Settings.model_fields)
    for name in os.environ:
        if name.lower() in fields:
            monkeypatch.delenv(name)


def test_environment_template_uses_supported_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверить имена параметров и загрузить настоящий шаблон через Settings."""
    _clear_setting_environment(monkeypatch)
    template = ROOT / ".env.example"
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", template.read_text(), re.M))
    supported = {name.upper() for name in Settings.model_fields}
    compose = ROOT / "docker-compose.yml"
    if compose.exists():
        supported.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose.read_text()))

    assert declared <= supported, sorted(declared - supported)
    Settings(_env_file=template)


def test_existing_dotenv_with_retired_asr_options_still_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Прочитать старый рабочий формат .env, сохранив только адрес пробы Whisper."""
    _clear_setting_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SPEECH_WHISPER_BASE_URL=http://whisper.test/v1\n"
        "SPEECH_WHISPER_API_KEY=synthetic-retired-secret\n"
        "SPEECH_WHISPER_MODEL=retired-model\n"
        "SPEECH_WHISPER_LANGUAGE=ru\n"
        "SPEECH_WHISPER_TIMEOUT_SECONDS=obsolete-invalid-value\n"
        "SPEECH_WHISPER_VAD_FILTER=obsolete-invalid-value\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.speech_whisper_base_url == "http://whisper.test/v1"
    assert "synthetic-retired-secret" not in settings.model_dump_json()
