"""Что показывает `/metrics`, и почему именно это.

Метрика заводится не потому, что число легко посчитать, а потому, что по нему
принимают решение. Здесь их пять групп, и у каждой есть вопрос, на который она
отвечает:

* **глубина очереди по полосам и статусам** — сколько работы стоит и в каком
  она состоянии; `dead` отдельно, потому что он не рассосётся сам;
* **возраст самого старого ожидающего задания в `daily`** — успевает ли ночная
  обработка к утреннему сроку. Это единственная метрика, по которой можно
  заметить срыв **до** того, как в девять утра выяснится, что искать нечего;
* **длительности этапов** — где именно уходит время, когда не успевает;
* **отказы по классам за сутки** — что именно ломается: провайдер, сеть, данные;
* **состояние архива**: сколько звонков в каком состоянии обработки и сколько
  занято хранилищем.

Формат — текстовый формат Prometheus, а не JSON: сервис в этом контуре
опрашивают тем, что уже есть, и собственный формат означал бы собственный
экспортёр рядом.

Числа считаются **одним запросом на группу**, а не по строке на метрику:
`/metrics` опрашивают раз в пятнадцать секунд на кластере, чей измеренный
запас соединений равен единице.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.queue.sql import WAITING_SINCE_SQL

#: Окно, за которое считаются длительности и отказы. Сутки, потому что цикл
#: обработки суточный: за час не наберётся ни одного завершённого этапа, а за
#: неделю среднее перестанет реагировать на вчерашнюю поломку.
WINDOW_HOURS = 24

_QUEUE_SQL = text(
    """
    SELECT lane, status, count(*) AS jobs
      FROM speech_jobs
     GROUP BY lane, status
    """
)

#: Возраст — с момента, когда задание ждёт воркера (`WAITING_SINCE_SQL`), и
#: только у тех, чья отсрочка кончилась. Приём откладывает часть звонков до
#: ночи: считая от создания, очередь каждый день выглядела бы зависшей на
#: полсуток, хотя эти задания ждут по расписанию.
_OLDEST_SQL = text(
    f"""
    SELECT lane,
           extract(epoch FROM now() - min({WAITING_SINCE_SQL})) AS age_seconds
      FROM speech_jobs
     WHERE status IN ('queued', 'running')
       AND {WAITING_SINCE_SQL} <= now()
     GROUP BY lane
    """
)

_DURATION_SQL = text(
    """
    SELECT kind,
           count(*) AS finished,
           avg(extract(epoch FROM finished_at - started_at)) AS avg_seconds,
           percentile_disc(0.95) WITHIN GROUP (
               ORDER BY extract(epoch FROM finished_at - started_at)
           ) AS p95_seconds
      FROM speech_jobs
     WHERE status = 'done'
       AND started_at IS NOT NULL
       AND finished_at IS NOT NULL
       AND finished_at > now() - make_interval(hours => :hours)
     GROUP BY kind
    """
)

_ERRORS_SQL = text(
    """
    SELECT coalesce(last_error_class, 'unknown') AS error_class,
           kind,
           count(*) AS jobs
      FROM speech_jobs
     WHERE last_error_at IS NOT NULL
       AND last_error_at > now() - make_interval(hours => :hours)
     GROUP BY 1, 2
    """
)

_CALLS_SQL = text(
    """
    SELECT processing_status, audio_status, count(*) AS calls
      FROM speech_calls
     GROUP BY processing_status, audio_status
    """
)

#: Байты — отдельным запросом, а не второй колонкой в предыдущем.
#: Сгруппированные по двум колонкам, они дали бы несколько строк с одним
#: и тем же набором меток `{audio_status=…}` и разными значениями — а это для
#: сборщика ошибка разбора, от которой отбрасывается **весь** ответ.
_AUDIO_BYTES_SQL = text(
    """
    SELECT audio_status, coalesce(sum(audio_bytes), 0) AS bytes
      FROM speech_calls
     GROUP BY audio_status
    """
)


class Sample(BaseModel):
    """Имя, метки и числовое значение одной метрики."""

    model_config = ConfigDict(frozen=False, arbitrary_types_allowed=True)

    name: str
    value: float
    labels: dict[str, str] = Field(default_factory=dict)

    def render(self) -> str:
        """Преобразовать метрики в текст формата Prometheus."""
        if not self.labels:
            return f"{self.name} {_number(self.value)}"
        inner = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(self.labels.items()))
        return f"{self.name}{{{inner}}} {_number(self.value)}"


def _escape(value: str) -> str:
    """Экранировать значение метки для текстового формата Prometheus."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace(
            "\n",
            "\\n",
        )
    )


def _number(value: float) -> str:
    """Целые печатаются целыми.

    `12.0` в счётчике заданий читается как «что-то усреднили», хотя это
    двенадцать штук.
    """
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}"


