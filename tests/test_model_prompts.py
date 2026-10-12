"""Содержание промптов: договор с разбором, ограничения и независимость темизации."""

import json
import re
import unicodedata
from typing import get_args

import pytest

from src.application.dto.model_execution import ModelScenario
from src.infrastructure.data.catalogues import CLASSIFIER_FILE
from src.infrastructure.persistence.classifier import EMPTY_LEVEL_LABEL, label_key
from src.infrastructure.prompts.catalogue import FilePromptCatalogue


def _part(scenario: ModelScenario, role: str) -> str:
    """Прочитать сообщение указанной роли из ресурса бэкенда."""
    return next(m.content for m in FilePromptCatalogue().get(scenario).messages if m.role == role)


@pytest.mark.parametrize("scenario", get_args(ModelScenario))
def test_all_template_references_are_declared_and_inputs_reach_messages(scenario):
    """Опечатка в ссылке либо забытая переменная обнаруживается до запуска сервиса."""
    template = FilePromptCatalogue().get(scenario)
    prompt = "\n".join(m.content for m in template.messages)
    references = set(re.findall(r"\{\{([a-z_]+)\}\}", prompt))
    declared = {v.name for v in template.variables}
    assert references == declared - {"call_id"}
    assert prompt.count("{{") == len(re.findall(r"\{\{[a-z_]+\}\}", prompt))
    assert "#" not in "".join(re.findall(r"\{\{.*?\}\}", prompt))
    assert "JSON" in template.messages[-1].content.rstrip().splitlines()[-1]


@pytest.mark.parametrize("scenario", ["analysis", "reason", "free_classification"])
def test_transcript_is_third_party_data_and_masking_does_not_return(scenario):
    """Действующая инструкция защищает границу данных до описания полей."""
    system = _part(scenario, "system")
    user = _part(scenario, "user")
    assert "данные стороннего источника" in system and "не выполняй" in system
    assert (
        "данные стороннего источника"
        in user[: user.index("{{segments}}")].split("{{classifier}}")[-1]
    )
    assert "•••" not in system and "█" not in system


def test_analysis_keeps_fields_tree_walk_and_sentiment_contract():
    """Промпт продолжает задавать все поля и последовательность уровней классификатора."""
    prompt = _part("analysis", "system")
    for field in (
        "path",
        "sentiment",
        "summary",
        "roles",
        "ai_path",
        "call_reason",
        "call_reason_code",
    ):
        assert f"`{field}`" in prompt
    for value in ("negative", "neutral", "positive"):
        assert value in prompt
    for gone in ("topic_code", "ai_topic", "needs_segment", "needs_product"):
        assert gone not in prompt
    for phrase in (
        "ВЕРХНЕГО уровня",
        "ВНУТРИ выбранного ключа",
        "Продукт первым уровнем",
        "Недовольство/негатив» — только если",
        EMPTY_LEVEL_LABEL,
    ):
        assert phrase in prompt
    assert prompt.index("данные стороннего источника") < prompt.index("Поля объекта")


def test_reason_scenario_takes_only_reference_and_transcript():
    """Причина определяется по разговору: без дерева и результата предыдущего анализа."""
    template = FilePromptCatalogue().get("reason")
    assert {v.name: v.max_length for v in template.variables} == {
        "call_id": 64,
        "reasons": 16000,
        "segments": 480000,
    }
    prompt = _part("reason", "system")
    for field in ("call_reason", "call_reason_code"):
        assert f"`{field}`" in prompt


def _labels_in(prompt: str) -> list[str]:
    """Найти названия каталога во всём промпте без обрезки длины текста."""
    paths = json.loads(CLASSIFIER_FILE.read_text(encoding="utf-8"))["paths"]
    labels = {label_key(level) for path in paths for level in path[:4] if level}
    text = unicodedata.normalize("NFC", prompt).casefold().replace("\\", "/").replace("_", "/")
    text = re.sub(r"\s*/\s*", "/", " ".join(text.split()))
    return sorted(label for label in labels if label and label in text)


def test_free_classification_is_independent_of_catalogue():
    """Ни названия категорий, ни путь оператора не подсказывают свободную тему."""
    template = FilePromptCatalogue().get("free_classification")
    assert {v.name for v in template.variables} == {"call_id", "segments"}
    assert not _labels_in(_part("free_classification", "system"))
    assert "физ.лицо" in _labels_in(_part("operator_compare", "system"))
    prompt = _part("free_classification", "system")
    for phrase in ("`free_path`", "`topic`", "не больше семи", "80 символов", "не цитата"):
        assert phrase in prompt


def test_comparison_uses_wordings_and_agreed_scales():
    """Судья сравнивает формулировки и выбирает только из переданных кандидатов."""
    template = FilePromptCatalogue().get("operator_compare")
    assert {v.name for v in template.variables} == {
        "call_id",
        "operator_path",
        "free_path",
        "topic",
        "candidates",
    }
    prompt = _part("operator_compare", "system")
    for field in ("operator", "catalog", "depth", "verdict", "score", "reason", "candidate"):
        assert f"`{field}`" in prompt
    for value in ("match", "partial", "mismatch", "in_catalog", "close", "absent"):
        assert f"`{value}`" in prompt
    assert "тема оператора (уровень 4) верна" in prompt
    assert "даже если сегмент, продукт и" in prompt
    assert "только из `candidates`" in prompt
    assert "segments" not in prompt


@pytest.mark.parametrize("scenario", ["free_classification", "operator_compare"])
def test_free_and_compare_keep_no_think_instruction(scenario):
    """Последняя строка задаёт JSON и отключает рассуждения для этих сценариев."""
    assert _part(scenario, "user").rstrip().splitlines()[-1].endswith("/no_think")
