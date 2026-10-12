"""Проверка файла перед передачей сценарию распознавания."""

from src.application.dto.audio import AudioConstraints, AudioUpload
from src.application.errors.audio import AudioProblem, AudioValidationError
from src.domain.transcription.media import resolve_audio_format


def validate_audio(
    *, filename: str, mime_type: str, content: bytes, constraints: AudioConstraints
) -> AudioUpload:
    """Проверить размер и формат байтов, вернуть DTO; отказ — AudioValidationError."""
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1][:255]
    mime_type = mime_type.split(";", 1)[0].strip().lower()
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not content:
        raise AudioValidationError(AudioProblem.EMPTY, "Audio file is empty.")
    if len(content) > constraints.max_bytes:
        raise AudioValidationError(
            AudioProblem.TOO_LARGE,
            f"Audio file exceeds {constraints.max_bytes // (1024 * 1024)} MB limit.",
        )
    allowed_mime_types = {value.lower() for value in constraints.allowed_mime_types}
    allowed_extensions = {value.lower() for value in constraints.allowed_extensions}
    audio_format = resolve_audio_format(filename, mime_type)
    if (
        mime_type not in allowed_mime_types and extension not in allowed_extensions
    ) or audio_format is None:
        raise AudioValidationError(AudioProblem.UNSUPPORTED, "Audio file type is not supported.")
    return AudioUpload(
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(content),
        content=content,
        audio_format=audio_format,
    )
