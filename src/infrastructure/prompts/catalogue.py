"""Чтение встроенных JSON-шаблонов без зависимости от текущего каталога."""

from functools import lru_cache
from importlib.resources import files

from src.application.dto.model_execution import ModelScenario, PromptTemplate


@lru_cache(maxsize=1)
def _templates() -> dict[ModelScenario, PromptTemplate]:
    """Загрузить и проверить пакетные ресурсы при первом обращении."""
    root = files("src.infrastructure.data.prompts")
    templates = {}
    for resource in root.iterdir():
        if resource.name.endswith(".json"):
            template = PromptTemplate.model_validate_json(resource.read_text(encoding="utf-8"))
            templates[template.scenario] = template
    return templates


class FilePromptCatalogue:
    """Получить проверенный шаблон из ресурсов установленного пакета."""

    def get(self, scenario: ModelScenario) -> PromptTemplate:
        """Вернуть шаблон сценария; неизвестный ключ не подменяется другим."""
        return _templates()[scenario]
