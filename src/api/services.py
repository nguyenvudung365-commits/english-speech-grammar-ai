"""Application invariants and transaction boundaries (no model calls in HTTP handlers)."""

import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from src.api.config import FAKE_WARNING, PARTIAL_WARNING, REAL_WARNING, Settings
from src.api.models import (
    AudioAsset,
    Consent,
    GrammarResult,
    Job,
    PracticeSession,
    PronunciationResult,
    now,
)
from src.api.storage import digest, private_path

MAX_ATTEMPTS = 3


def adapter_config(settings: Settings) -> dict:
    """Pure data shared by enqueue, save_audio and the worker's CONFIG_MISMATCH check."""
    config = {"fusion": "separate-evidence-v1", "seed": 42}
    if settings.adapter_backend == "real":
        return config | {
            "backend": "real-asr-grammar-v1",
            "stages": {"asr": "faster-whisper", "pronunciation": "fake", "grammar": "t5-gec"},
            "asr_model": Path(settings.asr_model).name,
            "asr_revision": settings.asr_revision,
            "grammar_model": Path(settings.grammar_model).name,
            "grammar_revision": settings.grammar_revision,
        }
    return config | {"backend": "fake-v1", "stages": {}}


def canonical_hash(value: dict) -> str:
    return digest(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def write_lock(db: Session) -> None:
    # Serializes local mutation/capacity checks across API processes, not in-memory locks.
    # Deliberately conservative for the small local demo; inference never holds this lock.
    db.execute(text("SELECT pg_advisory_xact_lock(73490123)"))


def owned(db: Session, user_id: str, sid: str, *, lock=False, deleting=False) -> PracticeSession:
    query = select(PracticeSession).where(
        PracticeSession.id == sid, PracticeSession.owner_user_id == user_id
    )
    if not deleting:
        query = query.where(PracticeSession.deletion_requested_at.is_(None))
    if lock:
        query = query.with_for_update()
    value = db.scalar(query)
    if value is None:
        raise HTTPException(404, "Not found")
    if not deleting:
        asset = db.scalar(select(AudioAsset).where(AudioAsset.session_id == sid))
        expiry = asset.expires_at if asset else value.created_at + timedelta(hours=24)
        if expiry <= now():
            raise HTTPException(404, "Not found")
    return value


def unexpired_session():
    """SQL history predicate: exact expiry regardless of whether scheduler has run."""
    return ((AudioAsset.id.is_not(None)) & (AudioAsset.expires_at > now())) | (
        (AudioAsset.id.is_(None)) & (PracticeSession.created_at > now() - timedelta(hours=24))
    )


def active_consent(db: Session, practice: PracticeSession) -> Consent:
    consent = db.get(Consent, practice.consent_id)
    if not consent or consent.owner_user_id != practice.owner_user_id or consent.withdrawn_at:
        raise HTTPException(409, "Consent no longer active")
    return consent


def session_view(value: PracticeSession) -> dict:
    return {
        name: getattr(value, name)
        for name in (
            "id",
            "consent_id",
            "created_at",
            "updated_at",
            "mode",
            "reference_text",
            "asr_draft",
            "draft_transcript",
            "confirmed_transcript",
            "transcript_revision",
            "transcript_confirmed_at",
        )
    }


def job_view(value: Job) -> dict:
    # Explicit allow-list: no private file keys, input hash, lease owner or snapshots.
    return {
        "id": value.id,
        "session_id": value.session_id,
        "kind": value.kind,
        "status": value.status,
        "created_at": value.created_at,
        "started_at": value.started_at,
        "finished_at": value.finished_at,
        "stages": value.stage_state_json,
        "error_code": value.error_code,
        "attempt_no": value.attempt_no,
        "max_attempts": MAX_ATTEMPTS,
        "retry_of_job_id": value.retry_of_job_id,
        "fake": value.provenance_json.get("fake", True),
        "warning": value.provenance_json.get("warning", FAKE_WARNING),
    }


def queue_capacity(db: Session, settings: Settings, owner: str) -> None:
    active = ("queued", "running")
    total = db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(active)))
    personal = db.scalar(
        select(func.count())
        .select_from(Job)
        .join(PracticeSession)
        .where(Job.status.in_(active), PracticeSession.owner_user_id == owner)
    )
    if total >= settings.queue_global or personal >= settings.queue_per_user:
        raise HTTPException(429, "Local queue full; try again later", headers={"Retry-After": "5"})


def existing_job(db: Session, key: str, owner: str, request_hash: str) -> Job | None:
    value = db.scalar(select(Job).where(Job.idempotency_key == key))
    if value:
        practice = db.get(PracticeSession, value.session_id)
        if not practice or practice.owner_user_id != owner:
            raise HTTPException(404, "Not found")
        if value.request_sha256 != request_hash:
            raise HTTPException(409, "Idempotency key used for another input")
    return value


