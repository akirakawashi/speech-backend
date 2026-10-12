"""Содержимое загруженной записи, передаваемое сценарию распознавания."""

from pydantic import BaseModel, ConfigDict


class AudioUpload(BaseModel):
    """Проверенная запись и её формат для сценария распознавания."""

    model_config = ConfigDict(frozen=True)

    filename: str
    mime_type: str
    size_bytes: int
    content: bytes
    audio_format: str


class AudioConstraints(BaseModel):
    """Ограничения размера и поддерживаемых типов загруженной записи."""

    model_config = ConfigDict(frozen=True)
    max_bytes: int
    allowed_mime_types: tuple[str, ...]
    allowed_extensions: tuple[str, ...]
