"""The declarative base, and the helper the models need.

They live outside `app/db/models` on purpose: importing any submodule of a
package runs the package's `__init__` first, so helpers placed inside it could
not be reached without loading every model.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class SpeechBase(DeclarativeBase):
    """Declarative base Speech Analytics.

    У сервиса своя база данных и своё дерево миграций (`alembic`), и
    `alembic` автогенерирует по `target_metadata` этого класса. Имя и отдельный
    класс остались от монорепозитория, где рядом жил общий `Base` основного
    приложения: с общим базовым классом дерево миграций речи видело бы чужие
    таблицы как отсутствующие в своей базе и предложило бы их создать.
    """


def utc_now() -> datetime:
    """Вернуть текущий момент с явным часовым поясом UTC."""
    return datetime.now(UTC)