def enqueue(
    db: Session,
    settings: Settings,
    practice: PracticeSession,
    kind: str,
    key: str,
    snapshot: dict,
    asset: AudioAsset | None = None,
    retry_of: Job | None = None,
) -> Job:
    active_consent(db, practice)
    config = adapter_config(settings)
    config_hash = canonical_hash(config)
    real = config["backend"] != "fake-v1"
    identity = {
        "kind": kind,
        "session_id": practice.id,
        "snapshot": snapshot,
        "config": config_hash,
    }
    if retry_of:  # only when retrying, so normal hashes still match save_audio
        identity["retry_of"] = retry_of.id
    request_hash = canonical_hash(identity)
    previous = existing_job(db, key, practice.owner_user_id, request_hash)
    if previous:
        return previous
    queue_capacity(db, settings, practice.owner_user_id)
    source = hashlib.sha256()
    for path in sorted(Path("src").rglob("*.py")):
        source.update(path.as_posix().encode())
        source.update(path.read_bytes())
    stages = {
        "asr": "queued" if kind == "speech" else "not_requested",
        "pronunciation": "queued"
        if kind == "speech" and practice.mode == "reference_reading"
        else "not_requested",
        "grammar": "queued" if kind == "grammar" else "not_requested",
    }
    fake = not real or any(
        config["stages"][name] == "fake" for name, state in stages.items() if state != "not_requested"
    )
    warning = FAKE_WARNING if not real else PARTIAL_WARNING if fake else REAL_WARNING
    value = Job(
        session_id=practice.id,
        audio_asset_id=asset.id if asset else None,
        kind=kind,
        idempotency_key=key,
        request_sha256=request_hash,
        input_snapshot_json=snapshot,
        config_sha256=config_hash,
        provenance_json={
            "schema_version": 1,
            # Any requested fake stage keeps the whole job labelled fake.
            "fake": fake,
            "backend": config["backend"],
            "source_sha256": source.hexdigest(),
            "seed": 42,
            "dataset": "not_applicable",
            "model": f"asr:{config['asr_model']}@{config['asr_revision']};"
            f"grammar:{config['grammar_model']}@{config['grammar_revision']}"
            if real
            else "no-model-test-double",
            "pilot": "not_run",
            "device": "cpu",
            "warning": warning,
        },
        stage_state_json=stages,
        retry_of_job_id=retry_of.id if retry_of else None,
        attempt_no=retry_of.attempt_no + 1 if retry_of else 1,
    )
    db.add(value)
    db.flush()
    return value


def retry_job(db: Session, settings: Settings, user_id: str, jid: str, key: str) -> Job:
    old = db.get(Job, jid)
    if old is None:
        raise HTTPException(404, "Not found")
    practice = owned(db, user_id, old.session_id, lock=True)
    # Idempotent replay must come before the "already retried" check below.
    replay = db.scalar(select(Job).where(Job.idempotency_key == key))
    if replay and replay.retry_of_job_id == jid and replay.session_id == old.session_id:
        return replay
    if old.status != "failed":
        raise HTTPException(409, "Only failed jobs can be retried")
    if db.scalar(select(Job.id).where(Job.retry_of_job_id == old.id)):
        raise HTTPException(409, "Job already retried; retry the newest attempt")
    if old.attempt_no >= MAX_ATTEMPTS:
        raise HTTPException(409, "Retry limit reached")
    asset = None
    if old.kind == "grammar":
        if (
            practice.confirmed_transcript is None
            or old.input_snapshot_json["revision"] != practice.transcript_revision
        ):
            raise HTTPException(409, "Transcript changed; confirm and queue a new analysis")
    else:
        asset = db.get(AudioAsset, old.audio_asset_id) if old.audio_asset_id else None
        if not asset or asset.state != "ready" or asset.expires_at <= now():
            raise HTTPException(409, "Audio unavailable; create a new session")
    return enqueue(
        db, settings, practice, old.kind, key, old.input_snapshot_json, asset, retry_of=old
    )


def save_audio(
    db: Session, settings: Settings, practice: PracticeSession, data: bytes, frames: int, key: str
) -> tuple[AudioAsset, Job, Path | None]:
    active_consent(db, practice)
    previous = db.scalar(select(AudioAsset).where(AudioAsset.session_id == practice.id))
    snapshot = {
        "audio_sha256": digest(data),
        "mode": practice.mode,
        "reference": practice.reference_text,
    }
    request_hash = canonical_hash(
        {
            "kind": "speech",
            "session_id": practice.id,
            "snapshot": snapshot,
            "config": canonical_hash(adapter_config(settings)),
        }
    )
    existing = existing_job(db, key, practice.owner_user_id, request_hash)
    if existing:
        if not previous or previous.state != "ready" or previous.expires_at <= now():
            raise HTTPException(409, "Audio unavailable; create a new session")
        return previous, existing, None
    if previous:
        raise HTTPException(409, "Only one audio per session; create a new session")
    queue_capacity(db, settings, practice.owner_user_id)
    storage_key = uuid4().hex + ".wav"
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    target = private_path(settings.storage_root, storage_key)
    timestamp = now()
    # Exclusive creation and server key: never follow client filenames.
    try:
        with target.open("xb") as output:
            output.write(data)
        asset = AudioAsset(
            session_id=practice.id,
            storage_key=storage_key,
            sha256=digest(data),
            byte_size=len(data),
            duration_ms=math.ceil(frames / 16),
            frame_count=frames,
            created_at=timestamp,
            expires_at=timestamp + timedelta(hours=24),
        )
        db.add(asset)
        db.flush()
        job = enqueue(db, settings, practice, "speech", key, snapshot, asset)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return asset, job, target


