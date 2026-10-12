"""Формирование CSV с BOM, разделителем Excel и защитой ячеек от формул."""

import csv
import io
from collections.abc import Sequence

from src.application.dto.calls import CallSource
from src.application.dto.classifier import path_caption
from src.application.dto.files import FileResult
from src.application.dto.media_access import CSV_BOM, CSV_DELIMITER
from src.application.dto.search import SearchItem


def _csv_cell(value: str | int) -> str | int:
    """Keep untrusted text literal in spreadsheets; CSV quoting alone does not."""
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return "'" + value
    return value


class CsvExportRenderer:
    """Формировать файл из уже разрешённой поисковой выборки."""

    def render(self, items: Sequence[SearchItem], source: CallSource) -> FileResult:
        """Из DTO результатов собрать безопасный CSV для Excel, вернуть байты и имя."""
        buffer = io.StringIO()
        buffer.write(CSV_BOM)
        writer = csv.writer(buffer, delimiter=CSV_DELIMITER, lineterminator="\n")
        writer.writerow(
            [
                "session_id",
                "started_at",
                "operator",
                "project",
                "duration_s",
                "classification",
                "sentiment",
                "segment",
                "product",
                "matches",
                "quote",
                "link",
            ]
        )
        for item in items:
            quote = item.quotes[0].text if item.quotes else ""
            writer.writerow(
                _csv_cell(value)
                for value in (
                    item.session_id,
                    item.started_at.isoformat(),
                    item.operator_login or "",
                    item.project_title or "",
                    item.duration_s,
                    path_caption(item.ai_path),
                    item.sentiment or "",
                    item.segment or "",
                    item.product or "",
                    item.match_count,
                    quote,
                    f"/calls/{item.call_id}",
                )
            )
        name = "speech-export" if source == CallSource.NCC else f"speech-export-{source.value}"
        return FileResult(
            content=buffer.getvalue().encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            filename=f"{name}.csv",
        )
