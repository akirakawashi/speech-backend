"""Матрица прав: у каждой ручки есть охрана, и она та, что записана здесь.

Отдельные тесты вида «reader получает 403 на раскрытие» уже есть, и они
проверяют одну ручку каждый. Здесь проверяется то, чего они по устройству не
видят: **новая** ручка, добавленная завтра без `require(...)`. Такой пропуск не
ломает ни один существующий тест — он просто открывает данные, и обнаруживается
на приёмке или позже.

Поэтому проверка идёт от списка маршрутов приложения, а не от списка тестов:
маршрут, которого нет в таблице ниже, роняет прогон и требует решения — какое
право ему нужно, — вместо того чтобы молча получить нулевое.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.routing import APIRoute

from src.application.authorization import OPEN_MODE_SUB
from src.di.http import create_app
from src.presentation.http.admin_routes import router as admin_router
from src.presentation.http.api import router as service_router
from src.presentation.http.auth import (
    PERM_ADMIN,
    PERM_DOWNLOAD_AUDIO,
    PERM_EXPORT,
    PERM_QUALITY,
    PERM_SEARCH,
    Principal,
    current_principal,
    require,
)
from src.presentation.http.auth_routes import router as auth_router
from src.presentation.http.dashboard_routes import router as dashboard_router
from src.presentation.http.media_routes import router as media_router
from src.presentation.http.quality_routes import router as quality_router
from src.presentation.http.report_routes import router as report_router
from src.presentation.http.routes import router as api_v1_router
from src.setting.config import Settings

#: Роутеры сервиса. Полнота этого списка проверяется сверкой с `openapi()`.
ROUTERS = (
    service_router,
    auth_router,
    api_v1_router,
    media_router,
    admin_router,
    report_router,
    quality_router,
    dashboard_router,
)

#: Право, требуемое каждой ручкой `/api/v1`. `None` — ручка требует
#: аутентификации, но никакого права сверх неё (`/me` отвечает про самого
#: спрашивающего и ничего чужого не показывает).
EXPECTED: dict[tuple[str, str], str | None] = {
    ("GET", "/api/v1/me"): None,
    ("GET", "/api/v1/stats"): PERM_SEARCH,
    ("GET", "/api/v1/search"): PERM_SEARCH,
    ("GET", "/api/v1/calls/{call_id}"): PERM_SEARCH,
    ("GET", "/api/v1/operators"): PERM_SEARCH,
    ("GET", "/api/v1/projects"): PERM_SEARCH,
    # Дерево классификатора для фильтра «Классификация» (SP-310).
    ("GET", "/api/v1/classifier"): PERM_SEARCH,
    # download=1 additionally checks PERM_DOWNLOAD_AUDIO before storage access;
    # the conditional guard is exercised by offline tests in test_speech_media_routes.
    # Прослушивание — по праву поиска; `?download=1` внутри требует ещё
    # `download_audio`. Только на этом пути принимается кука (SP-808) — её
    # границы проверяются в `tests/test_speech_audio_cookie.py`.
    ("GET", "/api/v1/calls/{call_id}/audio"): PERM_SEARCH,
    ("GET", "/api/v1/export"): PERM_EXPORT,
    # Выдача куки для тега `<audio>` (SP-808): требует ровно того же права,
    # что и само прослушивание — иначе кука давала бы больше, чем заголовок.
    ("POST", "/api/v1/session/audio"): PERM_SEARCH,
    # Удаление своей куки — без права и без аутентификации: требовать для
    # выхода действующий токен значит не дать выйти тому, у кого он протух.
    # Админка — одно право на весь раздел. Перечислены поимённо, а не
    # правилом «всё под /admin — admin»: правило пропустит ручку, которая
    # окажется под этим префиксом без охраны вовсе.
    # Классификатор обращений (SP-310): дерево и выключение лишнего.
    ("GET", "/api/v1/admin/classifier"): PERM_ADMIN,
    ("PATCH", "/api/v1/admin/classifier/{node_id}"): PERM_ADMIN,
    ("GET", "/api/v1/admin/projects"): PERM_ADMIN,
    ("PATCH", "/api/v1/admin/projects/{project_id}"): PERM_ADMIN,
    ("GET", "/api/v1/admin/settings"): PERM_ADMIN,
    ("PUT", "/api/v1/admin/settings"): PERM_ADMIN,
    ("GET", "/api/v1/admin/audit"): PERM_ADMIN,
    ("GET", "/api/v1/admin/users"): PERM_ADMIN,
    ("GET", "/api/v1/admin/pipeline"): PERM_ADMIN,
    ("GET", "/api/v1/admin/jobs"): PERM_ADMIN,
    ("POST", "/api/v1/admin/jobs/{job_id}/retry"): PERM_ADMIN,
    ("POST", "/api/v1/admin/calls/{call_id}/reprocess"): PERM_ADMIN,
    # Синхронизация справочников ходит в NCC под нашей служебной учётной
    # записью, а не под учётной записью нажавшего. Право админа здесь про то,
    # кому можно её запускать.
    ("POST", "/api/v1/admin/projects/sync"): PERM_ADMIN,
    ("POST", "/api/v1/admin/backfill/pause"): PERM_ADMIN,
    ("POST", "/api/v1/admin/backfill/start"): PERM_ADMIN,
    # Отчёт по звонкам в Excel (SP-604): файл со всеми звонками периода уходит из
    # системы — только админ, и каждая выгрузка пишется в журнал.
    ("GET", "/api/v1/admin/reports/classification"): PERM_ADMIN,
    ("GET", "/api/v1/admin/reports/calls.xlsx"): PERM_ADMIN,
    # Оценка звонков по скрипту — суждение о конкретном сотруднике: право
    # `quality`, а не поиск.
    ("GET", "/api/v1/calls/{call_id}/quality"): PERM_QUALITY,
    ("GET", "/api/v1/quality/summary"): PERM_QUALITY,
    ("GET", "/api/v1/quality/calls"): PERM_QUALITY,
    # Файл сверки оценок. Охрана маршрута — `quality`: без него оценок не видно
    # вовсе. Внутри дополнительно проверяется `export` — файл уходит из системы;
    # эта проверка — в `tests/test_speech_quality_review.py`.
    ("GET", "/api/v1/quality/review.xlsx"): PERM_QUALITY,
    # Дашборд — те же звонки, что находит поиск, сложенные в счёты. У файла
    # внутри дополнительно проверяется `export`, как у файла сверки оценок.
    ("GET", "/api/v1/dashboard"): PERM_SEARCH,
    ("GET", "/api/v1/dashboard.xlsx"): PERM_SEARCH,
}

#: Служебные ручки без аутентификации. Список закрытый и короткий именно потому,
#: что это исключения: ни одна из них не отдаёт данных о разговорах.
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/version"),
    ("DELETE", "/api/v1/session/audio"),
    # Вход (ADR-SPEECH-0002). Аутентификации на них нет по устройству:
    # войти должен уметь тот, кто ещё не вошёл, а выйти — тот, у кого сессия
    # уже протухла; ему это нужнее всего. Ни одна не отдаёт ни строчки о
    # разговорах: `login` перенаправляет, `callback` заводит сессию и
    # перенаправляет, `logout` гасит её. Границы проверяются в
    # `tests/test_speech_auth_bff.py`.
    ("GET", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/callback"),
    ("POST", "/api/v1/auth/logout"),
}

#: Ручки со своим ключом, а не с ролью пользователя. Отдельно от `PUBLIC`,
#: потому что «без `Principal`» и «без проверки» — разные вещи, и сложив их в
#: одну кучу, мы бы однажды не заметили потерянную проверку ключа. Сама
#: проверка ключа проверяется в `tests/test_speech_metrics.py`.
KEY_GUARDED = {
    ("GET", "/metrics"),
}


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="local",
        speech_auth_mode="open",
    )


def _routes() -> list[tuple[str, str, APIRoute]]:
    """Маршруты берутся у самих роутеров, а не у собранного приложения.

    `app.routes` — внутреннее устройство FastAPI, и оно меняется: до 0.141
    `include_router` разворачивал маршруты в плоский список, с 0.141 кладёт
    непубличную обёртку `_IncludedRouter`, у которой ни `.routes`, ни чего-либо
    публичного. Обход по `app.routes` на такой версии находит ноль ручек — и вся
    матрица зеленеет потому, что проверять нечего. Зависимость закреплена как
    `fastapi>=0.115`, то есть свежая установка получает именно новую версию.

    У `APIRouter` же `.routes` — часть публичного интерфейса и содержит настоящие
    `APIRoute` на обеих версиях. А то, что список роутеров здесь полный, проверяется
    отдельно — сверкой с `openapi()` собранного приложения.
    """
    found: list[tuple[str, str, APIRoute]] = []
    for router in ROUTERS:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods or set()):
                if method in {"HEAD", "OPTIONS"}:
                    continue
                found.append((method, route.path, route))
    return found


def _application_paths() -> set[tuple[str, str]]:
    """Что действительно отвечает в собранном приложении.

    `openapi()` — публичный интерфейс и видит все включённые роутеры, чем бы они
    ни были внутри. Здесь это второй источник истины против `ROUTERS`.
    """
    schema = create_app(_settings()).openapi()
    return {
        (method.upper(), path)
        for path, operations in schema.get("paths", {}).items()
        for method in operations
        if method.upper() not in {"HEAD", "OPTIONS"}
    }


def _guarding_permission(route: APIRoute) -> tuple[bool, str | None]:
    """Что стоит на маршруте: аутентификация и, возможно, право.

    Читается метка, проставленная в `require()`, а не замыкание функции: разбор
    замыкания — догадка о том, как устроена функция внутри, и он молча начнёт
    возвращать не то, если её перепишут.
    """
    authenticated = False
    permission: str | None = None
    for dependency in route.dependant.dependencies:
        call: Any = dependency.call
        if call is current_principal:
            authenticated = True
        marked = getattr(call, "speech_permission", None)
        if marked is not None:
            authenticated = True
            permission = str(marked)
    return authenticated, permission


def test_the_router_list_covers_the_whole_application() -> None:
    """Роутер, включённый в `create_app` и не попавший в `ROUTERS`, сделал бы всю
    матрицу ниже проверкой о части сервиса, молча."""
    # Сравниваются только видимые в схеме маршруты: `/metrics` объявлен
    # `include_in_schema=False` — его в `openapi()` нет и быть не должно.
    # Именно поэтому скрытые ручки обязаны быть в матрице явно: эта сверка их не
    # видит, а значит не поймает целый роутер из таких.
    from_routers = {(method, path) for method, path, route in _routes() if route.include_in_schema}

    assert from_routers == _application_paths()


def test_the_walk_finds_routes_at_all() -> None:
    """Без этого все проверки ниже зеленеют на пустом списке.

    Именно так это и случилось на FastAPI 0.141 до того, как обход стал
    рекурсивным.
    """
    assert len(_routes()) >= len(EXPECTED) + len(PUBLIC) + len(KEY_GUARDED)


def test_every_route_of_the_service_is_listed_here() -> None:
    """Ручка, которой нет в таблице, — это ручка, о правах которой никто не решал."""
    seen = {(method, path) for method, path, _route in _routes()}
    known = set(EXPECTED) | PUBLIC | KEY_GUARDED

    unlisted = sorted(seen - known)

    assert not unlisted, (
        f"маршруты без записи в матрице прав: {unlisted}. "
        "Добавь их в EXPECTED с нужным правом или в PUBLIC, если данных о "
        "разговорах они не отдают"
    )


def test_the_matrix_does_not_describe_routes_that_no_longer_exist() -> None:
    """Иначе таблица прав постепенно превращается в список пожеланий."""
    seen = {(method, path) for method, path, _route in _routes()}

    stale = sorted((set(EXPECTED) | PUBLIC | KEY_GUARDED) - seen)

    assert not stale, f"в матрице описаны несуществующие маршруты: {stale}"


@pytest.mark.parametrize(("method", "path"), sorted(EXPECTED))
def test_each_route_carries_the_permission_the_matrix_declares(method: str, path: str) -> None:
    route = next(r for m, p, r in _routes() if (m, p) == (method, path))

    authenticated, permission = _guarding_permission(route)

    assert authenticated, f"{method} {path} не требует аутентификации вовсе"
    assert permission == EXPECTED[(method, path)], (
        f"{method} {path} охраняется правом {permission!r}, "
        f"а матрица объявляет {EXPECTED[(method, path)]!r}"
    )


@pytest.mark.parametrize(("method", "path"), sorted(PUBLIC | KEY_GUARDED))
def test_public_routes_stay_public_and_carry_no_call_data(method: str, path: str) -> None:
    route = next(r for m, p, r in _routes() if (m, p) == (method, path))

    authenticated, permission = _guarding_permission(route)

    assert not authenticated and permission is None


# --------------------------------------------------------------------------- #
# Права ролей
# --------------------------------------------------------------------------- #


def _principal(*roles: str) -> Principal:
    return Principal(
        sub="u1",
        username="u1",
        email=None,
        display_name=None,
        roles=tuple(roles),
        auth_mode="keycloak",
    )


@pytest.mark.parametrize(
    ("role", "granted"),
    [
        ("speech_reader", {PERM_SEARCH}),
        (
            "speech_editor",
            {PERM_SEARCH, PERM_DOWNLOAD_AUDIO, PERM_EXPORT, PERM_QUALITY},
        ),
        (
            "speech_admin",
            {
                PERM_SEARCH,
                PERM_DOWNLOAD_AUDIO,
                PERM_EXPORT,
                PERM_QUALITY,
                PERM_ADMIN,
            },
        ),
    ],
)
def test_a_role_grants_exactly_these_permissions(role: str, granted: set[str]) -> None:
    """Право, добавленное роли «заодно», — самый тихий способ расширить доступ."""
    assert set(_principal(role).permissions) == granted


def test_no_role_is_implicitly_an_administrator() -> None:
    assert not _principal("speech_reader").can(PERM_ADMIN)
    assert not _principal("speech_editor").can(PERM_ADMIN)


def test_an_unknown_role_grants_nothing() -> None:
    """Опечатка в псевдониме группы обязана быть отказом, а не разрешением."""
    assert _principal("speech_editorr").permissions == frozenset()
    assert _principal().permissions == frozenset()


def test_the_open_mode_principal_is_recognisable_in_the_audit_trail() -> None:
    """Записи открытого режима не должны выглядеть как записи живого человека."""
    assert OPEN_MODE_SUB == "open-admin"


def test_require_refuses_to_guard_a_permission_that_does_not_exist() -> None:
    """Право с опечаткой не даётся никому — значит, ручка закрыта наглухо.

    Это безопасный отказ, но найти его нужно тестом, а не по заявке «кнопка не
    работает у всех»: `require` помечает себя, и матрица выше сверяет метку с
    объявленным набором.
    """
    guard = require("exprot")  # опечатка в `export`

    marked = getattr(guard.dependency, "speech_permission", None)

    assert marked == "exprot"
    assert marked not in _principal("speech_admin").permissions
