"""Служебные сценарии готовности, версии и закрытых метрик."""

import hmac

from src.application.dto.system import MetricsText, VersionInfo
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.system import SystemProbe


class SystemService:
    """Отображать готовность ресурса и ограничивать доступ к его метрикам."""

    def __init__(self, probe: SystemProbe, *, metrics_key: str) -> None:
        """Принять порт состояния и ключ сбора метрик, не читая окружение."""
        self._probe = probe
        self._metrics_key = metrics_key.strip()

    async def ready(self) -> None:
        """Проверить ресурсы и при неготовности вернуть причину отказа сценария."""
        result = await self._probe.readiness()
        if not result.ready:
            raise ApplicationError(ErrorKind.UNAVAILABLE, result.detail or "database unavailable")

    def version(self) -> VersionInfo:
        """Вернуть описание сборки и контрактов из внедрённого порта."""
        return self._probe.version()

    async def metrics(self, authorization: str | None) -> MetricsText:
        """Сравнить ключ постоянного времени до чтения счётчиков и вернуть текст."""
        if not self._metrics_key:
            raise ApplicationError(ErrorKind.NOT_FOUND, "not_found")
        presented = ""
        if authorization and authorization.lower().startswith("bearer "):
            presented = authorization.split(" ", 1)[1].strip()
        if not hmac.compare_digest(presented, self._metrics_key):
            raise ApplicationError(ErrorKind.UNAUTHORIZED, "unauthorized", challenge=True)
        return await self._probe.metrics()
