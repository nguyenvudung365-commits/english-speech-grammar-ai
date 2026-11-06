from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=12, max_length=128)


class ConsentInput(StrictModel):
    policy_version: Literal["local-v1"]
    allow_server_processing: Literal[True]
    allow_anonymized_retention: bool = False


class SessionInput(StrictModel):
    consent_id: UUID
    mode: Literal["reference_reading", "grammar_only"]
    reference_text: str | None = Field(default=None, min_length=1, max_length=3000)

    @model_validator(mode="after")
    def reference_matches_mode(self):
        if (self.mode == "reference_reading") != bool(self.reference_text):
            raise ValueError("Reference required only for reference_reading")
        return self


class RevisionInput(StrictModel):
    expected_revision: int = Field(ge=0)


class TranscriptInput(RevisionInput):
    text: str = Field(min_length=1, max_length=3000)


class GrammarInput(RevisionInput):
    idempotency_key: UUID
