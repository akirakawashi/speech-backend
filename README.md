# Speech Backend

Отдельный бэкенд распознавания и анализа разговоров: приём записей,
Whisper, разбор разговоров, оценка по скрипту продаж, поиск, отчёты и
администрирование. HTTP-контракт — `speech.api` 3.0.

Рабочий код находится в `src/`. Правила разработки — в [AGENTS.md](AGENTS.md),
подробности архитектуры и неочевидные решения — в [docs/](docs/README.md).

12.10.2026 сюда скопирована текущая реализация из `whisper-backend`.
Воркер выделен в самостоятельный `speech-worker`; прежние команды в этой копии
сохранены. Целевые границы описаны в [разделении проектов](docs/service-boundaries.md).
Исходная папка сохранена. У проекта собственное окружение `.venv`; рабочие
секреты и окружение исходной папки не переносились.

Сценарии Dify и проверки их YAML хранятся в `speech-worker/dify/speech/`.
В бэкенде их копии удалены; HTTP API использует сохранённые результаты.
Порядок владения сценариями описан в
[границах сервисов](docs/service-boundaries.md#сценарии-dify).

## Структура

| Путь | Ответственность |
|---|---|
| `src/presentation/` | HTTP-роутеры, разбор аргументов CLI, cookies и ответы транспорта |
| `src/application/` | Сценарии, Pydantic DTO и порты внешних операций |
| `src/domain/` | Скрипт продаж, оценки, роли, классификация, календарные и другие бизнес-правила |
| `src/infrastructure/` | PostgreSQL, очередь, Whisper, источники, хранилище, ffmpeg и файлы отчётов |
| `src/setting/` | Pydantic Settings и проверка конфигурации |
| `src/di/` | Сборка сценариев, адаптеров и ресурсов процессов |
| `alembic/` | Миграции отдельной базы речи |
| `tests/` | Правила, сценарии, HTTP, адаптеры, транзакции и границы слоёв |

Направления импортов и расположение DTO: [архитектура](docs/architecture.md).
Роутеры вызывают сценарии, сценарии работают через порты. SQL, HTTP-клиенты и
объекты SDK не попадают в domain/application. Настройки передаются явно.

## Запуск

```bash
uv sync --locked
cp .env.example .env
# Заполнить адрес отдельной базы и необходимых сервисов.
uv run --locked alembic -c alembic.ini upgrade head
uv run --locked uvicorn src.di.http:create_app --factory --port 8096
```

Все переменные описаны в [.env.example](.env.example). Пустой
`SPEECH_MS_DATABASE_URL` отклоняется: подстановки базы другого приложения нет.
`SPEECH_AUTH_MODE=open` разрешён только при `APP_ENV=local` или `alpha`.
Воркеру нужны ffmpeg и зависимости выбранных этапов: источник и хранилище для
приёма, Whisper для распознавания, приложение Dify для разбора.

| Команда после `uv run --locked python -m` | Назначение |
|---|---|
| `src.presentation.cli.worker --lanes daily` | Выполнить этапы конвейера |
| `src.presentation.cli.daily_ingest --yesterday` | Принять звонки за день |
| `src.presentation.cli.backfill status` | Посмотреть прогресс истории |
| `src.presentation.cli.evaluate --all --skip-failed --limit 100` | Выбрать порцию звонков для оценки |
| `src.presentation.cli.fill_reasons` | Заполнить причины ранее разобранных звонков |
| `src.presentation.cli.reanalyze` | Поставить повторный анализ |
| `src.presentation.cli.seed_classifier` | Загрузить дерево владельца |
| `src.presentation.cli.seed_call_reasons` | Загрузить справочник причин |
| `src.presentation.cli.classifier_backfill` | Дополнить дерево классификациями источника |
| `src.presentation.cli.directory_sync` | Синхронизировать справочники NCC |
| `src.presentation.cli.compare_models` | Сравнить кандидата и контрольную модель |
| `src.presentation.cli.prune` | Проверить удаление по сроку хранения |

Параметры каждой команды — `--help`. Оценка, загрузка справочников, повторный
анализ и очистка по умолчанию показывают план; `--apply` выполняет изменения.
Служебные HTTP-ручки: `/health`, `/ready`, `/version`, `/metrics`.

Все точки запуска находятся в `src/`. Для локальных скриптов этой папки:
`SPEECH_BACKEND=speech-backend ../local-dev/run.sh api`.
Пакет `app` удалён после перевода потребителей на новые точки входа;
подробности — в [архитектуре](docs/architecture.md#точки-запуска-и-совместимость).

В Docker:

```bash
docker compose --profile operations build
docker compose --profile operations run --rm migrate-speech
docker compose up -d --wait
```

Образ API использует фабрику `src.di.http:create_app --factory`.
Compose запускает только API и явную команду миграций. PostgreSQL и MinIO
подключаются отдельно; обработка выполняется проектом `speech-worker`.
Настройки сети, отдельные роли базы и порядок запуска описаны в
[развёртывании](docs/deployment.md).

## База и проверки

Рабочая локальная база — `speech_whisper`, тестовая — `speech_whisper_test`.
Перед проверками применяются миграции этой папки:

```bash
SPEECH_MS_DATABASE_URL=postgresql+asyncpg://…/speech_whisper_test \
  uv run --locked alembic -c alembic.ini upgrade head
export SPEECH_POSTGRES_TEST_URL=postgresql+asyncpg://…/speech_whisper_test
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy .
SPEECH_REQUIRE_LIVE_PROOFS=1 uv run --locked pytest -q -rs
```

Тесты PostgreSQL очищают таблицы: рабочая база для них не подходит.
Базы монорепозитория `speech` и `speech_test` не используем. Без адреса тестовой
базы соответствующие тесты пропускаются; для обязательных проверок на стенде
устанавливается `SPEECH_REQUIRE_LIVE_PROOFS=1`. Дополнительные тесты внешних
сервисов требуют собственного окружения; пропуски отображаются через `-rs`.

`/ready` проверяет подключение и ревизию `sp_0015_drop_masking`.
Ожидаемая ревизия находится в `src/infrastructure/persistence/session.py`.
Пулы и транзакции: [время жизни ресурсов](docs/architecture.md#транзакции-и-ресурсы).

### Снимки контракта

`tests/test_contract_compatibility.py` сверяет схему с `tests/contracts/`.
Архитектурный перенос сохраняет версию 3.0 и её снимок. При намеренном
изменении API сначала выбирается новая версия, затем обновляется снимок:

```bash
uv run --locked python scripts/update_contract_snapshots.py
```

Старые снимки сохраняются. `openapi.speech.json` используется фронтендом;
его пересоздают при изменении HTTP-контракта. Правила проверок — в
[документе разработки](docs/development.md).

## Откуда код

Основа — `aicistent-backend`, ветка `feature/speech-ingest-day-share`, коммит
`fc21fd3` от 09.10.2026. С того дня папка развивается отдельно; обновление
копированием из монорепозитория запрещено. Прод продолжает использовать старый
бэкенд. Git-репозиторий `speech-backend` создан владельцем и сохранён при
копировании.

09.10.2026 здесь удалено обезличивание расшифровки: после `transcribe` сразу
идёт `analyze`, текст хранится один раз в `text_raw`. Этапа `mask`, guardrails,
ручки раскрытия текста и права `reveal_pii` нет. Номер клиента остаётся маской
и хешем. Оценка действует относительно времени расшифровки и снимается при
изменении ролей. Скрипт продаж применяется только к Телфину.

Миграция `sp_0015_drop_masking` выполняется при остановленных воркерах:
она перестраивает таблицу реплик и индекс поиска. В монорепозитории и на проде
обезличивание продолжает работать. Подробности — в
[контексте проекта](docs/project-context.md#обезличивание-и-оценка).

11.10.2026 реализация в `whisper-backend` разделена на шесть слоёв. Зависимости сценариев заменяются
через порты и DI; дубли исходников в `app/` удалены, старые точки запуска
первоначально оставлены обёртками. С 12.10.2026 работа над бэкендом ведётся
в `speech-backend`. В этой папке переходные обёртки `app/` удалены,
локальные скрипты используют CLI и фабрику HTTP из `src/`.