def report(db: Session, practice: PracticeSession) -> dict:
    jobs = list(
        db.scalars(
            select(Job).where(Job.session_id == practice.id).order_by(Job.created_at, Job.id)
        )
    )
    grammar = []
    pronunciation = []
    for job in jobs:
        g = db.scalar(select(GrammarResult).where(GrammarResult.job_id == job.id))
        if g:
            grammar.append(
                {
                    "job_id": job.id,
                    "source_text": g.source_text,
                    "corrected_text": g.corrected_text,
                    "revision": g.transcript_revision,
                    "edits": g.edits_json,
                    "warnings": g.warnings_json,
                    "is_current": g.transcript_revision == practice.transcript_revision
                    and practice.confirmed_transcript is not None,
                    "fake": job.provenance_json.get("fake", True),
                }
            )
        p = db.scalar(select(PronunciationResult).where(PronunciationResult.job_id == job.id))
        if p:
            pronunciation.append(
                {
                    "job_id": job.id,
                    "score": p.score,
                    "evidence": p.evidence_json,
                    "feedback": p.feedback_json,
                    "fake": job.provenance_json.get("fake", True),
                }
            )
    return {
        "fake": any(job.provenance_json.get("fake", True) for job in jobs) or not jobs,
        # Real-backend jobs carry their own warning; fake-v1 jobs fall back to FAKE_WARNING.
        "warning": next(
            (w for job in jobs if (w := job.provenance_json.get("warning", FAKE_WARNING)) != FAKE_WARNING),
            FAKE_WARNING,
        ),
        "rule_version": "separate-evidence-v1",
        "jobs": [job_view(job) for job in jobs],
        "grammar": grammar,
        "pronunciation": pronunciation,
        "needs_confirmation": practice.confirmed_transcript is None,
        "report_complete": any(item["is_current"] for item in grammar)
        and (practice.mode != "reference_reading" or bool(pronunciation)),
    }


def delete_practice(database, settings: Settings, owner: str, sid: str) -> bool:
    """Durable tombstone before filesystem IO; failures stay pending for scheduler retry."""
    with database.sessions.begin() as db:
        write_lock(db)
        practice = owned(db, owner, sid, lock=True, deleting=True)
        practice.deletion_requested_at = practice.deletion_requested_at or now()
        for job in db.scalars(select(Job).where(Job.session_id == sid, Job.status == "queued")):
            job.status, job.finished_at, job.error_code = "failed", now(), "DELETION_REQUESTED"
        running = db.scalar(select(Job.id).where(Job.session_id == sid, Job.status == "running"))
        asset = db.scalar(select(AudioAsset).where(AudioAsset.session_id == sid))
        if asset:
            asset.state = "deleting"
        key = asset.storage_key if asset else None
        consent_id = practice.consent_id
    if running:
        return False  # wait until compute has stopped or its lease has been reaped
    try:
        if key:
            private_path(settings.storage_root, key).unlink(missing_ok=True)
    except OSError:
        return False
    with database.sessions.begin() as db:
        write_lock(db)
        ids = select(Job.id).where(Job.session_id == sid)
        db.execute(delete(GrammarResult).where(GrammarResult.job_id.in_(ids)))
        db.execute(delete(PronunciationResult).where(PronunciationResult.job_id.in_(ids)))
        db.execute(delete(Job).where(Job.session_id == sid))
        db.execute(delete(AudioAsset).where(AudioAsset.session_id == sid))
        db.execute(delete(PracticeSession).where(PracticeSession.id == sid))
        remaining = db.scalar(
            select(PracticeSession.id).where(PracticeSession.consent_id == consent_id)
        )
        if not remaining:
            db.execute(delete(Consent).where(Consent.id == consent_id))
    return True


def withdraw_consent(database, settings: Settings, owner: str, cid: str) -> bool:
    """Block new jobs, then delete every session that used this consent."""
    with database.sessions.begin() as db:
        write_lock(db)
        consent = db.get(Consent, cid)
        if consent is None or consent.owner_user_id != owner:
            raise HTTPException(404, "Not found")
        consent.withdrawn_at = consent.withdrawn_at or now()
        sids = list(db.scalars(select(PracticeSession.id).where(PracticeSession.consent_id == cid)))
    pending = False
    for sid in sids:
        try:
            pending |= not delete_practice(database, settings, owner, sid)
        except HTTPException:
            pass  # scheduler cleanup deleted it first
    return not pending
