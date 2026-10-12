"""Форма `docker-compose.yml` для конвейера речи.

Проверяется не то, что файл разбирается, а решения, которые в нём записаны и
которые легко потерять при следующей правке: какой профиль поднимает воркеров,
где ключ Dify обязателен, а где требовать его значило бы запретить запуск.

Дешевле теста здесь нет ничего: ошибка в профиле обнаруживается на узле при
выкатке, а ошибка в обязательности переменной — ночью, вечным перезапуском.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

COMPOSE: dict[str, Any] = yaml.safe_load(
    pathlib.Path("docker-compose.yml").read_text(encoding="utf-8")
)
SERVICES: dict[str, Any] = COMPOSE["services"]

#: Постоянные службы конвейера: поднимаются вместе со всем остальным.
WORKERS = ("speech-worker-daily", "speech-worker-backfill")

#: Задачи: отработали и завершились, расписание снаружи.
TASKS = ("speech-daily-ingest", "speech-prune", "speech-backfill")

#: Оценка по скрипту — тоже задача, но окружение у неё своё, а не общее с
#: воркерами: проверки про ключи конвейера к ней не относятся.
EVALUATE = "speech-evaluate"


@pytest.mark.parametrize("service", WORKERS)
def test_the_workers_start_with_the_rest_of_the_stack(service: str) -> None:
    """`up -d` поднимает воркеров (SP-307).

    До появления обработчиков они стояли в своём профиле, и это было верно:
    воркер с неполным реестром отказывался стартовать, а `restart:
    unless-stopped` превращал отказ в бесконечный перезапуск.
    """
    assert SERVICES[service]["profiles"] == ["managed-runtime"]
    assert SERVICES[service]["restart"] == "unless-stopped"


@pytest.mark.parametrize("service", (*TASKS, EVALUATE))
def test_a_task_is_not_raised_by_up(service: str) -> None:
    """Задача с расписанием снаружи не должна подниматься как служба.

    Поднявшись, суточный прогон отработал бы за вчера и завершился, а `restart`
    у него нет — контейнер остался бы в `Exited`, и мониторинг увидел бы
    остановленную службу там, где всё в порядке.
    """
    assert SERVICES[service]["profiles"] == ["operations"]
    assert "restart" not in SERVICES[service]


@pytest.mark.parametrize("service", WORKERS)
def test_the_workers_refuse_to_start_without_the_dify_key(service: str) -> None:
    """`up` обязан остановиться сразу, а не поднять перезапускающийся контейнер.

    Воркер берёт этап анализа, а клиент анализа без ключа не собирается — и
    правильно не собирается: воркер, тихо решивший, что анализировать нечем,
    оставил бы суточный пакет без тем, ничего не сказав. Два контейнера, вечно
    падающих в штатном профиле, обесценивают журнал: настоящий отказ в нём уже
    не заметить.
    """
    key = SERVICES[service]["environment"]["SPEECH_DIFY_APP_KEY"]

    assert ":?" in key, "ключ не объявлен обязательным — отказ станет перезапуском"
    assert "SPEECH_DIFY_APP_KEY" in key


@pytest.mark.parametrize("service", TASKS)
def test_the_tasks_do_not_demand_a_key_they_never_use(service: str) -> None:
    """Ни приём, ни удаление по сроку, ни перечисление дат к Dify не ходят.

    Требовать у них ключ значило бы запретить их запуск в контуре, где
    приложения Dify ещё нет, — а разбирать историю и удалять записи по сроку он
    там не мешает.
    """
    key = SERVICES[service]["environment"]["SPEECH_DIFY_APP_KEY"]

    assert ":?" not in key, f"{service} требует ключ, которым не пользуется"


@pytest.mark.parametrize("service", (*WORKERS, "speech-daily-ingest", "speech-backfill"))
def test_everything_that_enumerates_calls_sees_the_export_directory(service: str) -> None:
    """`SPEECH_NCC_EXPORT_DIR` без монтирования называет путь, которого нет.

    Переменная передавалась бы, каталог — нет, и сбор из локальной выгрузки не
    работал бы ни при какой настройке.
    """
    mounts = SERVICES[service]["volumes"]

    assert any("/var/lib/speech/export" in str(mount) for mount in mounts)
    assert any(str(mount).endswith(":ro") for mount in mounts), (
        "каталог с разговорами реальных клиентов смонтирован на запись"
    )


@pytest.mark.parametrize("service", (*WORKERS, "speech-daily-ingest", "speech-backfill"))
def test_the_reporting_view_source_can_be_configured_in_the_container(service: str) -> None:
    """`env_file` у служб конвейера нет: чего нет в `environment`, того нет вовсе.

    Источник `reporting_view` без адреса, пароля и представлений отказывает на
    старте прогона — но только если переменные вообще доходят до контейнера.
    Не дойди они, отказ был бы тем же, а исправить его правкой `.env` было бы
    нельзя.
    """
    env = SERVICES[service]["environment"]

    for name in (
        "SPEECH_NCC_REPORTING_DATABASE_URL",
        "SPEECH_NCC_REPORTING_DB_PASSWORD",
        "SPEECH_NCC_REPORTING_VIEWS",
        "SPEECH_NCC_REPORTING_TIMEZONE",
        "SPEECH_NCC_MIN_TALK_SECONDS",
    ):
        assert name in env, name
    assert ":?" not in env["SPEECH_NCC_REPORTING_DB_PASSWORD"], (
        "другим источникам база не нужна — обязательность запретила бы их запуск"
    )


def test_the_prune_task_checks_before_it_deletes() -> None:
    """Единственная необратимая задача проекта не удаляет от опечатки.

    `docker compose run speech-prune`, набранное по памяти, должно показать, что
    удалило бы, а не удалить.
    """
    assert "--apply" not in SERVICES["speech-prune"]["command"]


def test_the_evaluation_task_counts_before_it_calls_the_model() -> None:
    """Тот же довод: запуск по памяти показывает, сколько звонков ждут, а модель не зовёт.

    Порция при этом задана: расписание берёт сотню, а не весь архив, и не
    возвращается к звонкам, чья оценка уже не удалась.
    """
    command = SERVICES[EVALUATE]["command"]

    assert "--apply" not in command
    assert "--all" in command and "--skip-failed" in command
    assert command[command.index("--limit") + 1] == "100"


def test_the_evaluation_task_gets_the_model_and_nothing_else() -> None:
    """Без строки в `environment` настройки модели до контейнера не доходят вовсе.

    Обратная сторона — задаче не нужны ключи конвейера: она читает
    текст из базы и зовёт модель. Унаследуй она общее окружение
    воркеров, ключ хранилища записей и учётки источников доставались бы
    процессу, которому они ни к чему.
    """
    env = SERVICES[EVALUATE]["environment"]

    for name in (
        "SPEECH_MS_DATABASE_URL",
        "SPEECH_QUALITY_LLM_BASE_URL",
        "SPEECH_QUALITY_LLM_API_KEY",
        "SPEECH_QUALITY_LLM_MODEL",
    ):
        assert name in env, name
    assert ":?" not in env["SPEECH_QUALITY_LLM_BASE_URL"], (
        "проверка без --apply модель не зовёт — обязательность запретила бы и её"
    )
    foreign = [
        name
        for name in env
        if name.startswith(("SPEECH_STORAGE_", "SPEECH_TELPHIN_", "SPEECH_NCC_", "SPEECH_DIFY_"))
        or name.startswith("SPEECH_WHISPER_")
    ]
    assert foreign == [], foreign


def test_the_evaluation_task_declares_how_many_connections_it_may_hold() -> None:
    """По расписанию задача работает почти всё время, и её пул — часть бюджета кластера.

    Умолчание (пять и три сверху) рассчитано на службу с параллельными
    запросами. Оценка идёт по одному звонку, и оставить ей восемь соединений
    значило бы занять их в общем с Dify кластере без нужды.
    """
    env = SERVICES[EVALUATE]["environment"]

    assert (env["DB_POOL_SIZE"], env["DB_MAX_OVERFLOW"]) == ("1", "1")


def test_the_backfill_task_reports_before_it_walks() -> None:
    """Тот же довод: команда по умолчанию не должна начинать обход шестидесяти суток."""
    assert SERVICES["speech-backfill"]["command"][-1] == "status"


@pytest.mark.parametrize("service", (*WORKERS, *TASKS, EVALUATE))
def test_the_pipeline_runs_unprivileged_and_read_only(service: str) -> None:
    """Конвейер работает с чужой речью: лишние права ему не нужны ни на шаг."""
    spec = SERVICES[service]

    assert spec["read_only"] is True
    assert spec["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in spec["security_opt"]


def test_the_workers_get_room_for_audio_in_tmpfs() -> None:
    """Сюда пишется скачанная запись, её даунмикс и каналы.

    Умолчания `/tmp` не хватит на часовой разговор, и этап приёма упал бы на
    записи, которая до хранилища так и не доехала.
    """
    for service in WORKERS:
        assert any("size=" in str(entry) for entry in SERVICES[service]["tmpfs"])


@pytest.mark.parametrize("service", WORKERS)
def test_a_worker_is_given_time_to_finish_before_it_is_killed(service: str) -> None:
    """`request_stop` без срока не значит ничего.

    Воркер обрабатывает `SIGTERM` и доводит текущее задание — так написано и в
    коде, и в его собственной пояснительной строке. При умолчании Docker в
    десять секунд обработчик успевал поставить флаг, и на этом всё: расшифровка,
    за которую уже заплачено временем GPU, выбрасывалась на каждой выкатке.

    Проверяется наличие и порядок величины, а не точное число: смысл в том, что
    срок задан осознанно и измеряется минутами, а не в том, что он равен ста
    восьмидесяти секундам.
    """
    grace = SERVICES[service].get("stop_grace_period")

    assert grace is not None, "без этого ключа Docker убьёт воркер через десять секунд"
    assert grace.endswith("s")
    assert 60 <= int(grace[:-1]) <= 900, (
        "меньше минуты не хватит даже среднему звонку, больше аренды — бессмысленно: "
        "задание всё равно вернётся в очередь"
    )


def test_the_service_is_told_how_to_flag_its_cookies() -> None:
    """`SESSION_COOKIE_SECURE` обязана доезжать до `speech-ms` объявлением.

    `env_file: .env` у `speech-ms` нет, поэтому без строки в `environment`
    оператор, поставивший `SESSION_COOKIE_SECURE=always`, получал бы умолчание
    `auto`: переменная объявлена, а до процесса не доезжает.

    Здесь же держится и вторая половина защиты: пока переменная не доезжает,
    проверка на старте в `validate_speech_runtime` не видит `never` вовсе и
    ничего не запрещает.
    """
    service = SERVICES["speech-ms"]

    assert "env_file" not in service, (
        "если он появится, эту строку можно убрать — но не раньше, и осознанно"
    )
    assert service["environment"]["SESSION_COOKIE_SECURE"] == "${SESSION_COOKIE_SECURE:-auto}"


def test_only_the_browser_facing_service_needs_the_cookie_flag() -> None:
    """Список сервисов, которым флаг нужен, держится тем, кто ставит куки.

    Куки браузеру в этом файле ставит одна служба — `speech-ms`
    (`app/speech_service/*`). Воркеры и задачи исполняют тот же код, но HTTP не
    обслуживают, и объявлять им флаг значило бы утверждать, что он на что-то
    влияет.
    """
    got = {
        name
        for name, spec in SERVICES.items()
        if "SESSION_COOKIE_SECURE" in (spec.get("environment") or {})
    }

    assert got == {"speech-ms"}


# --------------------------------------------------------------------------- #
# Интерфейс
# --------------------------------------------------------------------------- #


def test_the_interface_is_declared_at_all() -> None:
    """Образ фронта собирался, а запускать его было негде.

    Служба появилась только 2026-09-14: до этого `build-and-save-images.sh`
    клал образ в архив, а ни одного места, где он объявлен к запуску, не
    существовало. Собранный и никуда не подключённый образ — это выкатка,
    которая молча не приезжает.
    """
    assert "speech-frontend" in SERVICES


def test_the_interface_is_not_published_to_the_world() -> None:
    """На петле, как и всё остальное: наружу отдаёт прокси, а не контейнер."""
    ports = SERVICES["speech-frontend"]["ports"]

    assert len(ports) == 1
    assert ports[0].startswith("127.0.0.1:"), ports[0]
    assert ports[0].endswith(":3000"), "внутри образ слушает 3000"


def test_the_interface_reads_its_addresses_at_runtime() -> None:
    """Ради этого фронт и собирается без аргументов сборки.

    Все настройки приходят окружением контейнера и отдаются через
    `/config.json`. Если хоть одна переедет в аргументы сборки, образ снова
    станет пригодным только для того стенда, под который собран.
    """
    env = SERVICES["speech-frontend"]["environment"]

    for name in (
        "SPEECH_AUTH_MODE",
        "SPEECH_KEYCLOAK_URL",
        "SPEECH_KEYCLOAK_REALM",
        "SPEECH_KEYCLOAK_CLIENT_ID",
        "SPEECH_API_URL",
        # Адрес `speech-ms` для проброса `/api/*` самим фронтом (2026-09-15).
        "SPEECH_API_PROXY_TARGET",
    ):
        assert name in env, name
    assert "build" not in SERVICES["speech-frontend"], (
        "исходники фронта в соседнем репозитории; сборка объявлена там же"
    )


def test_the_interface_checks_the_one_thing_it_must_do_itself() -> None:
    """Проверка живости бьёт в `/config.json`, а не в корень.

    Корень отдаёт страницу и при сломанном обработчике настроек — то есть
    здоровым будет выглядеть фронт, который не может сообщить браузеру ни
    адреса API, ни режима входа.
    """
    test = SERVICES["speech-frontend"]["healthcheck"]["test"]

    assert any("config.json" in str(part) for part in test)
