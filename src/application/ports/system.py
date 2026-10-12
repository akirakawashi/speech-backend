"""Порт наблюдения за сборкой и внешними ресурсами сервиса."""

from typing import Protocol

from src.application.dto.system import MetricsText, Readiness, VersionInfo


class SystemProbe(Protocol):
    """Проверки состояния технических ресурсов без транспортных ошибок."""

    async def readiness(self) -> Readiness:
        """Проверить соединение и ревизию схемы; вернуть причину неготовности."""
        ...

    def version(self) -> VersionInfo:
        """Вернуть идентификатор артефакта и версии реализованных контрактов."""
        ...

    async def metrics(self) -> MetricsText:
        """Собрать и отформатировать счётчики обработки и очереди."""
        ...