#: Пояснение и тип для каждой метрики. Не украшение: `# TYPE` — это то, по чему
#: сборщик решает, можно ли считать по метрике скорость. Ошибка здесь делает
#: график молча неверным, а не ломает сбор.
_META: dict[str, tuple[str, str]] = {
    "speech_queue_jobs": ("gauge", "Заданий в очереди по полосе и статусу"),
    "speech_queue_oldest_seconds": (
        "gauge",
        "Возраст самого старого незавершённого задания полосы, секунды",
    ),
    "speech_stage_finished_jobs": ("gauge", f"Завершено этапов за {WINDOW_HOURS} ч"),
    "speech_stage_seconds_avg": ("gauge", "Средняя длительность этапа, секунды"),
    "speech_stage_seconds_p95": ("gauge", "95-й процентиль длительности этапа, секунды"),
    "speech_errors_recent_jobs": (
        "gauge",
        f"Заданий с отказом за {WINDOW_HOURS} ч, по классу отказа и этапу",
    ),
    "speech_calls": ("gauge", "Звонков по состоянию обработки и состоянию записи"),
    "speech_audio_bytes": ("gauge", "Байтов аудио в хранилище по состоянию записи"),
    "speech_calls_total": ("gauge", "Звонков всего"),
    "speech_jobs_total": ("gauge", "Заданий в очереди всего"),
}


async def collect(session: AsyncSession, *, window_hours: int = WINDOW_HOURS) -> list[Sample]:
    """Собрать метрики. Пять запросов, ни одного цикла по строкам."""
    samples: list[Sample] = []

    for row in (await session.execute(_QUEUE_SQL)).mappings():
        samples.append(
            Sample(
                name="speech_queue_jobs",
                value=float(row["jobs"]),
                labels={"lane": str(row["lane"]), "status": str(row["status"])},
            )
        )

    for row in (await session.execute(_OLDEST_SQL)).mappings():
        samples.append(
            Sample(
                name="speech_queue_oldest_seconds",
                value=float(row["age_seconds"] or 0.0),
                labels={"lane": str(row["lane"])},
            )
        )

    for row in (await session.execute(_DURATION_SQL, {"hours": window_hours})).mappings():
        labels = {"kind": str(row["kind"])}
        samples.append(
            Sample(name="speech_stage_finished_jobs", value=float(row["finished"]), labels=labels)
        )
        samples.append(
            Sample(
                name="speech_stage_seconds_avg",
                value=float(row["avg_seconds"] or 0.0),
                labels=labels,
            )
        )
        samples.append(
            Sample(
                name="speech_stage_seconds_p95",
                value=float(row["p95_seconds"] or 0.0),
                labels=labels,
            )
        )

    for row in (await session.execute(_ERRORS_SQL, {"hours": window_hours})).mappings():
        samples.append(
            Sample(
                name="speech_errors_recent_jobs",
                value=float(row["jobs"]),
                labels={"error_class": str(row["error_class"]), "kind": str(row["kind"])},
            )
        )

    for row in (await session.execute(_CALLS_SQL)).mappings():
        samples.append(
            Sample(
                name="speech_calls",
                value=float(row["calls"]),
                labels={
                    "processing_status": str(row["processing_status"]),
                    "audio_status": str(row["audio_status"]),
                },
            )
        )

    for row in (await session.execute(_AUDIO_BYTES_SQL)).mappings():
        samples.append(
            Sample(
                name="speech_audio_bytes",
                value=float(row["bytes"]),
                labels={"audio_status": str(row["audio_status"])},
            )
        )

    # Две всегда присутствующие строки без меток. Пока их не было, пустая база
    # давала пустой ответ, в котором невозможно отличить «звонков нет» от
    # «сбор сломан»: и то и другое выглядит как отсутствие данных. Считаются из уже
    # собранного, без шестого запроса.
    samples.append(
        Sample(
            name="speech_calls_total",
            value=sum(s.value for s in samples if s.name == "speech_calls"),
        )
    )
    samples.append(
        Sample(
            name="speech_jobs_total",
            value=sum(s.value for s in samples if s.name == "speech_queue_jobs"),
        )
    )

    return samples


def render(samples: list[Sample]) -> str:
    """Текстовый формат Prometheus.

    `# HELP`/`# TYPE` печатаются один раз на имя, перед первой его выборкой:
    повторный `# TYPE` для одного имени сборщик считает ошибкой разбора и
    отбрасывает **весь** ответ, а не одну строку.
    """
    lines: list[str] = []
    announced: set[str] = set()
    # Выборки одного имени идут подряд: иначе `# HELP` оказывается посреди
    # чужих строк. Сортировка устойчивая — порядок внутри имени тот, в котором
    # его вернула база.
    for sample in sorted(samples, key=lambda item: item.name):
        if sample.name not in announced:
            announced.add(sample.name)
            kind, help_text = _META.get(sample.name, ("gauge", sample.name))
            lines.append(f"# HELP {sample.name} {help_text}")
            lines.append(f"# TYPE {sample.name} {kind}")
        lines.append(sample.render())
    return "\n".join(lines) + "\n"


def as_mapping(samples: list[Sample]) -> dict[str, Any]:
    """Те же числа словарём — для тестов и для админского снимка."""
    result: dict[str, Any] = {}
    for sample in samples:
        key = sample.render().split(" ", 1)[0]
        result[key] = sample.value
    return result
