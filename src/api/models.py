"""Seven business tables plus one explicitly technical login-session table."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Identity, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    credential: Mapped[str] = mapped_column(Text)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    """Technical auth storage, not an eighth business entity. Store token hashes only."""

    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Consent(Identity, Base):
    __tablename__ = "consent_records"
    __table_args__ = (
        CheckConstraint("allow_server_processing = true", name="consent_processing"),
        CheckConstraint("audio_retention_hours = 24", name="consent_retention"),
    )
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    policy_version: Mapped[str] = mapped_column(String(50))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    allow_server_processing: Mapped[bool] = mapped_column(Boolean)
    allow_external_processing: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_anonymized_retention: Mapped[bool] = mapped_column(Boolean, default=False)
    audio_retention_hours: Mapped[int] = mapped_column(Integer, default=24)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PracticeSession(Identity, Base):
    __tablename__ = "practice_sessions"
    __table_args__ = (
        CheckConstraint("mode IN ('reference_reading','grammar_only')", name="session_mode"),
        CheckConstraint(
            "(mode = 'reference_reading' AND length(trim(reference_text)) > 0 AND reference_text IS NOT NULL) OR (mode = 'grammar_only' AND reference_text IS NULL)",
            name="session_reference",
        ),
        CheckConstraint("transcript_revision >= 0", name="session_revision"),
        CheckConstraint(
            "(confirmed_transcript IS NULL AND transcript_confirmed_at IS NULL) OR (confirmed_transcript IS NOT NULL AND length(trim(confirmed_transcript)) > 0 AND transcript_confirmed_at IS NOT NULL)",
            name="session_confirmation",
        ),
        CheckConstraint("updated_at >= created_at", name="session_times"),
        Index("ix_sessions_owner_created", "owner_user_id", "created_at"),
    )
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    consent_id: Mapped[str] = mapped_column(ForeignKey("consent_records.id"), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    mode: Mapped[str] = mapped_column(String(30))
    reference_text: Mapped[str | None] = mapped_column(Text)
    asr_draft: Mapped[str | None] = mapped_column(Text)
    draft_transcript: Mapped[str | None] = mapped_column(Text)
    confirmed_transcript: Mapped[str | None] = mapped_column(Text)
    transcript_revision: Mapped[int] = mapped_column(Integer, default=0)
    transcript_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AudioAsset(Identity, Base):
    __tablename__ = "audio_assets"
    __table_args__ = (
        CheckConstraint("byte_size > 0 AND byte_size <= 10485760", name="audio_size"),
        CheckConstraint("duration_ms > 0 AND duration_ms <= 30000", name="audio_duration"),
        CheckConstraint(
            "sample_rate_hz = 16000 AND channels = 1 AND encoding = 'pcm_s16le_wav'",
            name="audio_format",
        ),
        CheckConstraint("length(sha256) = 64", name="audio_hash"),
        CheckConstraint("state IN ('ready','deleting','deleted','missing')", name="audio_state"),
        CheckConstraint("expires_at = created_at + interval '24 hours'", name="audio_expiry"),
        Index("ix_audio_expiry", "state", "expires_at"),
    )
    session_id: Mapped[str] = mapped_column(ForeignKey("practice_sessions.id"), unique=True)
    storage_key: Mapped[str] = mapped_column(String(80), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    frame_count: Mapped[int] = mapped_column(Integer)  # preserve exact frame boundary evidence
    sample_rate_hz: Mapped[int] = mapped_column(Integer, default=16000)
    channels: Mapped[int] = mapped_column(Integer, default=1)
    encoding: Mapped[str] = mapped_column(String(30), default="pcm_s16le_wav")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), default="ready")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Identity, Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('speech','grammar')", name="job_kind"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','partial','failed')", name="job_status"
        ),
        CheckConstraint("attempt_no >= 1", name="job_attempt"),
        CheckConstraint(
            "length(request_sha256) = 64 AND length(config_sha256) = 64", name="job_hashes"
        ),
        CheckConstraint(
            "(kind = 'speech' AND audio_asset_id IS NOT NULL) OR kind = 'grammar'",
            name="speech_asset",
        ),
        CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status IN ('succeeded','partial','failed') AND finished_at IS NOT NULL)",
            name="job_lifecycle",
        ),
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_session_created", "session_id", "created_at"),
    )
    session_id: Mapped[str] = mapped_column(ForeignKey("practice_sessions.id"))
    audio_asset_id: Mapped[str | None] = mapped_column(ForeignKey("audio_assets.id"))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    retry_of_job_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_jobs.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    input_snapshot_json: Mapped[dict] = mapped_column(JSONB)
    provenance_json: Mapped[dict] = mapped_column(JSONB)
    config_sha256: Mapped[str] = mapped_column(String(64))
    stage_state_json: Mapped[dict] = mapped_column(JSONB)
    output_summary_json: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(150))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PronunciationResult(Identity, Base):
    __tablename__ = "pronunciation_results"
    __table_args__ = (
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)", name="pronunciation_score"
        ),
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("analysis_jobs.id"), unique=True)
    method_id: Mapped[str] = mapped_column(String(50))
    rubric_version: Mapped[str] = mapped_column(String(50))
    score: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[dict] = mapped_column(JSONB)
    feedback_json: Mapped[dict] = mapped_column(JSONB)


class GrammarResult(Identity, Base):
    __tablename__ = "grammar_results"
    __table_args__ = (
        CheckConstraint(
            "transcript_revision >= 1 AND length(trim(source_text)) > 0", name="grammar_source"
        ),
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("analysis_jobs.id"), unique=True)
    transcript_revision: Mapped[int] = mapped_column(Integer)
    source_text: Mapped[str] = mapped_column(Text)
    corrected_text: Mapped[str] = mapped_column(Text)
    edits_json: Mapped[list] = mapped_column(JSONB)
    warnings_json: Mapped[list] = mapped_column(JSONB)
