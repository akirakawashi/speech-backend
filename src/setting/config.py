"""Настройки API и служебных команд речи из окружения и .env."""

from functools import lru_cache
from typing import Annotated, Any

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки процессов речи с проверкой значений из окружения и .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Приложение и HTTP.
    app_env: str = "local"
    aicistent_git_commit: str = "unknown"
    aicistent_build_time: str = "unknown"
    backend_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:8000",
            "http://localhost:5173",
            "http://localhost:5174",
        ]
    )

    session_cookie_secure: str = "auto"

    cors_allow_methods: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["GET", "POST", "DELETE", "OPTIONS"]
    )
    cors_allow_headers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "Authorization",
            "Content-Type",
            "X-Aicistent-Dev-User",
            "X-Request-Id",
        ]
    )

    # Пул базы данных.
    db_pool_size: int = 5
    db_max_overflow: int = 3
    db_pool_timeout_seconds: float = 10.0
    db_pool_recycle_seconds: int = 1800
    db_write_trace_enabled: bool = False

    # Отдельная база речи.
    speech_ms_database_url: str = ""
    # Вход и пользовательские сессии.
    speech_auth_mode: str = "open"
    speech_keycloak_issuer_url: str = ""
    speech_keycloak_jwks_url: str = ""
    speech_keycloak_audience: str = ""
    speech_keycloak_client_id: str = ""
    speech_keycloak_client_secret: str = ""
    speech_keycloak_url: str = ""
    speech_keycloak_realm: str = ""
    speech_public_base_url: str = ""
    speech_session_ttl_seconds: int = 43200
    speech_session_refresh_seconds: int = 900
    speech_role_aliases_json: dict[str, list[str]] = Field(default_factory=dict)
    speech_display_timezone: str = "Europe/Moscow"
    speech_ncc_ingest_interval_seconds: int = Field(default=900, ge=60, le=86400)
    speech_telphin_ingest_interval_seconds: int = Field(default=3600, ge=60, le=86400)
    speech_quality_interval_seconds: int = Field(default=900, ge=60, le=86400)
    speech_ncc_immediate_share: float = Field(default=0.5, ge=0, le=1)
    speech_night_from: int = Field(default=19, ge=0, le=23)
    speech_night_until: int = Field(default=7, ge=0, le=23)
    speech_quality_batch_limit: int = Field(default=100, ge=1, le=1000)
    speech_metrics_key: str = ""

    # Источник записей NCC.
    speech_ncc_base_url: AnyHttpUrl | str = ""
    speech_ncc_username: str = ""
    speech_ncc_api_token: str = ""
    speech_ncc_timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    speech_ncc_reporting_database_url: str = ""
    speech_ncc_reporting_db_password: str = ""
    speech_ncc_reporting_views: str = ""
    speech_ncc_reporting_timezone: str = "Europe/Moscow"
    speech_ncc_min_talk_seconds: int = Field(default=10, ge=0, le=3600)

    # Источник продаж Телфина.
    speech_telphin_base_url: str = "https://apiproxy.telphin.ru"
    speech_telphin_app_id: str = ""
    speech_telphin_app_secret: str = ""
    speech_telphin_timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    speech_telphin_min_talk_seconds: int = Field(default=10, ge=0, le=3600)
    speech_telphin_excluded_extensions: str = "13240*000,13240*007,13240*444"
    speech_telphin_excluded_groups: str = "22001"
    speech_telphin_history_from: str = ""
    speech_telphin_history_days_per_run: int = Field(default=7, ge=1, le=60)
    speech_phone_hash_pepper: str = ""

    # Оригиналы записей в MinIO.
    speech_storage_base_url: str = ""
    speech_storage_bucket: str = "speech-recordings"
    speech_storage_region: str = "us-east-1"
    speech_storage_access_key_id: str = ""
    speech_storage_secret_key: str = ""
    speech_storage_timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)

    # Существующий Whisper по HTTP.
    speech_whisper_base_url: str = ""
    speech_whisper_api_key: str = ""
    speech_whisper_model: str = "large-v3"
    speech_whisper_language: str = "ru"
    speech_whisper_timeout_seconds: float = Field(default=600.0, ge=30.0, le=1800.0)
    speech_whisper_vad_filter: bool = True

    # Прямой вызов LLM.
    speech_analysis_llm_base_url: str = ""
    speech_analysis_llm_api_key: str = Field(default="", repr=False)
    speech_analysis_llm_model: str = "qwen3.8-27b-fast"
    speech_analysis_llm_free_model: str = "qwen3.8-27b"
    speech_analysis_llm_candidate_model: str = ""
    speech_analysis_llm_timeout_seconds: float = Field(default=120.0, ge=10.0, le=280.0)
    speech_analysis_llm_max_tokens: int = Field(default=8192, ge=512, le=32768)
    speech_worker_api_token: str = Field(default="", repr=False)

    # Оценка продаж Телфина.
    speech_quality_llm_base_url: str = ""
    speech_quality_llm_api_key: str = ""
    speech_quality_llm_model: str = "qwen3.8-27b-fast"
    speech_quality_llm_timeout_seconds: float = Field(default=180.0, ge=10.0, le=1800.0)
    speech_quality_llm_max_tokens: int = Field(default=4096, ge=512, le=32768)

    # Способ получения записей NCC.
    speech_ncc_source_kind: str = "local_export"
    speech_ncc_export_dir: str = ""
    speech_ncc_session_anchor: str = ""

    @field_validator("backend_cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        """Прочитать разрешённые origins из списка или строкового значения настройки."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            return value
        raise TypeError("backend_cors_origins must be a comma-separated string or list")

    @field_validator(
        "cors_allow_methods",
        "cors_allow_headers",
        mode="before",
    )
    @classmethod
    def parse_string_list(cls, value: Any) -> list[str]:
        """Прочитать список строк из поддерживаемого формата настройки."""
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raise TypeError("list setting must be a comma-separated string or list")

    @field_validator("session_cookie_secure", mode="before")
    @classmethod
    def parse_session_cookie_secure(cls, value: Any) -> str:
        """Прочитать явный флаг защищённого cookie либо вернуть отсутствие."""
        mode = str(value or "auto").strip().lower()
        if mode not in {"auto", "always", "never"}:
            raise ValueError("session_cookie_secure must be auto, always, or never")
        return mode

    def validate_speech_runtime(self) -> None:
        """Проверить настройки HTTP-приложения перед запуском API.

        Вход — поля текущего Settings; результат — None. Неверный режим входа,
        неполные настройки Keycloak и отключение Secure в production вызывают
        RuntimeError. Режим open разрешён только в local и alpha.
        """
        from src.setting.security import (
            SPEECH_AUTH_MODES,
            SPEECH_OPEN_MODE_ENVIRONMENTS,
            assert_cookie_security,
        )

        assert_cookie_security(self)

        mode = self.speech_auth_mode.strip().lower()
        if mode not in SPEECH_AUTH_MODES:
            raise RuntimeError(
                f"SPEECH_AUTH_MODE={self.speech_auth_mode!r} is not a valid mode. "
                "Known: " + ", ".join(sorted(SPEECH_AUTH_MODES))
            )
        if mode == "open" and self.app_env.strip().lower() not in SPEECH_OPEN_MODE_ENVIRONMENTS:
            raise RuntimeError(
                "SPEECH_AUTH_MODE=open disables authentication entirely and is "
                "allowed only when APP_ENV is local or alpha. Configure Keycloak "
                "(SPEECH_KEYCLOAK_ISSUER_URL, SPEECH_KEYCLOAK_JWKS_URL, "
                "SPEECH_KEYCLOAK_AUDIENCE) and set SPEECH_AUTH_MODE=keycloak."
            )
        if mode == "keycloak":
            missing = [
                name
                for name, value in (
                    ("SPEECH_KEYCLOAK_ISSUER_URL", self.speech_keycloak_issuer_url),
                    ("SPEECH_KEYCLOAK_JWKS_URL", self.speech_keycloak_jwks_url),
                    ("SPEECH_KEYCLOAK_AUDIENCE", self.speech_keycloak_audience),
                    ("SPEECH_KEYCLOAK_URL", self.speech_keycloak_url),
                    ("SPEECH_KEYCLOAK_REALM", self.speech_keycloak_realm),
                    ("SPEECH_KEYCLOAK_CLIENT_ID", self.speech_keycloak_client_id),
                    ("SPEECH_KEYCLOAK_CLIENT_SECRET", self.speech_keycloak_client_secret),
                    ("SPEECH_PUBLIC_BASE_URL", self.speech_public_base_url),
                )
                if not value.strip()
            ]
            if missing:
                raise RuntimeError("SPEECH_AUTH_MODE=keycloak requires " + ", ".join(missing))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Загрузить и проверить окружение один раз при сборке процессов."""
    return Settings()
