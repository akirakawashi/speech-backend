"""Что уходит в браузер.

Отдельные модели, а не датаклассы поискового слоя. Причина не в чистоте:
`SearchItem` — внутренняя структура, и стоит добавить в неё поле, как оно
уезжает во внешний контракт незамеченным. Здесь же каждое поле написано
руками, поэтому расширение контракта — осознанное действие, которое видно в
диффе и попадает в снимок `tests/contract_registry.py`.

`contract_version` в каждом ответе: фронт генерирует типы из `openapi.json`, и
несовпадение версий должно быть видно ему, а не выясняться по форме данных.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.domain.calls.contracts import SPEECH_API_CONTRACT_VERSION, CallSource


class Envelope(BaseModel):
    """Общая часть каждого ответа."""

    contract_version: str = Field(default=SPEECH_API_CONTRACT_VERSION)


class MeResponse(Envelope):
    """Личность текущего пользователя, его роли и доступные действия."""

    sub: str
    username: str | None = None
    display_name: str | None = None
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    auth_mode: str


class ClassifierCount(BaseModel):
    """Узел классификатора и сколько звонков в его ветке (SP-310)."""

    node_id: uuid.UUID
    #: Названия уровней от корня; пустой уровень в середине пути — пустая строка.
    path: list[str] = Field(default_factory=list)
    calls: int


class StatsResponse(Envelope):
    """KPI периода (SRC-08).

    Три числа считаются по **всем обнаруженным** звонкам периода, а не по
    найденным поиском: «обработано» — это про сбор, а не про запрос. Звонки при
    этом — одного источника, `source` запроса (S-35): у вкладок NCC и Телфина
    счётчики свои.

    `topics` — «Основные темы» (SRC-07): десять узлов уровня темы (4) с самыми
    большими ветками по классификации ИИ — в том же источнике.
    """

    processed: int
    transcribed: int
    queued: int
    topics: list[ClassifierCount] = Field(default_factory=list)


class QuoteModel(BaseModel):
    """Найденная реплика с таймкодом и подсветкой совпадений."""

    seq: int
    start_ms: int
    speaker: str
    text: str


class SearchItemModel(BaseModel):
    """Звонок из поисковой выдачи с цитатами и результатом анализа."""

    call_id: uuid.UUID
    session_id: str
    started_at: datetime
    duration_s: int
    match_count: int
    operator_login: str | None = None
    project_title: str | None = None
    #: Классификация ИИ (SP-310): узел дерева и названия уровней от корня. Пустой
    #: уровень в середине пути — пустая строка; пустой путь — классификации нет.
    ai_node_id: uuid.UUID | None = None
    ai_path: list[str] = Field(default_factory=list)
    sentiment: str | None = None
    audio_status: str
    processing_status: str
    #: Сегмент и продукт — **со своим источником каждый**. Интерфейс обязан
    #: показывать, что значение выведено моделью, а не взято из проекта:
    #: пользователь, строящий по этому отчёт, должен видеть разницу.
    segment: str | None = None
    segment_source: str = "unknown"
    product: str | None = None
    product_source: str = "unknown"
    #: Источник звонка (S-35, контракт 2.2): входящие NCC или исходящие Телфина.
    #: Перечислением, а не строкой: в сгенерированных типах фронта это закрытый
    #: набор, и третий источник появится там как новое значение, а не как строка,
    #: которую экран молча покажет без подписи.
    source: CallSource = CallSource.NCC
    #: Балл по скрипту продаж (контракт 2.3): есть у звонка с действующей
    #: оценкой и только у того, кто вправе её видеть (`quality`). Поиск по
    #: разговорам права на оценку не даёт — см. `quality_routes`.
    quality_score: int | None = None
    quotes: list[QuoteModel] = Field(default_factory=list)
    hit_seqs: list[int] = Field(default_factory=list)


class SearchResponse(Envelope):
    """Страница поиска с числом совпадений и размером охвата."""

    mode: str
    total_matches: int
    total_calls: int
    period_calls: int
    #: Доля найденных разговоров от всех звонков периода (SRC-03) — того же
    #: источника `source` (S-35), что и выдача. Считается на сервере: в браузере
    #: то же деление дало бы NaN на пустом периоде.
    share: float
    items: list[SearchItemModel] = Field(default_factory=list)


class SegmentModel(BaseModel):
    """Реплика в карточке звонка.

    `text` — текст расшифровки как есть.

    Когда карточка запрошена с `q`, найденное в этом же поле помечено
    управляющими символами `STX`/`ETX` — теми же, что в цитатах выдачи
    (PLY-04). Отдельным полем это не сделано намеренно: два поля с текстом
    одной реплики означали бы, что однажды покажут не то, а разметку HTML
    сервер не отдаёт вовсе, чтобы её не пришлось вставлять как есть.
    """

    seq: int
    speaker: str
    start_ms: int
    end_ms: int
    text: str


class CallResponse(Envelope):
    """Карточка звонка с расшифровкой и результатом анализа."""

    call_id: uuid.UUID
    session_id: str
    started_at: datetime
    ended_at: datetime
    duration_s: int
    direction: str
    operator_login: str | None = None
    project_title: str | None = None
    #: Классификация ИИ и классификация оператора (SP-310) — узлы одного дерева
    #: и названия их уровней от корня. Пустой уровень в середине пути — пустая
    #: строка; пустой путь — классификации нет.
    ai_node_id: uuid.UUID | None = None
    ai_path: list[str] = Field(default_factory=list)
    operator_node_id: uuid.UUID | None = None
    operator_path: list[str] = Field(default_factory=list)
    #: Совпал ли путь ИИ с путём оператора на каждом из пяти уровней — правило
    #: владельца: пустой уровень оператора или прочерк не совпадает. Пусто —
    #: сравнивать не с чем: нет одного из путей.
    level_match: list[bool] = Field(default_factory=list)
    sentiment: str | None = None
    summary: str | None = None
    #: Тема по предложению ИИ помощника (SP-309, путём с SP-310): мнение модели
    #: сверх разметки. `ai_suggestion_is_new` — путь со своей формулировкой,
    #: которой в дереве нет.
    ai_suggestion: list[str] = Field(default_factory=list)
    ai_suggestion_is_new: bool = False
    #: Причина обращения (S-32): зачем звонил клиент — оценка модели по
    #: расшифровке, не запись оператора. Код — справочника
    #: `K01…K99`, подпись — из справочника. Пусто — причины нет.
    call_reason: str | None = None
    call_reason_code: str | None = None
    call_reason_label: str | None = None
    audio_status: str
    processing_status: str
    analysis_status: str
    segment: str | None = None
    segment_source: str = "unknown"
    product: str | None = None
    product_source: str = "unknown"
    #: Источник звонка (S-35, контракт 2.2). Карточка по ссылке открывает звонок
    #: любого источника, поэтому чей он — говорит только это поле.
    source: CallSource = CallSource.NCC
    role_method: str | None = None
    segments: list[SegmentModel] = Field(default_factory=list)
    hit_seqs: list[int] = Field(default_factory=list)


class OptionModel(BaseModel):
    """Идентификатор и подпись варианта фильтра поиска."""

    id: uuid.UUID
    label: str


class OptionsResponse(Envelope):
    """Доступные варианты фильтра с их подписями."""

    items: list[OptionModel] = Field(default_factory=list)


class ClassifierOption(BaseModel):
    """Узел классификатора для фильтра «Классификация» — плоско, со ссылкой на родителя."""

    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    level: int
    #: Пусто у пустого уровня в середине пути.
    label: str
    sort_order: int
    #: Выключенный узел для нового выбора не предлагается, но звонки у него есть.
    is_active: bool


class ClassifierResponse(Envelope):
    """Дерево классификатора для выбора ветки поиска."""

    items: list[ClassifierOption] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Оценка звонков по скрипту
# --------------------------------------------------------------------------- #


class QualityStageModel(BaseModel):
    """Вердикт по этапу скрипта: 1 или 0, как у контролёра.

    Неприменимый этап засчитан, `passed` у него `true`: правило заказчика из
    макета. `evidence` — номера реплик карточки, на которых основан вывод.
    """

    key: str
    label: str
    #: Обязательное условие: его пропуск — нарушение, а не слабая продажа.
    mandatory: bool
    applicable: bool
    passed: bool
    evidence: list[int] = Field(default_factory=list)
    comment: str = ""


class QualityObjectionModel(BaseModel):
    """Возражение или вопрос клиента и отработал ли его оператор."""

    type: str
    label: str
    #: Реплика клиента с возражением; пусто, если модель её не назвала.
    seq: int | None = None
    handled: bool
    evidence: list[int] = Field(default_factory=list)
    comment: str = ""


class CallQualityResponse(Envelope):
    """Оценка звонка по скрипту.

    `script_applies` — оценивается ли звонок по этому скрипту вообще. Если нет,
    баллов и этапов у звонка нет: это входящее обращение или звонок другой
    кампании, и 0 за них поставить было бы неправдой.
    """

    call_id: uuid.UUID
    script_version: str
    model_name: str
    evaluated_at: datetime
    script_applies: bool
    score: int | None = None
    passed_count: int | None = None
    stage_count: int | None = None
    all_passed: bool | None = None
    outcome: str | None = None
    outcome_label: str | None = None
    summary: str | None = None
    stages: list[QualityStageModel] = Field(default_factory=list)
    objections: list[QualityObjectionModel] = Field(default_factory=list)


class QualityBuckets(BaseModel):
    """Звонки по скрипту, разложенные по баллу.

    Пороги — из макета: от 85 — хорошие, от 70 — средние, ниже — плохие. Сумма
    трёх равна числу звонков по скрипту.
    """

    good: int
    fair: int
    poor: int


class QualityStageShare(BaseModel):
    """Этап за период. `share` — доля звонков, где этап засчитан, от 0 до 1.

    Засчитан — выполнен или не понадобился (`passed`). Сколько раз этап был нужен
    и не выполнен — `failed`; выполнен он в `applicable - failed` звонках.
    """

    key: str
    label: str
    #: Обязательное условие: его пропуск — нарушение, а не слабая продажа.
    mandatory: bool
    calls: int
    #: В скольких звонках условие этапа наступало.
    applicable: int
    passed: int
    failed: int
    share: float


class QualityObjectionShare(BaseModel):
    """Тип возражения за период. `share` — доля отработанных, от 0 до 1."""

    type: str
    label: str
    raised: int
    handled: int
    share: float


class QualityOutcomeShare(BaseModel):
    """Число и доля звонков одного исхода продаж."""

    key: str
    label: str
    calls: int
    share: float


class QualityOperatorStage(BaseModel):
    """Этап у одного сотрудника: сколько раз был нужен и сколько раз не выполнен."""

    key: str
    applicable: int
    failed: int


class QualityOperatorScore(BaseModel):
    """Сотрудник за период.

    `operator_id` пуст у звонков без оператора — так приходят записи из
    локальной выгрузки, где оператора источник не знает.
    """

    operator_id: uuid.UUID | None = None
    login: str | None = None
    full_name: str | None = None
    calls: int
    average_score: float
    fully_correct: int
    fully_correct_share: float
    buckets: QualityBuckets
    #: Звонки, где не выполнено обязательное условие.
    violations: int
    #: Этапы сотрудника в порядке скрипта — строка таблицы «сотрудник × этап».
    stages: list[QualityOperatorStage] = Field(default_factory=list)


class QualityWeek(BaseModel):
    """Неделя периода; начало недели — понедельник по часам колл-центра."""

    week_start: date
    calls: int
    fully_correct: int
    fully_correct_share: float
    average_score: float | None = None
    buckets: QualityBuckets


class QualitySummaryResponse(Envelope):
    """Сводка оценок за период — вкладка «Оценка качества».

    Всё считается по звонкам, к которым скрипт применим (`calls`); `evaluated` —
    все оценённые звонки периода вместе с неприменимыми.
    """

    script_version: str
    #: Все записи периода, включая ошибки оценки и ожидающие обработки.
    total: int = 0
    failed: int = 0
    pending: int = 0
    #: Расшифровки уже не будет: запись короткая, отсутствует или не обработана.
    unavailable: int = 0
    evaluated: int
    calls: int
    fully_correct: int
    fully_correct_share: float
    average_score: float | None = None
    buckets: QualityBuckets
    #: Звонки, где не выполнено обязательное условие.
    violations: int
    stages: list[QualityStageShare] = Field(default_factory=list)
    objections: list[QualityObjectionShare] = Field(default_factory=list)
    outcomes: list[QualityOutcomeShare] = Field(default_factory=list)
    operators: list[QualityOperatorScore] = Field(default_factory=list)
    weeks: list[QualityWeek] = Field(default_factory=list)


class QualityCallStage(BaseModel):
    """Этап в строке списка: был ли нужен и засчитан ли. Подписи — в сводке."""

    key: str
    applicable: bool
    passed: bool


class QualityCallItem(BaseModel):
    """Запись звонка с оценкой или состоянием её получения.

    `failed_stages` — подписи этапов, которые в звонке наступали и не
    засчитаны: по ним контролёр решает, какой звонок открыть первым.
    `stages` — те же этапы в порядке скрипта, для полоски в строке.

    У звонка вне скрипта (`script_applies` ложно) баллов и этапов нет, а в
    `summary` — причина, по которой модель его не оценила.
    """

    call_id: uuid.UUID
    session_id: str
    started_at: datetime
    duration_s: int
    operator_id: uuid.UUID | None = None
    operator_login: str | None = None
    operator_name: str | None = None
    evaluation_status: Literal["evaluated", "failed", "pending", "unavailable"] = "evaluated"
    failure_reason: str | None = None
    script_applies: bool | None
    score: int | None = None
    #: `good`, `fair` или `poor` — см. `QualityBuckets`.
    bucket: str | None = None
    passed_count: int | None = None
    stage_count: int | None = None
    all_passed: bool | None
    #: Не выполнено обязательное условие.
    violation: bool
    outcome: str | None = None
    outcome_label: str | None = None
    summary: str | None = None
    failed_stages: list[str] = Field(default_factory=list)
    stages: list[QualityCallStage] = Field(default_factory=list)


class QualityCallsResponse(Envelope):
    """Все записи периода; `total` — сколько их всего с учётом фильтров."""

    total: int
    items: list[QualityCallItem] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Дашборд (2.4)
# --------------------------------------------------------------------------- #


class DashboardTheme(BaseModel):
    """Тема на графике: уровень темы классификатора, сложенный по всем веткам."""

    #: Ключ темы — им же тему выбирают (`theme` запроса). У «Прочего» — `__other__`.
    key: str
    label: str
    calls: int
    #: Доля среди звонков периода, у которых есть тема.
    share: float
    #: Доля негатива среди звонков темы с тональностью. `None` — таких нет.
    negative_share: float | None = None


class DashboardBucket(BaseModel):
    """Столбец графика: день, неделя или месяц в пределах периода."""

    start: date
    #: Последний день корзины, включительно.
    end: date
    calls: int
    #: Звонки по темам — в порядке `themes` ответа.
    by_theme: list[int] = Field(default_factory=list)
    negative_share: float | None = None


class DashboardSubtopic(BaseModel):
    """Подтема выбранной темы с числом обращений и долей негатива."""

    key: str
    label: str
    calls: int
    #: Доля в выбранной теме.
    share: float


class DashboardPeak(BaseModel):
    """Неделя с наибольшим числом обращений."""

    start: date
    end: date
    calls: int


class DashboardSelection(BaseModel):
    """Числа выбранной темы — или всех тем, если тема не выбрана."""

    theme: str | None = None
    label: str | None = None
    calls: int
    negative_share: float | None = None
    #: Вторая половина периода к первой, долей: 0.12 — на 12% больше. `None` —
    #: сравнивать не с чем.
    change: float | None = None
    peak: DashboardPeak | None = None
    subtopics: list[DashboardSubtopic] = Field(default_factory=list)


class DashboardResponse(Envelope):
    """Обращения по темам за период: столбцы по времени и числа выбранной темы."""

    #: Сутки периода по часам колл-центра, обе включительно.
    first_day: date
    last_day: date
    #: Корзина, по которой построены столбцы: названная в запросе или автоматическая.
    grain: Literal["day", "week", "month"]
    #: Звонков периода, у которых есть тема.
    calls: int
    themes: list[DashboardTheme] = Field(default_factory=list)
    buckets: list[DashboardBucket] = Field(default_factory=list)
    selection: DashboardSelection
