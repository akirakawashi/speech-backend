"""Разряды качества и правила чтения этапов скрипта продаж."""

from typing import Final, Literal

from src.domain.quality.script import CallScript

Bucket = Literal["good", "fair", "poor"]
SCORE_GOOD: Final[int] = 85
SCORE_FAIR: Final[int] = 70
BUCKET_BOUNDS: Final[dict[Bucket, tuple[int | None, int | None]]] = {
    "good": (SCORE_GOOD, None),
    "fair": (SCORE_FAIR, SCORE_GOOD - 1),
    "poor": (None, SCORE_FAIR - 1),
}


def bucket_of(score: int | None) -> Bucket | None:
    """По баллу от 0 до 100 вернуть разряд качества; без оценки вернуть None."""
    if score is None:
        return None
    if score >= SCORE_GOOD:
        return "good"
    return "fair" if score >= SCORE_FAIR else "poor"


def stage_label(script: CallScript, key: str) -> str:
    """Вернуть подпись этапа выбранного скрипта либо исходный неизвестный ключ."""
    stage = script.stage(key)
    return stage.label if stage else key


def stage_mandatory(script: CallScript, key: str) -> bool:
    """Вернуть признак обязательного этапа; неизвестный ключ не является обязательным."""
    stage = script.stage(key)
    return bool(stage and stage.mandatory)


def mandatory_keys(script: CallScript) -> list[str]:
    """Вернуть ключи обязательных этапов в порядке скрипта."""
    return [stage.key for stage in script.stages if stage.mandatory]
