"""Local-only HTTP application. Run worker and cleanup as separate processes."""

import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from src.api.config import FAKE_WARNING, MAX_AUDIO_BYTES, Settings
from src.api.db import Database
from src.api.models import AudioAsset, AuthSession, Consent, Job, PracticeSession, User, now
from src.api.schemas import (
    ConsentInput,
    Credentials,
    GrammarInput,
    RevisionInput,
    SessionInput,
    TranscriptInput,
)
from src.api.security import check_csrf, current_user, hasher, login_session, set_cookie, token_hash
from src.api.services import (
    active_consent,
    delete_practice,
    enqueue,
    job_view,
    owned,
    report,
    save_audio,
    session_view,
    write_lock,
)
from src.api.storage import private_path, validate_wav

CurrentUser = Annotated[str, Depends(current_user)]


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = MAX_AUDIO_BYTES if scope["path"].endswith("/audio") else 24000
        count = 0

        async def limited_receive():
            nonlocal count
            message = await receive()
            if message["type"] == "http.request":
                count += len(message.get("body", b""))
                if count > limit:
                    raise HTTPException(413, "Request body too large")
            return message

        await self.app(scope, limited_receive, send)


class LocalRateLimit:
    """Bounded per-process guard only; distributed/proxy rate limiting is deferred."""

    def __init__(self):
        self.events = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str, limit: int):
        stamp = time.monotonic()
        with self.lock:
            for old_key in list(self.events):
                if not self.events[old_key] or self.events[old_key][-1] <= stamp - 60:
                    del self.events[old_key]
            if key not in self.events and len(self.events) >= 2048:
                raise HTTPException(429, "Local rate limiter full")
            events = self.events[key]
            while events and events[0] <= stamp - 60:
                events.popleft()
            if len(events) >= limit:
                raise HTTPException(
                    429, "Rate limit; retry in one minute", headers={"Retry-After": "60"}
                )
            events.append(stamp)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or Settings()
    database = database or Database(settings.database_url)
    app = FastAPI(title="Local Speech & Grammar — FAKE adapters", version="0.1.0")
    app.state.settings, app.state.db = settings, database
    limiter = LocalRateLimit()
    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        try:
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                if request.headers.get("origin") not in (None, settings.origin):
                    raise HTTPException(403, "Origin rejected")
                await run_in_threadpool(check_csrf, request)
                host = request.client.host if request.client else "unknown"
                if request.url.path.startswith("/api/auth/"):
                    limiter.check("auth:" + host, settings.auth_rate_per_minute)
                else:
                    limiter.check(
                        "mutation:" + token_hash(request.cookies.get("session", host)), 120
                    )
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, exc.status_code, headers=exc.headers)
        except SQLAlchemyError:
            response = JSONResponse({"detail": "Database operation failed"}, 503)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(SQLAlchemyError)
    async def db_error(_request, _exc):
        return JSONResponse({"detail": "Database operation failed"}, 503)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "fake": True}

    @app.get("/readyz")
    def ready():
        try:
            with database.engine.connect() as connection:
                connection.execute(text("SELECT version_num FROM alembic_version"))
        except SQLAlchemyError:
            raise HTTPException(503, "Database or migration not ready") from None
        return {"status": "ready", "worker": "not_probed", "fake": True}

    @app.get("/api/auth/csrf")
    def csrf(request: Request, response: Response):
        token = request.cookies.get("csrf") or secrets.token_urlsafe(32)
        raw = request.cookies.get("session")
        if raw:
            with database.sessions.begin() as db:
                auth = db.get(AuthSession, token_hash(raw))
                if not auth or auth.expires_at <= now():
                    response.delete_cookie("session")
                    token = secrets.token_urlsafe(32)
                elif not secrets.compare_digest(auth.csrf_hash, token_hash(token)):
                    token = secrets.token_urlsafe(32)
                    auth.csrf_hash = token_hash(token)
        set_cookie(response, request, "csrf", token, settings.session_hours * 3600, False)
        return {"csrf_token": token}

    @app.post("/api/auth/register", status_code=201)
    def register(payload: Credentials):
        try:
            with database.sessions.begin() as db:
                db.add(
                    User(email=str(payload.email).lower(), credential=hasher.hash(payload.password))
                )
        except IntegrityError:
            raise HTTPException(409, "Registration unavailable") from None
        return {"message": "Account created"}

    @app.post("/api/auth/login")
    def login(payload: Credentials, request: Request, response: Response):
        return login_session(request, response, str(payload.email), payload.password)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user: CurrentUser):
        with database.sessions.begin() as db:
            db.execute(
                delete(AuthSession).where(
                    AuthSession.token_hash == token_hash(request.cookies.get("session", ""))
                )
            )
        response.delete_cookie("session")
        response.delete_cookie("csrf")
        return {"message": "Logged out"}

    @app.get("/api/me")
    def me(user: CurrentUser):
        return {"id": user, "fake": True, "warning": FAKE_WARNING}

    @app.post("/api/consents", status_code=201)
    def consent(payload: ConsentInput, user: CurrentUser):
        with database.sessions.begin() as db:
            value = Consent(owner_user_id=user, **payload.model_dump())
            db.add(value)
            db.flush()
            return {
                "id": value.id,
                "policy_version": value.policy_version,
                "accepted_at": value.accepted_at,
            }

    @app.post("/api/sessions", status_code=201)
    def create_session(payload: SessionInput, user: CurrentUser):
        with database.sessions.begin() as db:
            write_lock(db)
            consent = db.get(Consent, str(payload.consent_id))
            if not consent or consent.owner_user_id != user:
                raise HTTPException(404, "Not found")
            if consent.withdrawn_at:
                raise HTTPException(409, "Consent withdrawn")
            timestamp = now()
            value = PracticeSession(
                owner_user_id=user,
                consent_id=consent.id,
                mode=payload.mode,
                reference_text=payload.reference_text,
                created_at=timestamp,
                updated_at=timestamp,
            )
            db.add(value)
            db.flush()
            return session_view(value)

    @app.get("/api/sessions")
    def history(user: CurrentUser, limit: int = 30):
        with database.sessions() as db:
            from src.api.services import unexpired_session

            values = db.scalars(
                select(PracticeSession)
                .outerjoin(AudioAsset)
                .where(
                    PracticeSession.owner_user_id == user,
                    PracticeSession.deletion_requested_at.is_(None),
                    unexpired_session(),
                )
                .order_by(PracticeSession.created_at.desc(), PracticeSession.id)
                .limit(max(1, min(100, limit)))
            )
            return [
                {
                    "id": value.id,
                    "created_at": value.created_at,
                    "mode": value.mode,
                    "transcript_revision": value.transcript_revision,
                }
                for value in values
            ]

    @app.get("/api/sessions/{sid}")
    def get_session(sid: UUID, user: CurrentUser):
        with database.sessions() as db:
            return session_view(owned(db, user, str(sid)))

    @app.put("/api/sessions/{sid}/transcript")
    def transcript(sid: UUID, payload: TranscriptInput, user: CurrentUser):
        with database.sessions.begin() as db:
            write_lock(db)
            value = owned(db, user, str(sid), lock=True)
            active_consent(db, value)
            if payload.expected_revision != value.transcript_revision:
                raise HTTPException(409, "Stale transcript revision; reload")
            if value.draft_transcript != payload.text:
                value.draft_transcript = payload.text
                value.transcript_revision += 1
                value.confirmed_transcript = value.transcript_confirmed_at = None
                value.updated_at = now()
            return session_view(value)

    @app.post("/api/sessions/{sid}/confirm")
    def confirm(sid: UUID, payload: RevisionInput, user: CurrentUser):
        with database.sessions.begin() as db:
            write_lock(db)
            value = owned(db, user, str(sid), lock=True)
            active_consent(db, value)
            if payload.expected_revision != value.transcript_revision or not value.draft_transcript:
                raise HTTPException(409, "Draft missing or revision stale")
            value.confirmed_transcript = value.draft_transcript
            value.transcript_confirmed_at = value.updated_at = now()
            return session_view(value)

    @app.post("/api/sessions/{sid}/jobs/grammar", status_code=201)
    def grammar(sid: UUID, payload: GrammarInput, user: CurrentUser):
        with database.sessions.begin() as db:
            write_lock(db)
            value = owned(db, user, str(sid), lock=True)
            if (
                value.transcript_revision != payload.expected_revision
                or not value.confirmed_transcript
            ):
                raise HTTPException(409, "Confirm the current revision before analysis")
            job = enqueue(
                db,
                settings,
                value,
                "grammar",
                str(payload.idempotency_key),
                {"text": value.confirmed_transcript, "revision": value.transcript_revision},
            )
            return job_view(job)

    @app.post("/api/sessions/{sid}/audio", status_code=201)
    async def audio_upload(sid: UUID, request: Request, user: CurrentUser):
        # Raw WAV protocol avoids multipart parser reading audio before authorization/consent.
        def authorize_upload():
            with database.sessions() as db:
                value = owned(db, user, str(sid))
                active_consent(db, value)

        await run_in_threadpool(authorize_upload)
        try:
            key = str(UUID(request.headers.get("Idempotency-Key", "")))
        except ValueError:
            raise HTTPException(422, "A random UUID Idempotency-Key is required") from None
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "Audio exceeds 10 MiB")
        normalized, frames = await run_in_threadpool(validate_wav, bytes(data))

        def persist():
            path = None
            try:
                with database.sessions.begin() as db:
                    write_lock(db)
                    value = owned(db, user, str(sid), lock=True)
                    asset, job, path = save_audio(db, settings, value, normalized, frames, key)
                    result = {
                        "asset_id": asset.id,
                        "job_id": job.id,
                        "expires_at": asset.expires_at,
                        "frame_count": asset.frame_count,
                        "sample_rate_hz": 16000,
                        "fake": True,
                    }
                return result
            except Exception:
                if path:
                    path.unlink(missing_ok=True)
                raise

        return await run_in_threadpool(persist)

    @app.get("/api/sessions/{sid}/audio")
    def audio_get(sid: UUID, user: CurrentUser):
        with database.sessions() as db:
            value = owned(db, user, str(sid))
            active_consent(db, value)
            asset = db.scalar(select(AudioAsset).where(AudioAsset.session_id == str(sid)))
            if not asset or asset.state != "ready" or asset.expires_at <= now():
                raise HTTPException(404, "Audio unavailable")
            path = private_path(settings.storage_root, asset.storage_key)
            if not path.is_file():
                raise HTTPException(404, "Audio unavailable")
            return FileResponse(path, media_type="audio/wav", filename="practice.wav")

    @app.get("/api/jobs/{jid}")
    def get_job(jid: UUID, user: CurrentUser):
        with database.sessions() as db:
            job = db.get(Job, str(jid))
            if not job:
                raise HTTPException(404, "Not found")
            owned(db, user, job.session_id)
            return job_view(job)

    @app.get("/api/sessions/{sid}/report")
    def session_report(sid: UUID, user: CurrentUser):
        with database.sessions() as db:
            return report(db, owned(db, user, str(sid)))

    @app.get("/api/sessions/{sid}/export")
    def export(sid: UUID, user: CurrentUser):
        with database.sessions() as db:
            value = report(db, owned(db, user, str(sid)))
            # Minimal data export: no free text, identifiers, paths, hashes or timestamps.
            return {
                "schema_version": 1,
                "fake": True,
                "warning": FAKE_WARNING,
                "privacy": "Content-free technical summary; not a guarantee of anonymization",
                "jobs": [
                    {"kind": item["kind"], "status": item["status"], "stages": item["stages"]}
                    for item in value["jobs"]
                ],
                "pronunciation_scores": [item["score"] for item in value["pronunciation"]],
                "grammar_edit_counts": [len(item["edits"]) for item in value["grammar"]],
            }

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: UUID, response: Response, user: CurrentUser):
        done = delete_practice(database, settings, user, str(sid))
        response.status_code = 200 if done else 202
        return {"status": "deleted" if done else "deletion_pending"}

    @app.delete("/api/account")
    def delete_account(response: Response, user: CurrentUser):
        with database.sessions.begin() as db:
            write_lock(db)
            account = db.get(User, user)
            account.deleted_at = now()
            db.execute(delete(AuthSession).where(AuthSession.user_id == user))
            practices = list(
                db.scalars(select(PracticeSession).where(PracticeSession.owner_user_id == user))
            )
            for practice in practices:
                practice.deletion_requested_at = practice.deletion_requested_at or account.deleted_at
            ids = [practice.id for practice in practices]
        pending = False
        for sid in ids:
            pending |= not delete_practice(database, settings, user, sid)
        if not pending:
            with database.sessions.begin() as db:
                write_lock(db)
                db.execute(delete(Consent).where(Consent.owner_user_id == user))
                db.execute(delete(User).where(User.id == user))
        response.delete_cookie("session")
        response.delete_cookie("csrf")
        response.status_code = 202 if pending else 200
        return {"status": "deletion_pending" if pending else "deleted"}

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")

        @app.get("/")
        def index():
            return FileResponse(static / "index.html")

    return app


app = create_app()
