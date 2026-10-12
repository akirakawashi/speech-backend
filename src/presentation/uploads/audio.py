"""Перевод FastAPI UploadFile в DTO приложения."""

from fastapi import HTTPException, UploadFile

from src.application.dto.audio import AudioConstraints, AudioUpload
from src.application.errors.audio import AudioProblem, AudioValidationError
from src.application.transcription.uploads import validate_audio

_STATUS = {AudioProblem.EMPTY: 400, AudioProblem.TOO_LARGE: 413, AudioProblem.UNSUPPORTED: 415}


async def read_and_validate_audio(upload: UploadFile, constraints: AudioConstraints) -> AudioUpload:
    """Прочитать HTTP-файл, проверить сценарием и вернуть DTO либо HTTP-ошибку."""
    try:
        return validate_audio(
            filename=upload.filename or "audio",
            mime_type=upload.content_type or "",
            content=await upload.read(),
            constraints=constraints,
        )
    except AudioValidationError as exc:
        raise HTTPException(_STATUS[exc.reason], str(exc)) from exc
