"""Single source of truth for audio format normalization.

Providers never decide which formats are acceptable: they receive an already
validated request carrying a normalized format. Keeping the tables here is what
allows `read_and_validate_audio` to reject an unknown format with a 415 before
any provider or network call happens.
"""

from __future__ import annotations

from typing import Final

_MIME_TO_FORMAT: Final[dict[str, str]] = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/aac": "aac",
}

_EXTENSION_TO_FORMAT: Final[dict[str, str]] = {
    "mp3": "mp3",
    "mp4": "m4a",
    "m4a": "m4a",
    "wav": "wav",
    "webm": "webm",
    "ogg": "ogg",
    "flac": "flac",
    "aac": "aac",
}


def resolve_audio_format(filename: str, mime_type: str) -> str | None:
    """Return the normalized audio format, or None when it cannot be determined."""

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in _EXTENSION_TO_FORMAT:
        return _EXTENSION_TO_FORMAT[extension]

    normalized_mime = mime_type.strip().lower()
    if normalized_mime in _MIME_TO_FORMAT:
        return _MIME_TO_FORMAT[normalized_mime]

    return None
