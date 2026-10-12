from __future__ import annotations

import pytest

from src.domain.transcription.media import resolve_audio_format


@pytest.mark.parametrize(
    ("filename", "mime_type", "expected"),
    [
        ("call.mp3", "audio/mpeg", "mp3"),
        ("call.MP4", "audio/mp4", "m4a"),
        ("call.bin", "audio/wav", "wav"),
        ("call.webm", "", "webm"),
        ("call.bin", "application/octet-stream", None),
        ("call", "", None),
    ],
)
def test_resolve_audio_format_prefers_extension_then_mime(
    filename: str,
    mime_type: str,
    expected: str | None,
) -> None:
    assert resolve_audio_format(filename, mime_type) == expected
