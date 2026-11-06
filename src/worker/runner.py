import argparse
import time
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select

from src.api.config import FAKE_WARNING, Settings
from src.api.db import Database
from src.api.models import (
    AudioAsset,
    AuthSession,
    Consent,
    GrammarResult,
    Job,
    PracticeSession,
    PronunciationResult,
    User,
    now,
)
from src.api.services import delete_practice, write_lock
from src.api.storage import private_path
from src.worker.contracts import GrammarOutput
from src.worker.fakes import AdapterFailure, FakeAdapters, LanguageAdapter


def claim_job(database: Database, owner: str) -> str | None:
    with database.sessions.begin() as db:
        job = db.scalar(
            select(Job)
            .where(Job.status == "queued")
            .order_by(Job.created_at, Job.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.status, job.started_at = "running", now()
        job.lease_owner = owner
        job.lease_expires_at = now() + timedelta(seconds=120)
        job.heartbeat_at = now()
        job.stage_state_json = {
            key: "running" if value == "queued" else value
            for key, value in job.stage_state_json.items()
        }
        return job.id


def heartbeat(database: Database, jid: str, owner: str) -> bool:
    with database.sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.id == jid).with_for_update())
        if (
            not job
            or job.status != "running"
            or job.lease_owner != owner
            or job.lease_expires_at <= now()
        ):
            return False
        job.heartbeat_at = now()
        job.lease_expires_at = now() + timedelta(seconds=120)
        return True


def expire_leases(database: Database) -> int:
    with database.sessions.begin() as db:
        write_lock(db)
        values = list(
            db.scalars(
                select(Job)
                .where(Job.status == "running", Job.lease_expires_at <= now())
                .with_for_update(skip_locked=True)
            )
        )
        for job in values:
            job.status, job.finished_at, job.error_code = "failed", now(), "WORKER_LOST"
            job.error_message = "Worker lease expired; no automatic replay"
            job.stage_state_json = {
                key: "failed" if value == "running" else value
                for key, value in job.stage_state_json.items()
            }
        return len(values)


def complete_job(
    database: Database,
    settings: Settings,
    jid: str,
    owner: str,
    adapters: LanguageAdapter | None = None,
) -> bool:
    adapters = adapters or FakeAdapters()
    # Snapshot detached before compute; no row lock or transaction held during adapter calls.
    with database.sessions() as db:
        job = db.get(Job, jid)
        if (
            not job
            or job.status != "running"
            or job.lease_owner != owner
            or job.lease_expires_at <= now()
        ):
            return False
        snapshot, kind, stages = job.input_snapshot_json, job.kind, dict(job.stage_state_json)
        asset = db.get(AudioAsset, job.audio_asset_id) if job.audio_asset_id else None
        practice = db.get(PracticeSession, job.session_id)
        consent = db.get(Consent, practice.consent_id)
        allowed = not practice.deletion_requested_at and not consent.withdrawn_at
        if asset:
            allowed &= asset.state == "ready" and asset.expires_at > now()
        path = private_path(settings.storage_root, asset.storage_key) if asset else None
    outputs, errors = {}, {}
    audio = None
    if kind == "speech" and allowed:
        try:
            audio = path.read_bytes()
        except OSError:
            allowed = False
    for stage, state in stages.items():
        if state == "not_requested":
            continue
        try:
            if not allowed:
                raise AdapterFailure("INPUT_UNAVAILABLE")
            if stage == "asr":
                result = adapters.asr(audio)
                if not isinstance(result, str) or not result.strip() or len(result) > 3000:
                    raise ValueError("Invalid ASR output")
            elif stage == "pronunciation":
                result = adapters.pronunciation(audio, snapshot["reference"])
                if not isinstance(result, dict) or result.get("fake") is not True or result.get("score") is not None:
                    raise ValueError("Uncalibrated local adapters cannot produce scores")
            else:
                result = GrammarOutput.model_validate(adapters.grammar(snapshot["text"])).validate_source(snapshot["text"])
            outputs[stage] = result
            stages[stage] = "succeeded"
        except AdapterFailure as error:
            stages[stage], errors[stage] = "failed", error.code
        except (ValidationError, ValueError):
            stages[stage], errors[stage] = "failed", "INVALID_ADAPTER_OUTPUT"
        except Exception:  # noqa: BLE001 - untrusted adapter boundary; sanitize failure, keep other stage
            # Never persist adapter exception strings (may contain private text or paths).
            stages[stage], errors[stage] = "failed", "ADAPTER_ERROR"
    with database.sessions.begin() as db:
        write_lock(db)
        # Consistent lock order: session before job, matching HTTP mutations/deletion.
        practice = db.scalar(
            select(PracticeSession).where(PracticeSession.id == job.session_id).with_for_update()
        )
        value = db.scalar(select(Job).where(Job.id == jid).with_for_update())
        if (
            not value
            or value.status != "running"
            or value.lease_owner != owner
            or value.lease_expires_at <= now()
        ):
            return False
        if practice.deletion_requested_at:
            outputs = {}
            errors = {
                key: "DELETION_REQUESTED"
                for key, state in stages.items()
                if state != "not_requested"
            }
            stages = {
                key: "failed" if state != "not_requested" else state
                for key, state in stages.items()
            }
        if "asr" in outputs:
            practice.asr_draft = outputs["asr"]
            if practice.transcript_revision == 0 and practice.draft_transcript is None:
                practice.draft_transcript = outputs["asr"]
                practice.transcript_revision = 1
                practice.updated_at = now()
        if "pronunciation" in outputs:
            p = outputs["pronunciation"]
            db.add(
                PronunciationResult(
                    job_id=jid,
                    method_id="fake-no-assessment-v1",
                    rubric_version="not-calibrated",
                    score=None,
                    evidence_json=p,
                    feedback_json={"warning": FAKE_WARNING},
                )
            )
        if "grammar" in outputs:
            g = outputs["grammar"]
            # The immutable confirmed snapshot, never a mutable session draft.
            db.add(
                GrammarResult(
                    job_id=jid,
                    transcript_revision=snapshot["revision"],
                    source_text=snapshot["text"],
                    corrected_text=g["corrected_text"],
                    edits_json=g["edits"],
                    warnings_json=g["warnings"],
                )
            )
        value.stage_state_json = stages
        value.output_summary_json = {
            "fake": True,
            "warning": FAKE_WARNING,
            "errors": errors,
            "fusion_rule": "separate-evidence-v1",
        }
        value.status = "partial" if outputs and errors else "succeeded" if outputs else "failed"
        value.finished_at = now()
        value.error_code = next(iter(errors.values()), None)
        value.error_message = "One or more requested stages failed" if errors else None
    return True


