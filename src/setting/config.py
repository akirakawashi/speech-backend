"""Настройки Speech Analytics: сервиса `speech-ms`, воркеров и задач конвейера.

Файл вырос из `app/core/config.py` монорепозитория `aicistent-backend`, где
настройки речи лежали в одном классе с настройками чата, RAG и воркфлоу. Здесь
оставлены только поля, которые читает код этого репозитория. Имена переменных
окружения и умолчания прежние: окружение, собранное для монорепозитория,
подходит без правок, лишние переменные молча пропускаются (`extra="ignore"`).

Единственное намеренное отличие — нет `DATABASE_URL`: это была база основного
приложения, и речь откатывалась на неё при пустом `SPEECH_MS_DATABASE_URL`.
См. `src/infrastructure/persistence/session.py`.
"""

import json
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

    # Поля, которыми типизирован `KeycloakTokenValidator` (`src/infrastructure/auth/keycloak.py`,
    # адаптер проверки JWT). Из окружения `speech-ms` их не читает: проверке
    # токена он передаёт не этот класс, а обёртку `_SpeechKeycloakSettings`
    # (`src/infrastructure/auth/identity.py`), которая подставляет сюда значения
    # `SPEECH_KEYCLOAK_*` и `SPEECH_ROLE_ALIASES_JSON`. Настраивать вход нужно
    # ими, а не этими пятью.
    keycloak_issuer_url: str = ""
    keycloak_jwks_url: str = ""
    keycloak_audience: str = ""
    keycloak_role_aliases_json: dict[str, list[str]] = Field(default_factory=dict)
    # В монорепозитории умолчанием стоял адрес суперадминистратора основного
    # приложения. Здесь пусто: обёртка речи передаёт в проверку пустую строку.
    keycloak_super_admin_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Флаг `Secure` на куках `speech_session`, `speech_login` и `speech_audio`:
    # `auto|always|never`.
    #
    # Строка, а не `bool | "auto"`, намеренно: pydantic-settings не приведёт
    # `"auto"` из переменной окружения к `bool`, а объединение `bool | str`
    # пропустило бы любую опечатку (`"alwais"`) молча — как строку мимо любой
    # проверки. Значение, от которого зависит защита куки, должно отвергаться
    # на старте, а не толковаться.
    #
    # Почему флаг вообще стал настройкой, а не выводится из `X-Forwarded-Proto`:
    # см. `src/presentation/http/cookies.py`. Коротко: наш же фронтовой nginx ЗАТИРАЛ этот
    # заголовок значением `$scheme` == `http` (с 2026-09-21 уже не затирает), а
    # TLS терминирует корпоративный nginx, которым мы не управляем. Находка
    # пентеста COOKIE-SEC (WSTG-SESS).
    #
    # `never` отвергается на старте только в продуктиве —
    # `src.presentation.http.cookies.assert_cookie_security`, зовётся из
    # `validate_speech_runtime`.
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

    # Бюджет соединений движка. Объявлен, а не оставлен умолчанием SQLAlchemy
    # (5 + 10 на движок): кластер Postgres общий с LLM, и умолчание незаметно
    # до пика. В каждом процессе речи движок один; расчёт на весь конвейер — в
    # описании `get_speech_ms_sessionmaker` (`src/infrastructure/persistence/session.py`).
    db_pool_size: int = 5
    db_max_overflow: int = 3
    db_pool_timeout_seconds: float = 10.0
    # Соединение пересоздаётся раньше, чем его закроет по простою прокси или
    # межсетевой экран: из пула не выдаётся уже мёртвое.
    db_pool_recycle_seconds: int = 1800
    # Журнал «какой процесс в какую таблицу пишет», только имена таблиц.
    # Почему это не `echo` SQLAlchemy — в `src/infrastructure/persistence/write_trace.py`.
    db_write_trace_enabled: bool = False

    # Настройки общего транспорта распознавания (`app/transcription`, перенесён
    # без правок) и проверки загруженного аудио (`src/presentation/uploads/audio.py`).
    # Конвейер звонков их НЕ читает: он собирает `TranscriptionConfig` сам из
    # `SPEECH_WHISPER_*` ниже (`src/infrastructure/clients/whisper.py`). Читает их только
    # `resolve_transcription_config` — путь «один файл → текст», которым в
    # монорепозитории пользуется черновик заявки из аудио.
    #
    # DEPRECATED: use TRANSCRIPTION_* instead. These are still read
    # when TRANSCRIPTION_PROVIDER is empty, so existing deployments keep
    # working; they are removed in phase 3.
    openrouter_enabled: bool = False
    openrouter_base_url: AnyHttpUrl | str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    openrouter_transcription_model: str = "openai/whisper-large-v3"
    openrouter_timeout_seconds: int = 60
    # Provider-neutral transcription. An empty provider means legacy
    # mode: the OPENROUTER_* block above is used instead.
    transcription_enabled: bool | None = None
    transcription_provider: str = ""
    transcription_base_url: str = ""
    transcription_api_key: str = ""
    transcription_model: str = ""
    transcription_timeout_seconds: float | None = None
    transcription_audio_window_seconds: float = 0.0
    transcription_response_format: str = "verbose_json"
    # Engine-specific request fields, JSON object of string values, e.g.
    # {"vad_filter": "true"}. Passed through to the provider unchanged.
    # NoDecode because pydantic-settings decodes JSON for complex fields inside
    # the source, ahead of every validator we write. Without it an empty
    # TRANSCRIPTION_EXTRA_OPTIONS= raises SettingsError at import and the
    # process never starts -- measured on the icai1 deployment 2026-09-04,
    # where compose passes the variable through empty. With it the raw string
    # reaches parse_transcription_extra_options below, which already maps ""
    # and None to {}.
    transcription_extra_options: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    support_ticket_audio_max_bytes: int = 25 * 1024 * 1024
    support_ticket_audio_allowed_mime_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["audio/mpeg", "audio/mp4", "audio/wav", "audio/webm", "audio/ogg"]
    )
    support_ticket_audio_allowed_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [".mp3", ".mp4", ".m4a", ".wav", ".webm", ".ogg"]
    )

    # --- Speech Analytics (speech-ms, порт 8096) -------------------------
    # Своя база данных и своё дерево миграций (`alembic`). Умолчания нет
    # намеренно: пустая строка — отказ и у миграций (`alembic/env.py`),
    # и у движка (`src/infrastructure/persistence/session.py`).
    speech_ms_database_url: str = ""
    # Режим входа. `open` — временный, пока проекту не выделен отдельный
    # Keycloak; допустим только в local/alpha, см. validate_speech_runtime.
    speech_auth_mode: str = "open"
    speech_keycloak_issuer_url: str = ""
    speech_keycloak_jwks_url: str = ""
    speech_keycloak_audience: str = ""
    # Клиент и его секрет для серверного обмена кода на токен (BFF).
    #
    # Браузер в этой схеме с Keycloak не разговаривает вовсе: клиент `speech`
    # объявлен confidential, и обмен кода требует секрета, который в браузер
    # положить нельзя. Поэтому обмен делает сервис, а браузеру достаётся кука.
    #
    # Секрет — именно секрет: наружу не печатается и в ответы не попадает.
    speech_keycloak_client_id: str = ""
    speech_keycloak_client_secret: str = ""
    # Адрес Keycloak и realm — теперь их читает и СЕРВИС, а не только контейнер
    # фронта. Причина в том же: обмен кода делает сервер, значит адреса realm
    # нужны ему самому. Имена намеренно те же, что у фронта, — один экземпляр
    # Keycloak, и две пары переменных с разными именами разъехались бы.
    speech_keycloak_url: str = ""
    speech_keycloak_realm: str = ""
    # Публичный адрес приложения, от которого собирается redirect_uri.
    #
    # Не выводится из запроса намеренно. За обратным прокси `request.url` несёт
    # внутренний адрес, а Keycloak сверяет redirect_uri побайтово со своим
    # списком: расхождение в схеме или порте даёт отказ уже после ввода пароля.
    # Здесь должно стоять ровно то, что зарегистрировано в клиенте.
    speech_public_base_url: str = ""
    # Сколько живёт сессия BFF без обновления. Меньше суток намеренно: сессия
    # хранится на сервере и отзывается мгновенно, но забытая вкладка не должна
    # оставаться входом в записи разговоров на неделю.
    speech_session_ttl_seconds: int = 43200
    # Как часто перечитываются роли живой сессии. Обновление токена по
    # refresh_token заодно отвечает на вопрос «жив ли человек в Keycloak»:
    # `invalid_grant` означает, что его отключили или он вышел, и тогда сессия
    # гасится и у нас. Пятнадцать минут — потолок запаздывания отзыва прав.
    speech_session_refresh_seconds: int = 900
    # Соответствие групп Keycloak ролям сервиса. Пусто — берётся умолчание из
    # src/domain/calls/contracts.py с группами whisper-icg-*.
    speech_role_aliases_json: dict[str, list[str]] = Field(default_factory=dict)
    # Часовой пояс отображения. Хранится всё в timestamptz; это только показ.
    speech_display_timezone: str = "Europe/Moscow"
    # Ключ к `/metrics`. Пустой — ручка не зарегистрирована вовсе (не «есть,
    # но отвечает 401»): глубина очереди и число звонков — сведения о
    # работе колл-центра, и ответ «тут есть что-то за ключом» сам по себе
    # сообщение. Секрет, а не настройка: наружу не печатается.
    speech_metrics_key: str = ""

    # Naumen Contact Center. Два независимых источника, и это не дублирование:
    # REST отдаёт аудиозапись по идентификатору сессии и справочники, но списка
    # звонков в нём нет вовсе — список берётся из отчётной базы. Отсюда две
    # строки подключения и две учётные записи.
    #
    # REST: аутентификация заголовками `Username` и `X-API-Key` (раздел 2
    # руководства). Базовый адрес включает версию, как у SPEECH_ANALYSIS_LLM_BASE_URL: путь
    # `/callrecord/download` дописывается к нему.
    speech_ncc_base_url: AnyHttpUrl | str = ""
    speech_ncc_username: str = ""
    speech_ncc_api_token: str = ""
    # Скачивание записи — не запрос к справочнику: минуты разговора едут потоком.
    speech_ncc_timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    # Отчётная база NCC (SPEECH_NCC_SOURCE_KIND=reporting_view): представления со
    # списком звонков. Чужая база: только чтение, мы в ней ничего не создаём и не
    # мигрируем. Драйвер — asyncpg, тот же, что у нашей базы: второй стек ради
    # двух выборок в сутки не нужен.
    #
    # Адрес без пароля, пароль отдельно: в адресе его пришлось бы кодировать для
    # URL, а строка подключения с секретом внутри попадает в журналы целиком.
    speech_ncc_reporting_database_url: str = ""
    speech_ncc_reporting_db_password: str = ""
    # Представления и проекты: `calls_detail=Волна,tss_7tel_calls_detail=7Телеком`.
    # Проект обязателен: поле проекта в представлении пустое у всех строк
    # (проверено 2026-09-15), и без имени звонки двух проектов неразличимы.
    speech_ncc_reporting_views: str = ""
    # Время в представлениях — `timestamp without time zone` по часам сервера NCC.
    speech_ncc_reporting_timezone: str = "Europe/Moscow"
    # Разговор короче порога в приём не идёт. Секунды: `speakingtime` в
    # представлениях в секундах (медиана 150, ни одной строки от 10000).
    speech_ncc_min_talk_seconds: int = Field(default=10, ge=0, le=3600)

    # Телфин — второй источник звонков: исходящие ШПД с ВАТС «ГИК» (фича 14,
    # решения S-35, S-36). Список — REST API (`call_history`), запись — по
    # постоянной ссылке хранилища без аутентификации. Приложение API — trusted,
    # Call API; пустой App ID выключает источник: прогон `--source telphin`
    # откажет и назовёт недостающее.
    speech_telphin_base_url: str = "https://apiproxy.telphin.ru"
    speech_telphin_app_id: str = ""
    speech_telphin_app_secret: str = ""
    speech_telphin_timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    # Разговор короче порога в приём не идёт: 10 с, как у NCC (S-36).
    speech_telphin_min_talk_seconds: int = Field(default=10, ge=0, le=3600)
    # Кроме служебных добавочных и группы «Переадресации» (S-36): имена
    # добавочных и идентификаторы групп через запятую.
    speech_telphin_excluded_extensions: str = "13240*000,13240*007,13240*444"
    speech_telphin_excluded_groups: str = "22001"
    # С какой даты брать историю (S-36: 2026-09-01). Пусто — только вчера и
    # сегодня. История идёт полосой `backfill`, порциями по N суток за запуск.
    speech_telphin_history_from: str = ""
    speech_telphin_history_days_per_run: int = Field(default=7, ge=1, le=60)
    # Соль псевдонима номера клиента (ING-02). Пусто — хранится только маска:
    # хеш без соли подбирается перебором номеров (`src.domain.calls.phone`).
    speech_phone_hash_pepper: str = ""

    # Собственное объектное хранилище аудиозаписей (S3-совместимое). Своё, а
    # не корпоративное: та учётная запись read-only на стороне провайдера,
    # писать ею нельзя в принципе.
    speech_storage_base_url: str = ""
    speech_storage_bucket: str = "speech-recordings"
    speech_storage_region: str = "us-east-1"
    speech_storage_access_key_id: str = ""
    speech_storage_secret_key: str = ""
    speech_storage_timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)

    # Распознавание речи конвейером. Свои настройки, а не общие
    # `TRANSCRIPTION_*` выше: те принадлежат пути «один файл → текст», который
    # у основного приложения на альфе выключен и смотрит в заглушку, а
    # конвейеру нужен настоящий Whisper. Одна пара настроек на два назначения
    # означала бы, что выключить одно нельзя, не выключив другое.
    speech_whisper_base_url: str = ""
    speech_whisper_api_key: str = ""
    speech_whisper_model: str = "large-v3"
    speech_whisper_language: str = "ru"
    # Часовая запись на загруженном GPU обрабатывается минутами. Таймаут
    # соотнесён с арендой задания расшифровки (900 с) и заведомо меньше её:
    # клиент обязан сдаться раньше, чем задание отберут по истечении аренды.
    speech_whisper_timeout_seconds: float = Field(default=600.0, ge=30.0, le=1800.0)
    # Параметры движка, уходящие в запрос как есть. `vad_filter` у
    # faster-whisper отсекает тишину и заметно ускоряет телефонные записи, где
    # пауз много.
    speech_whisper_vad_filter: bool = True

    # Бэкенд владеет промптами, версиями и выбором модели анализа.
    speech_analysis_llm_base_url: str = ""
    speech_analysis_llm_api_key: str = Field(default="", repr=False)
    speech_analysis_llm_model: str = "qwen3.8-27b-fast"
    speech_analysis_llm_free_model: str = "qwen3.8-27b"
    speech_analysis_llm_candidate_model: str = ""
    speech_analysis_llm_timeout_seconds: float = Field(default=120.0, ge=10.0, le=280.0)
    speech_analysis_llm_max_tokens: int = Field(default=8192, ge=512, le=32768)
    # Пустой служебный токен закрывает подготовку заданий для воркера.
    speech_worker_api_token: str = Field(default="", repr=False)

    # Оценка звонков по скрипту (`python -m src.presentation.cli.evaluate`). Языковая
    # модель вызывается напрямую; скрипт и промпт оценки живут в коде.
    # Адрес базовый, с `/v1` на конце.
    speech_quality_llm_base_url: str = ""
    speech_quality_llm_api_key: str = ""
    speech_quality_llm_model: str = "qwen3.8-27b-fast"
    # Скрипт и разговор на десять минут — тысячи токенов на вход, а в ответе —
    # вердикт по каждому этапу с пояснением.
    speech_quality_llm_timeout_seconds: float = Field(default=180.0, ge=10.0, le=1800.0)
    speech_quality_llm_max_tokens: int = Field(default=4096, ge=512, le=32768)

    # Откуда берётся суточный пакет: reporting_view | sequence | local_export |
    # storage | rest. На узле — `reporting_view`: представления отчётной базы NCC
    # (SPEECH_NCC_REPORTING_* выше). Умолчание `local_export` — каталог с
    # выгрузкой, переданной колл-центром, для разработки без доступа к NCC.
    speech_ncc_source_kind: str = "local_export"
    # Каталог выгрузки для `local_export`. Настоящие записи — разговоры
    # реальных клиентов, поэтому путь ведёт наружу репозитория и в образ
    # не попадает.
    speech_ncc_export_dir: str = ""
    # UUID проектов NCC для `rest`, через запятую.
    #
    # Перечисления проектов в API нет — проверено на живой площадке: списка
    # `/projects` не существует ни в v1, ни в v2, а все 315 идентификаторов,
    # которые отдают `/employees`, `/ous` и `/ous/root/subous`, принадлежат
    # классам `CoreOU` и сотрудникам. UUID проекта берётся из адресной строки
    # консоли NCC (руководство, раздел 6.1): `.../published?uuid=<UUID>`.
    #
    # Отсюда и настройка: значение, которое может назвать только человек с
    # доступом к консоли, обязано приходить снаружи, а не угадываться.
    speech_ncc_projects: str = ""
    # Опорный идентификатор сессии для `sequence`.
    #
    # Идентификаторы NCC — сквозной счётчик (`node_domain_D_nauss_N_<epoch>_<seq>`);
    # `HEAD /callrecord/download` подтверждает существование сессии и отдаёт дату,
    # не скачивая тела. Обход счётчика от свежей опорной точки восстанавливает
    # список звонков за период — это и есть перечисление, которого нет отдельным
    # эндпойнтом.
    #
    # Одна точка покрывает одно поколение нумерации (одну `epoch`). Взять её
    # можно из любого недавнего звонка; сменится `epoch` при пересборке NCC —
    # нужна свежая. Проверено на живой площадке 2026-09-12.
    speech_ncc_session_anchor: str = ""
    # Плечи звонка для перебора на каждом `seq` (`domain`,`nauss`), через запятую.
    # У каждого `seq` существует ровно одно сочетание; порядок — порядок перебора.
    speech_ncc_session_domains: str = "0,1,2,3"
    speech_ncc_session_nauss: str = "0"

    @field_validator("backend_cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        """Прочитать разрешённые origins из списка или строкового значения настройки."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            return value
        raise TypeError("backend_cors_origins must be a comma-separated string or list")

    @field_validator("keycloak_role_aliases_json", mode="before")
    @classmethod
    def parse_keycloak_role_aliases(cls, value: Any) -> dict[str, list[str]]:
        """Прочитать JSON соответствий групп Keycloak ролям приложения."""
        if value in (None, ""):
            return {}
        if isinstance(value, dict):
            return {
                str(role): [str(alias) for alias in aliases if str(alias).strip()]
                for role, aliases in value.items()
                if isinstance(aliases, list)
            }
        if isinstance(value, str):
            import json

            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise TypeError("keycloak_role_aliases_json must decode to an object")
            return cls.parse_keycloak_role_aliases(parsed)
        raise TypeError("keycloak_role_aliases_json must be a JSON object string or dict")

    @field_validator("keycloak_super_admin_emails", mode="before")
    @classmethod
    def parse_super_admin_emails(cls, value: Any) -> list[str]:
        """Прочитать список email администраторов из значения настройки."""
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [email.strip().lower() for email in value.split(",") if email.strip()]
        if isinstance(value, list):
            return [str(email).strip().lower() for email in value if str(email).strip()]
        raise TypeError("keycloak_super_admin_emails must be a comma-separated string or list")

    @field_validator(
        "support_ticket_audio_allowed_mime_types",
        "support_ticket_audio_allowed_extensions",
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

    # `X | None = None` says "unset means None". An environment variable set to
    # the empty string is not unset -- pydantic receives "" and fails to parse it
    # as a bool or a float, and the process dies at import with a validation
    # error naming a field the operator never touched. .env.example ships both of
    # these empty, meaning exactly "unset", so the two spellings have to agree.
    # Measured on the icai1 deployment 2026-09-04.
    @field_validator("transcription_enabled", "transcription_timeout_seconds", mode="before")
    @classmethod
    def empty_string_means_unset(cls, value: Any) -> Any:
        """Считать пустую строку отсутствующим значением необязательной настройки."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("transcription_extra_options", mode="before")
    @classmethod
    def parse_transcription_extra_options(cls, value: Any) -> dict[str, str]:
        """Прочитать JSON дополнительных параметров провайдера."""
        if value in (None, ""):
            return {}
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise ValueError("transcription_extra_options must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("transcription_extra_options must be a JSON object")
        return {str(k): str(v) for k, v in parsed.items()}

    @field_validator("transcription_provider", mode="before")
    @classmethod
    def parse_transcription_provider(cls, value: Any) -> str:
        """Привести имя провайдера к разрешённому значению настройки."""
        provider = str(value or "").strip().lower()
        if provider not in {"", "openrouter", "openai_compatible"}:
            raise ValueError("transcription_provider must be openrouter or openai_compatible")
        return provider

    @field_validator("session_cookie_secure", mode="before")
    @classmethod
    def parse_session_cookie_secure(cls, value: Any) -> str:
        """Прочитать явный флаг защищённого cookie либо вернуть отсутствие."""
        mode = str(value or "auto").strip().lower()
        if mode not in {"auto", "always", "never"}:
            raise ValueError("session_cookie_secure must be auto, always, or never")
        return mode

    def validate_speech_runtime(self) -> None:
        """Правила запуска `speech-ms`. Вызывается из его `create_app`.

        Воркеры и задачи конвейера его не зовут: у них нет ни входа, ни кук.

        Главное здесь то, ценой ошибки в чём являются записи разговоров
        реальных клиентов: режим `open` отключает аутентификацию целиком. Он
        существует, пока проекту не выделен отдельный Keycloak, и его
        единственная защита — сетевое ограничение. Разрешать такой режим там,
        где приложение доступно пользователям, нельзя, а обнаруживать ошибку по
        факту чужого захода — поздно. Поэтому процесс не стартует.

        Второе — флаг `Secure` на куках. Без этой проверки сервис поднимался с
        `SESSION_COOKIE_SECURE=never` в любом окружении, раздавая
        `speech_session`, `speech_login` и `speech_audio` без `Secure`.
        """
        # Импорт внутри метода: модули домена читают настройки, и импорт на
        # уровне файла замкнул бы цикл.
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
            # Первые три нужны, чтобы проверить токен; остальные четыре — чтобы
            # его вообще получить. Клиент `speech` конфиденциальный, обмен кода
            # делает сервис (ADR-SPEECH-0002), и без секрета войти не может
            # никто. Обнаруживать это на экране входа первого пользователя —
            # поздно: отказ приходит после правильно введённого пароля.
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
