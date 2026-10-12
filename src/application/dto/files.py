"""Результат формирования файла без объектов HTTP или Excel-библиотеки."""

from pydantic import BaseModel, ConfigDict, Field

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FileResult(BaseModel):
    """Байты файла, формат и предлагаемое имя при сохранении."""

    model_config = ConfigDict(frozen=True)
    content: bytes = Field(repr=False)
    media_type: str
    filename: str