def run_once(
    database: Database, settings: Settings, adapters: LanguageAdapter | None = None
) -> bool:
    owner = uuid4().hex
    expire_leases(database)
    jid = claim_job(database, owner)
    if not jid:
        return False
    complete_job(database, settings, jid, owner, adapters)
    return True


def cleanup(database: Database, settings: Settings) -> int:
    expire_leases(database)
    with database.sessions() as db:
        # No anonymization proof exists: conservatively delete ALL expired session content,
        # even when optional retention was selected. Typed-only sessions also expire at 24h.
        rows = db.execute(
            select(PracticeSession.id, PracticeSession.owner_user_id)
            .outerjoin(AudioAsset)
            .where(
                (PracticeSession.deletion_requested_at.is_not(None))
                | (AudioAsset.expires_at <= now())
                | (
                    (AudioAsset.id.is_(None))
                    & (PracticeSession.created_at <= now() - timedelta(hours=24))
                )
            )
        ).all()
    count = 0
    for sid, owner in rows:
        try:
            count += delete_practice(database, settings, owner, sid)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            # A concurrent API request or scheduler already removed this session.
            count += 1
    with database.sessions.begin() as db:
        write_lock(db)
        db.execute(delete(AuthSession).where(AuthSession.expires_at <= now()))
        for account in db.scalars(select(User).where(User.deleted_at.is_not(None))):
            remaining = db.scalar(
                select(PracticeSession.id).where(PracticeSession.owner_user_id == account.id)
            )
            if not remaining:
                db.execute(delete(Consent).where(Consent.owner_user_id == account.id))
                db.delete(account)
        # Remove crash-orphaned server-generated files only after a one-hour grace period.
        keys = set(db.scalars(select(AudioAsset.storage_key)))
    for path in settings.storage_root.glob("*.wav"):
        try:
            if path.name not in keys and path.stat().st_mtime < time.time() - 3600:
                private_path(settings.storage_root, path.name).unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
    return count


def main():
    parser = argparse.ArgumentParser(description="Local FAKE worker or retention scheduler")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings.database_url)
    while True:
        if args.cleanup:
            cleanup(database, settings)
        else:
            run_once(database, settings)
        if args.once:
            return
        time.sleep(60 if args.cleanup else 1)


if __name__ == "__main__":
    main()
