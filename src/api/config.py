from pathlib import Path

from pydantic import Field
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


MAX_AUDIO_BYTES = 10 * 1024 * 1024
POLICY_VERSION = "local-v1"
FAKE_WARNING = "FAKE / TEST DOUBLE: no real ASR, grammar model, or pronunciation assessment."
