"""Private local storage adapter. No static mount; server-generated keys only."""

import hashlib
import io
import re
import wave
from pathlib import Path

from fastapi import HTTPException

from src.api.config import MAX_AUDIO_BYTES


def validate_wav(data: bytes) -> tuple[bytes, int]:
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio exceeds 10 MiB")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getframerate(),
                audio.getcomptype(),
            ) != (1, 2, 16000, "NONE"):
                raise HTTPException(415, "Only WAV PCM16 mono 16 kHz is supported locally")
            frames = audio.getnframes()
            if frames <= 0 or frames > 480000:
                raise HTTPException(422, "Audio must be nonempty and at most 30 seconds")
            samples = audio.readframes(frames)
            if len(samples) != frames * 2:
                raise HTTPException(415, "Truncated WAV data")
    except (wave.Error, EOFError, ValueError):
        raise HTTPException(415, "Invalid WAV file") from None
    output = io.BytesIO()
    with wave.open(output, "wb") as normalized:
        normalized.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        normalized.writeframes(samples)
    return output.getvalue(), frames


def private_path(root: Path, key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}\.wav", key):
        raise ValueError("Invalid internal storage key")
    root = root.resolve()
    result = (root / key).resolve()
    if result.parent != root:
        raise ValueError("Storage path escaped root")
    return result


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
