"""Гейт владения данными видит таблицы, которые пишутся сырым SQL.

Проверяется инструмент из соседнего репозитория, а не наш код, и это осознанно:
он отвечает на вопрос «кто пишет в таблицу», а Speech Analytics написан на
`text()` — очередь держится на `FOR UPDATE SKIP LOCKED` и `ON CONFLICT`,
которых ORM не выражает. Пока гейт умел только ORM, одиннадцать наших таблиц не
появлялись в его отчёте вовсе.

Отсутствие строки читается как «такой таблицы нет», прочерк в колонке «кто
пишет» — как «никто не пишет», а зелёная строка — как «нарушений нет». На деле
все три означали «инструмент не смотрел». Хуже пустого отчёта только отчёт,
выглядящий полным.

Своего набора тестов у скриптов проверки нет, поэтому проверка стоит здесь:
ломается она от правки в том репозитории, а замечает её тот, кто трогает этот.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

GATE = (
    Path(__file__).resolve().parents[2]
    / "aicistent-infrastructure"
    / "scripts"
    / "verify"
    / "ownership-matrix.py"
)

pytestmark = pytest.mark.skipif(
    not GATE.exists(), reason=f"репозиторий инфраструктуры не рядом: {GATE}"
)


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ownership_matrix", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Модуль регистрируется **до** исполнения: `@dataclass` внутри него ищет
    # себя в `sys.modules`, чтобы разрешить аннотации, и на незарегистрированном
    # падает с `NoneType has no attribute __dict__` — сообщением, по которому
    # причину не угадать.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    return _gate()


def test_every_speech_table_has_a_declared_owner(gate: ModuleType) -> None:
    """Таблица без владельца читается как ничья, и это худшая из неправд."""
    declared = {name for name in gate.TARGET_OWNER if name.startswith("speech_")}

    assert len(declared) >= 11
    assert all(gate.TARGET_OWNER[name] == "speech-ms" for name in declared)


def test_our_packages_belong_to_our_service(gate: ModuleType) -> None:
    """Иначе писателем наших таблиц объявится `backend`.

    Префиксы разбираются по порядку, и общий `app/` в конце ловит всё, для чего
    не нашлось своего, — наш код попадал бы туда молча.
    """
    assert gate.service_of("src/presentation/cli/worker.py") == "speech-ms"
    assert gate.service_of("src/presentation/http/routes.py") == "speech-ms"
    assert gate.service_of("src/infrastructure/sources/ncc/sync.py") == "speech-ms"


def test_raw_sql_writes_are_seen(gate: ModuleType) -> None:
    """`INSERT`, `UPDATE`, `DELETE` в строке — такие же записи, как `session.add`."""
    import ast  # noqa: PLC0415

    source = '''
QUERY = """
    INSERT INTO speech_calls (id) VALUES (:id)
    ON CONFLICT (session_id) DO NOTHING
