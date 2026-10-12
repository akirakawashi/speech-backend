"""Набор сценариев одного запроса без знания о FastAPI и сессии базы."""

from src.application.admin.service import AdminService
from src.application.authentication.service import AuthenticationService
from src.application.calls.service import CallsService
from src.application.dashboard.service import DashboardService
from src.application.media.service import MediaService
from src.application.quality_read.service import QualityReadService
from src.application.reports.service import ReportsService


class HttpApplication:
    """Держать уже собранные сценарии, которые разделяют одну транзакцию запроса."""

    def __init__(
        self,
        *,
        media: MediaService,
        authentication: AuthenticationService,
        calls: CallsService,
        admin: AdminService,
        quality_read: QualityReadService,
        reports: ReportsService,
        dashboard: DashboardService,
    ) -> None:
        """Получить сценарии с ресурсами, которыми управляет DI."""
        self.media = media
        self.authentication = authentication
        self.calls = calls
        self.admin = admin
        self.quality_read = quality_read
        self.reports = reports
        self.dashboard = dashboard
