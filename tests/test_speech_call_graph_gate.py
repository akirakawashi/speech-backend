"""Граф вызовов обязан знать про конвейер речи.

Сам гейт запускается только против поднятого стека — без контейнеров он
отказывается работать, и правильно делает. Но **объявление** в нём — обычный
текст, и рассыхается оно молча: заголовок гейта прямо предупреждает, что связь,
добавленная без правки сервиса, портит объявление. Здесь проверяется то, что
можно проверить без стека: что в объявлении есть то, чего в нём когда-то не
было, и что пропуск службы виден.
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
    / "service-call-graph.py"
)

pytestmark = pytest.mark.skipif(
    not GATE.exists(), reason=f"репозиторий инфраструктуры не рядом: {GATE}"
)


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("service_call_graph", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_workers_are_callers_in_the_declaration() -> None:
    """Воркеры ходят наружу за всем, что делает конвейер.

    Пока их не было в объявлении, граф молчал о самой связной части системы:
    HTTP-поверхность речи не вызывает никого, и по графу выходило, что конвейер
    не разговаривает ни с чем.
    """
    gate = _gate()

    speech_workers = {"speech-worker-daily", "speech-worker-backfill"}
    # Подмножество, а не равенство. Утверждение этого теста — «воркеры речи
    # объявлены как вызывающие», а не «кроме них не бывает никого»: список
    # `COMPOSE_SERVICES` общий для всех служб без `container_name`, и с
    # 2026-09-20 там же воркер файлов беседы. Равенство ломало бы этот тест на
    # каждой такой службе, ничего не говоря о речи.
    assert speech_workers <= set(gate.COMPOSE_SERVICES)
    for worker in speech_workers:
        assert gate.PERMITTED[worker], "вызовы воркера обязаны быть объявлены"


def test_the_recording_store_is_reachable_from_somewhere_declared() -> None:
    """«Откуда достижимо хранилище разговоров» — первый вопрос ИБ.

    До этой строки граф на него не отвечал вовсе.
    """
    gate = _gate()
    store = ("speech-minio", 9000)

    reaching = {name for name, targets in gate.SHARED.items() if store in targets}

    assert "speech-ms" in reaching, "служба отдаёт аудио браузеру по подписанной ссылке"
    assert set(gate.COMPOSE_SERVICES) <= reaching, "воркеры пишут записи и читают их"


def test_a_service_without_containers_is_named_not_silently_dropped() -> None:
    """Пропуск обязан быть виден: «расхождений нет» на неполном обходе — ложь.

    Проверяется вывод, а не намерение: строка о пропуске существует затем, чтобы
    её прочитал человек.
    """
    gate = _gate()
    report = gate.Report()
    report.skipped = ["speech-worker-daily"]
    report.findings = [gate.Finding("backend", "rag-ms", "allow", "open", "", credential=False)]

    printed: list[str] = []
    original = print

    def capture(*args: object, **kwargs: object) -> None:
        printed.append(" ".join(str(a) for a in args))

    gate.print = capture  # type: ignore[attr-defined]
    try:
        gate.render(report)
    finally:
        gate.print = original  # type: ignore[attr-defined]

    body = "\n".join(printed)
    assert "НЕ ПРОВЕРЕНО" in body
    assert "speech-worker-daily" in body


def test_a_missing_compose_service_resolves_to_nothing_rather_than_guessing() -> None:
    """Имя контейнера складывается из имени проекта и номера реплики.

    Зашитое имя разошлось бы с реальностью при первом переименовании проекта,
    поэтому службы разрешаются по метке. Несуществующая служба обязана дать
    пустой список, а не догадку.
    """
    gate = _gate()

    assert gate.replicas("no-such-compose-service-0000") == []
