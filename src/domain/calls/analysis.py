"""Правила классификации, резюме, ролей и причин обращения в анализе звонка."""

from __future__ import annotations

import logging
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.domain.calls.classifier import (
    EMPTY_LEVEL_LABEL,
    ClassifierIndex,
    classifier_levels,
    clean_label,
    dimensions,
)
from src.domain.calls.contracts import Sentiment, Speaker
from src.domain.calls.reasons import interpret_reason

logger = logging.getLogger(__name__)

SUMMARY_LIMIT: Final[int] = 300
SUGGESTION_LABEL_LIMIT: Final[int] = 120


class RoleCandidate(BaseModel):
    """Предложенная роль: прочитанный номер реплики и ещё не проверенное имя роли."""

    model_config = ConfigDict(frozen=True)
    seq: int | None
    speaker: str


class AnalysisAnswer(BaseModel):
    """Прочитанное содержание ответа до применения правил и сверки со справочниками."""

    model_config = ConfigDict(frozen=True)
    path: tuple[str | None, ...]
    sentiment: Sentiment
    summary: str | None = None
    roles: tuple[RoleCandidate, ...] = ()
    suggestion: tuple[str | None, ...] = ()
    call_reason: str | None = None
    call_reason_code: str | None = None


class AnalyzeWork(BaseModel):
    """Результат разбора, готовый к атомарному сохранению."""

    model_config = ConfigDict(frozen=True)

    #: Узел классификатора, до которого путь из ответа модели нашёлся в дереве.
    #: `None` — мимо дерева уже уровень 1.
    node_id: UUID | None
    #: Названия найденных уровней — из дерева, а не из ответа.
    path: tuple[str, ...]
    sentiment: Sentiment | None
    summary: str | None
    #: Уровни 1 и 2 пути в перечислениях сегмента и продукта. `None` — уровня
    #: нет или ему нет места в перечислении; подставлять умолчание нельзя.
    segment: str | None = None
    product: str | None = None
    #: Роли, которые расставила модель. Пусто, если их не просили или этап
    #: определил роли сам.
    roles: tuple[tuple[int, Speaker], ...] = ()
    #: Тема по предложению ИИ помощника: названия уровней и узел, если весь путь
    #: есть в дереве.
    suggestion: tuple[str, ...] = ()
    suggestion_node_id: UUID | None = None
    #: Причина обращения: фраза со стороны клиента и код справочника (S-32).
    call_reason: str | None = None
    call_reason_code: str | None = None


def interpret_analysis(
    answer: AnalysisAnswer,
    *,
    classifier: ClassifierIndex,
    needs_roles: bool,
    reason_codes: frozenset[str],
) -> AnalyzeWork:
    """Сверить содержание со справочниками и вернуть согласованный результат.

    Принимает прочитанный ответ, дерево, признак запроса ролей и отправленные
    модели коды причин. Сохраняет только найденную часть пути; известные роли
    не заменяет. Не меняет дерево и не обращается к внешним сервисам.
    """
    asked = _levels(answer.path)
    resolved = classifier.resolve(asked)
    if len(resolved.labels) < len(asked):
        logger.getChild("interpret").info(
            "the path leaves the classifier after level %d of %d", len(resolved.labels), len(asked)
        )
    summary = answer.summary.strip()[:SUMMARY_LIMIT] if answer.summary is not None else None
    roles = _roles(answer.roles) if needs_roles else ()
    segment, product = dimensions(resolved.labels)
    suggestion, suggestion_node_id = _suggestion(answer.suggestion, classifier)
    reason = interpret_reason(
        {"call_reason": answer.call_reason, "call_reason_code": answer.call_reason_code},
        reason_codes,
    )
    return AnalyzeWork(
        node_id=resolved.node_id,
        path=resolved.labels,
        sentiment=answer.sentiment,
        summary=summary or None,
        segment=segment,
        product=product,
        roles=roles,
        suggestion=suggestion,
        suggestion_node_id=suggestion_node_id,
        call_reason=reason.text,
        call_reason_code=reason.code,
    )


def _roles(candidates: tuple[RoleCandidate, ...]) -> tuple[tuple[int, Speaker], ...]:
    """Вернуть допустимые роли; ошибочный номер или неизвестную роль пропустить."""
    speakers = frozenset(speaker.value for speaker in Speaker)
    return tuple(
        (candidate.seq, Speaker(candidate.speaker))
        for candidate in candidates
        if candidate.seq is not None and candidate.speaker in speakers
    )


def _levels(values: tuple[str | None, ...]) -> tuple[str, ...]:
    """Нормализовать подписи по местам; прочерк и «(пусто)» обозначают пустой уровень."""
    return classifier_levels(
        "" if value is None or clean_label(value) == EMPTY_LEVEL_LABEL else value
        for value in values
    )


def _suggestion(
    values: tuple[str | None, ...], classifier: ClassifierIndex
) -> tuple[tuple[str, ...], UUID | None]:
    """Привязать найденное предложение к узлу; новую формулировку сохранить словами."""
    levels = tuple(level[:SUGGESTION_LABEL_LIMIT].strip() for level in _levels(values))
    if not any(levels):
        return (), None
    resolved = classifier.resolve(levels)
    if resolved.node_id is not None and len(resolved.labels) == len(levels):
        return resolved.labels, resolved.node_id
    return levels, None
