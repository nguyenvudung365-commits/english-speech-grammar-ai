from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://speech@127.0.0.1:55439/speech"
    storage_root: Path = Path("data/private-audio")
    origin: str = "http://127.0.0.1:8000"
    cookie_secure: bool = False  # localhost HTTP only; never deploy this configuration.
    session_hours: int = Field(default=8, ge=1, le=24)
    queue_global: int = Field(default=32, ge=1, le=1000)
    queue_per_user: int = Field(default=4, ge=1, le=100)
    auth_rate_per_minute: int = Field(default=20, ge=1, le=1000)
    adapter_backend: Literal["fake", "real"] = "fake"
    asr_model: str = ""  # local faster-whisper model directory (real backend only)
    asr_revision: str = ""  # Hugging Face commit of the downloaded checkpoint, for provenance
    grammar_model: str = ""  # local seq2seq GEC checkpoint directory (real backend only)
    grammar_revision: str = ""  # Hugging Face commit of the grammar checkpoint, for provenance

    @model_validator(mode="after")
    def real_backend_needs_models(self):
        if self.adapter_backend == "real":
            missing = [
                name.upper()
                for name in ("asr_model", "asr_revision", "grammar_model", "grammar_revision")
                if not getattr(self, name).strip()
            ]
            if missing:
                raise ValueError(f"ADAPTER_BACKEND=real requires non-empty {', '.join(missing)}")
        return self


MAX_AUDIO_BYTES = 10 * 1024 * 1024
POLICY_VERSION = "local-v1"
FAKE_WARNING = "FAKE / TEST DOUBLE: no real ASR, grammar model, or pronunciation assessment."
PARTIAL_WARNING = "Real ASR and grammar models; pronunciation is still a FAKE test double."
REAL_WARNING = "Model output only; may be wrong. Pronunciation is not assessed and has no score."