"""
OTHER = "UPDATE speech_jobs SET status = 'done' WHERE id = :id"
GONE = "DELETE FROM speech_audit_events WHERE id = :id"
'''
    found, _ = gate.raw_sql_accesses(
        "app/speech/x.py",
        "speech-ms",
        ast.parse(source),
        frozenset({"speech_calls", "speech_jobs", "speech_audit_events"}),
    )

    assert {(access.table, access.kind) for access in found} == {
        ("speech_calls", "insert"),
        ("speech_jobs", "update"),
        ("speech_audit_events", "delete"),
    }


def test_raw_sql_reads_are_seen(gate: ModuleType) -> None:
    import ast  # noqa: PLC0415

    source = (
        'Q = "SELECT s.text FROM speech_transcript_segments s '
        'JOIN speech_calls c ON c.id = s.call_id"'
    )
    found, _ = gate.raw_sql_accesses(
        "app/speech/x.py",
        "speech-ms",
        ast.parse(source),
        frozenset({"speech_transcript_segments", "speech_calls"}),
    )

    assert {(access.table, access.kind) for access in found} == {
        ("speech_transcript_segments", "select"),
        ("speech_calls", "select"),
    }


def test_a_cte_name_does_not_become_a_table(gate: ModuleType) -> None:
    """`WITH ins AS (…)` завёл бы таблицу `ins`, а алиасы — десяток фантомов.

    Отличить их от настоящих таблиц стало бы нечем, и матрица наполнилась бы
    мусором — то есть перестала бы читаться.
    """
    import ast  # noqa: PLC0415

    source = (
        'Q = "WITH ins AS (INSERT INTO speech_topics (code) VALUES (:c) RETURNING id) '
        'SELECT id FROM ins"'
    )
    found, unknown = gate.raw_sql_accesses(
        "app/speech/x.py", "speech-ms", ast.parse(source), frozenset({"speech_topics"})
    )

    assert {access.table for access in found} == {"speech_topics"}
    assert "ins" in unknown, "неопознанное имя должно попасть в отчёт, а не пропасть"


def test_locking_clauses_are_not_mistaken_for_writes(gate: ModuleType) -> None:
    """`FOR UPDATE SKIP LOCKED` — блокировка строки, а не запись в таблицу `skip`.

    Наша очередь построена ровно на этой конструкции, и без отрицательного
    просмотра назад гейт объявлял бы «таблицу» `skip` на каждом её вызове.
    """
    import ast  # noqa: PLC0415

    # Кавычка внутри SQL — двойная снаружи, одинарная внутри: так строка
    # остаётся читаемой и не требует экранирования.
    source = (
        "Q = \"SELECT id FROM speech_jobs WHERE status = 'queued' "
        'ORDER BY priority FOR UPDATE SKIP LOCKED"'
    )
    found, unknown = gate.raw_sql_accesses(
        "app/speech/x.py", "speech-ms", ast.parse(source), frozenset({"speech_jobs"})
    )

    assert {(a.table, a.kind) for a in found} == {("speech_jobs", "select")}
    assert unknown == set(), f"ключевые слова попали в неопознанные: {unknown}"


def test_upsert_is_a_write_to_the_table_not_to_the_word_set(gate: ModuleType) -> None:
    """`ON CONFLICT DO UPDATE SET` — часть вставки, а не запись в таблицу `set`."""
    import ast  # noqa: PLC0415

    source = (
        'Q = "INSERT INTO speech_topics (code) VALUES (:c) '
        'ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label"'
    )
    found, unknown = gate.raw_sql_accesses(
        "app/speech/x.py", "speech-ms", ast.parse(source), frozenset({"speech_topics"})
    )

    assert {(a.table, a.kind) for a in found} == {("speech_topics", "insert")}
    assert unknown == set()


def test_a_plain_string_is_not_mistaken_for_a_query(gate: ModuleType) -> None:
    """Без отсева регулярки пошли бы по каждой строке в файле."""
    import ast  # noqa: PLC0415

    source = 'MESSAGE = "не удалось обновить справочник: попробуйте позже"'
    found, unknown = gate.raw_sql_accesses(
        "app/speech/x.py", "speech-ms", ast.parse(source), frozenset({"speech_topics"})
    )

    assert found == []
    assert unknown == set()


def test_the_matrix_lists_tables_that_have_no_orm_class(gate: ModuleType) -> None:
    """Строка нужна даже там, где модели нет.

    Её отсутствие читается как «такой таблицы не существует» — а она
    существует, у неё есть владелец, и именно это гейт и должен показывать.
    """
    matrix = gate.build_matrix({}, [])

    assert "speech_calls" in matrix["tables"]
    assert matrix["tables"]["speech_calls"]["target_owner"] == "speech-ms"
    assert matrix["tables"]["speech_calls"]["model"] is None


def test_an_access_to_an_unknown_table_does_not_break_the_run(gate: ModuleType) -> None:
    """Имя из запроса может не значить ничего: у матрицы о нём нет сведений.

    Ронять на этом весь отчёт значило бы терять и то, что инструмент выяснил.
    """
    access = gate.Access(
        module="app/x.py", service="backend", model="", table="ins", kind="insert", line=1
    )

    matrix = gate.build_matrix({}, [access])

    assert "ins" not in matrix["tables"]
