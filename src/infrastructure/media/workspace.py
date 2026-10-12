"""Локальные временные каталоги обработки записей."""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class TemporaryRecordingWorkspace:
    """Выделять и очищать отдельный каталог каждой операции над записью."""

    @contextmanager
    def directory(self, *, prefix: str) -> Iterator[Path]:
        """Создать каталог с префиксом, выдать путь и удалить содержимое при выходе."""
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            yield Path(directory)
