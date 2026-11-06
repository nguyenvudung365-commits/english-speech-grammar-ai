"""Real PostgreSQL integration; synthetic audio and explicitly fake adapters only."""

import io
import uuid
import wave
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api.config import Settings
from src.api.db import Database
from src.api.main import create_app
from src.api.models import AudioAsset, Job, User
from src.worker.runner import claim_job, cleanup, complete_job, expire_leases, run_once

pytestmark = pytest.mark.postgres


@pytest.fixture
def private_db():
    import os

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required: real disposable PostgreSQL only")
    database = Database(url)
    # A private schema for each test; do not drop any pre-existing schema/database.
    schema = "test_" + uuid.uuid4().hex
    with database.engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    isolated = None
    try:
        isolated = Database(url, schema=schema)
        yield isolated
    finally:
        if isolated:
            isolated.engine.dispose()
        try:
            with database.engine.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            database.engine.dispose()


@pytest.fixture
def app(tmp_path, private_db):
    settings = Settings(database_url=str(private_db.engine.url), storage_root=tmp_path / "private", origin="http://testserver")
    from src.api.models import Base
    Base.metadata.create_all(private_db.engine)
    yield create_app(settings, private_db)


def login(client, email="learner@example.com"):
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    assert (
        client.post(
            "/api/auth/register", json={"email": email, "password": "correct horse battery staple"}
        ).status_code
        == 201
    )
    response = client.post(
        "/api/auth/login", json={"email": email, "password": "correct horse battery staple"}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def session(client, mode="grammar_only"):
    consent = client.post(
        "/api/consents", json={"policy_version": "local-v1", "allow_server_processing": True}
    ).json()["id"]
    payload = {"consent_id": consent, "mode": mode}
    if mode == "reference_reading":
        payload["reference_text"] = "She goes home."
    response = client.post("/api/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def draft(client, sid, text="He go home.", revision=0):
    return client.put(
        f"/api/sessions/{sid}/transcript", json={"text": text, "expected_revision": revision}
    )


def grammar(client, sid, revision=1, key=None):
    return client.post(
        f"/api/sessions/{sid}/jobs/grammar",
        json={"expected_revision": revision, "idempotency_key": key or str(uuid.uuid4())},
    )


def wav_bytes(frames=1600, silent=False):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes((b"\x00\x00" if silent else b"\xe8\x03") * frames)
    return buffer.getvalue()


def upload(client, sid, audio=None):
    return client.post(
        f"/api/sessions/{sid}/audio",
        content=audio if audio is not None else wav_bytes(),
        headers={"Content-Type": "audio/wav", "Idempotency-Key": str(uuid.uuid4())},
    )


def test_auth_csrf_password_logout_ownership(app):
    with TestClient(app) as a, TestClient(app) as b:
        assert a.get("/api/sessions").status_code == 401
        assert (
            a.post(
                "/api/auth/register", json={"email": "a@example.com", "password": "123456789012"}
            ).status_code
            == 403
        )
        login(a)
        sid = session(a)
        login(b, "other@example.com")
        for path in [
            f"/api/sessions/{sid}",
            f"/api/sessions/{sid}/report",
            f"/api/sessions/{sid}/export",
            f"/api/sessions/{sid}/audio",
        ]:
            assert b.get(path).status_code == 404
        assert b.delete(f"/api/sessions/{sid}").status_code == 404
        assert draft(b, sid).status_code == 404
        assert grammar(b, sid).status_code == 404
        with app.state.db.sessions() as db:
            user = db.scalar(select(User).where(User.email == "learner@example.com"))
            assert user.credential.startswith("$argon2id$")
        cookie = a.cookies.get("session")
        assert a.post("/api/auth/logout").status_code == 200
        a.cookies.set("session", cookie)
        assert a.get("/api/sessions").status_code == 401


def test_revision_confirmation_snapshot_idempotency_fake_output(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        assert draft(client, sid).status_code == 200
        assert grammar(client, sid).status_code == 409
        assert (
            client.post(f"/api/sessions/{sid}/confirm", json={"expected_revision": 0}).status_code
            == 409
        )
        assert (
            client.post(f"/api/sessions/{sid}/confirm", json={"expected_revision": 1}).status_code
            == 200
        )
        key = str(uuid.uuid4())
        job = grammar(client, sid, key=key).json()
        assert grammar(client, sid, key=key).json()["id"] == job["id"]
        assert draft(client, sid, "She do not like coffee.", 1).status_code == 200
        assert draft(client, sid, "stale", 1).status_code == 409
        assert grammar(client, sid, 1).status_code == 409
        assert run_once(app.state.db, app.state.settings)
        report = client.get(f"/api/sessions/{sid}/report").json()
        result = report["grammar"][0]
        assert result["source_text"] == "He go home."
        assert result["corrected_text"] == "He goes home."
        assert result["is_current"] is False
        assert report["report_complete"] is False
        assert report["fake"] is True
        assert "FAKE" in report["warning"]
        assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "succeeded"
        exported = client.get(f"/api/sessions/{sid}/export").text
        for private in ["He go", "learner@", "storage_key", "consent_id", sid]:
            assert private not in exported


@pytest.mark.parametrize("frames,status", [(479999, 201), (480000, 201), (480001, 422)])
def test_audio_frame_boundary(app, frames, status):
    with TestClient(app) as client:
        login(client)
        sid = session(client, "reference_reading")
        assert upload(client, sid, wav_bytes(frames)).status_code == status
        assert len(list(app.state.settings.storage_root.glob("*.wav"))) == (
            1 if status == 201 else 0
        )


def test_audio_invalid_size_consent_private_delete(app):
    with TestClient(app) as client:
        login(client)
        assert upload(client, str(uuid.uuid4())).status_code == 404
        assert (
            client.post(
                "/api/consents",
                json={"policy_version": "local-v1", "allow_server_processing": False},
            ).status_code
            == 422
        )
        sid = session(client)
        assert upload(client, sid, b"not wav").status_code == 415
        assert upload(client, sid, wav_bytes()[:-1]).status_code == 415
        assert upload(client, sid, b"x" * (10485760 + 1)).status_code == 413
        assert upload(client, sid).status_code == 201
        assert client.get(f"/api/sessions/{sid}/audio").status_code == 200
        assert client.get("/private").status_code == 404
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert not list(app.state.settings.storage_root.glob("*.wav"))
        assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_speech_fake_asr_preserves_manual_draft_silence(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client, "reference_reading")
        assert upload(client, sid).status_code == 201
        assert draft(client, sid, "My own words.").status_code == 200
        run_once(app.state.db, app.state.settings)
        value = client.get(f"/api/sessions/{sid}").json()
        assert value["asr_draft"] == "He go home."
        assert value["draft_transcript"] == "My own words."
        report = client.get(f"/api/sessions/{sid}/report").json()
        assert report["pronunciation"][0]["score"] is None
        silent = session(client, "reference_reading")
        jid = upload(client, silent, wav_bytes(silent=True)).json()["job_id"]
        run_once(app.state.db, app.state.settings)
        job = client.get(f"/api/jobs/{jid}").json()
        assert job["status"] == "failed"
        assert job["error_code"] == "INSUFFICIENT_SIGNAL"


def test_lease_late_write_and_retention(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        upload(client, sid)
        claimed = claim_job(app.state.db, "worker-a")
        with app.state.db.sessions.begin() as db:
            job = db.get(Job, claimed)
            job.lease_expires_at -= timedelta(hours=1)
        assert expire_leases(app.state.db) == 1
        assert complete_job(app.state.db, app.state.settings, claimed, "worker-a") is False
        assert client.get(f"/api/jobs/{claimed}").json()["error_code"] == "WORKER_LOST"
        with app.state.db.sessions.begin() as db:
            asset = db.scalar(select(AudioAsset).where(AudioAsset.session_id == sid))
            asset.expires_at -= timedelta(days=2)
            asset.created_at -= timedelta(days=2)
        cleanup(app.state.db, app.state.settings)
        assert client.get(f"/api/sessions/{sid}").status_code == 404
        assert not list(app.state.settings.storage_root.glob("*.wav"))


def test_origin_and_csrf_on_authenticated_mutation(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        assert (
            client.delete(
                f"/api/sessions/{sid}", headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        del client.headers["X-CSRF-Token"]
        assert client.delete(f"/api/sessions/{sid}").status_code == 403


def test_local_ui_accessible_shell(app):
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "FAKE" in response.text
        assert "<main" in response.text
        assert 'role="status"' in response.text
        assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
        assert client.get("/static/app.js").status_code == 200


def test_expired_auth_wrong_password_and_account_deletion(app):
    from src.api.models import AuthSession, Consent
    from src.api.security import token_hash

    with TestClient(app) as client:
        login(client)
        sid = session(client)
        upload(client, sid)
        assert (
            client.post(
                "/api/auth/login",
                json={"email": "learner@example.com", "password": "wrong password here"},
            ).status_code
            == 401
        )
        assert client.delete("/api/account").json()["status"] == "deleted"
        assert client.get("/api/sessions").status_code == 401
        assert not list(app.state.settings.storage_root.glob("*.wav"))
        with app.state.db.sessions() as db:
            assert not list(db.scalars(select(User)))
            assert not list(db.scalars(select(Consent)))
        login(client, "new@example.com")
        with app.state.db.sessions.begin() as db:
            auth = db.get(AuthSession, token_hash(client.cookies.get("session")))
            auth.expires_at -= timedelta(days=1)
        assert client.get("/api/me").status_code == 401


def test_capacity_and_cross_owner_idempotency(app):
    app.state.settings.queue_per_user = 1
    with TestClient(app) as a, TestClient(app) as b:
        login(a)
        sid = session(a)
        draft(a, sid)
        a.post(f"/api/sessions/{sid}/confirm", json={"expected_revision": 1})
        key = str(uuid.uuid4())
        jid = grammar(a, sid, key=key).json()["id"]
        assert grammar(a, sid).status_code == 429
        assert grammar(a, sid, key=key).json()["id"] == jid
        login(b, "b@example.com")
        other = session(b)
        draft(b, other)
        b.post(f"/api/sessions/{other}/confirm", json={"expected_revision": 1})
        assert grammar(b, other, key=key).status_code == 404
        assert b.get(f"/api/jobs/{jid}").status_code == 404
        assert grammar(b, other).status_code == 201


def test_partial_independent_stages_and_negation(app):
    from src.worker.fakes import FakeAdapters

    with TestClient(app) as client:
        login(client)
        sid = session(client, "reference_reading")
        jid = upload(client, sid).json()["job_id"]
        run_once(app.state.db, app.state.settings, FakeAdapters(fail_pronunciation=True))
        value = client.get(f"/api/jobs/{jid}").json()
        assert value["status"] == "partial"
        assert value["stages"] == {
            "asr": "succeeded",
            "pronunciation": "failed",
            "grammar": "not_requested",
        }
        draft(client, sid, "She do not like coffee.", 1)
        client.post(f"/api/sessions/{sid}/confirm", json={"expected_revision": 2})
        grammar(client, sid, 2)
        run_once(app.state.db, app.state.settings)
        result = client.get(f"/api/sessions/{sid}/report").json()["grammar"][0]
        assert result["corrected_text"] == "She does not like coffee."
        assert result["edits"][0]["start"] == 4


def test_concurrent_workers_and_revision_requests(app):
    from concurrent.futures import ThreadPoolExecutor

    with TestClient(app) as client:
        login(client)
        sid = session(client)
        upload(client, sid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(
                pool.map(lambda name: claim_job(app.state.db, name), ["worker1", "worker2"])
            )
        assert sum(value is not None for value in claims) == 1
        cookies = dict(client.cookies)
        headers = dict(client.headers)

        def edit(text):
            with TestClient(app) as other:
                other.cookies.update(cookies)
                other.headers.update(headers)
                return draft(other, sid, text).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(edit, ["First draft", "Second draft"]))
        assert sorted(statuses) == [200, 409]


def test_expired_browser_session_can_login_again(app):
    from src.api.models import AuthSession
    from src.api.security import token_hash

    with TestClient(app) as client:
        login(client)
        with app.state.db.sessions.begin() as db:
            db.get(AuthSession, token_hash(client.cookies.get("session"))).expires_at -= timedelta(
                days=1
            )
        # Bootstrap recovers from expired HttpOnly session; no manual browser-cookie clearing.
        client.headers["X-CSRF-Token"] = client.get("/api/auth/csrf").json()["csrf_token"]
        result = client.post(
            "/api/auth/login",
            json={"email": "learner@example.com", "password": "correct horse battery staple"},
        )
        assert result.status_code == 200


def test_expired_content_unavailable_before_scheduler(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        draft(client, sid)
        with app.state.db.sessions.begin() as db:
            from src.api.models import PracticeSession

            db.get(PracticeSession, sid).created_at -= timedelta(days=2)
        assert client.get(f"/api/sessions/{sid}").status_code == 404
        assert client.get(f"/api/sessions/{sid}/report").status_code == 404
        assert client.get("/api/sessions").json() == []
        assert draft(client, sid, "new", 1).status_code == 404
        assert client.delete(f"/api/sessions/{sid}").status_code == 200


def test_no_audio_bytes_read_before_ownership_and_consent(app):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    with TestClient(app) as client:
        login(client)
        cookies, headers = dict(client.cookies), dict(client.headers)
    received = 0

    async def payload():
        nonlocal received
        received += 1
        yield wav_bytes()

    async def attempt():
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            cookies=cookies,
            headers=headers,
        ) as client:
            result = await client.post(f"/api/sessions/{uuid.uuid4()}/audio", content=payload())
            assert result.status_code == 404

    asyncio.run(attempt())
    assert received == 0


def test_db_commit_failure_compensates_file(app):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        def fail_commit(_db):
            raise SQLAlchemyError("injected test transaction failure")
        event.listen(app.state.db.sessions.class_, "before_commit", fail_commit)
        try:
            assert upload(client, sid).status_code == 503
        finally:
            event.remove(app.state.db.sessions.class_, "before_commit", fail_commit)
        with app.state.db.sessions() as db:
            assert not list(db.scalars(select(AudioAsset)))
            assert not list(db.scalars(select(Job)))
        assert not list(app.state.settings.storage_root.glob("*.wav"))


def test_file_delete_failure_is_pending_then_retried(app, monkeypatch):
    from pathlib import Path
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        upload(client, sid)
        original = Path.unlink
        def fail_wav(path, *args, **kwargs):
            if path.suffix == ".wav":
                raise PermissionError("synthetic file lock")
            return original(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_wav)
            response = client.delete(f"/api/sessions/{sid}")
            assert response.status_code == 202
            assert response.json()["status"] == "deletion_pending"
            assert client.get(f"/api/sessions/{sid}/audio").status_code == 404
        cleanup(app.state.db, app.state.settings)
        assert not list(app.state.settings.storage_root.glob("*.wav"))
        with app.state.db.sessions() as db:
            assert not list(db.scalars(select(AudioAsset)))


def test_running_deletion_cannot_publish_late_results(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        upload(client, sid)
        jid = claim_job(app.state.db, "worker")
        assert client.delete(f"/api/sessions/{sid}").status_code == 202
        assert complete_job(app.state.db, app.state.settings, jid, "worker")
        cleanup(app.state.db, app.state.settings)
        assert client.get(f"/api/sessions/{sid}/report").status_code == 404


def test_invalid_grammar_adapter_output_fails_terminally(app):
    from src.worker.fakes import FakeAdapters
    class BrokenOutput(FakeAdapters):
        def grammar(self, text):
            return {"corrected_text": "not a valid edits payload", "edits": "wrong", "warnings": []}
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        draft(client, sid)
        client.post(f"/api/sessions/{sid}/confirm", json={"expected_revision": 1})
        jid = grammar(client, sid).json()["id"]
        run_once(app.state.db, app.state.settings, BrokenOutput())
        value = client.get(f"/api/jobs/{jid}").json()
        assert value["status"] == "failed"
        assert value["error_code"] == "INVALID_ADAPTER_OUTPUT"


@pytest.mark.parametrize("size", [10485759, 10485760, 10485761])
def test_size_layer_exact_boundary(size):
    """Byte-gate unit check only: odd-byte payloads are not claimed valid WAV E2E fixtures."""
    from fastapi import HTTPException

    from src.api.storage import validate_wav
    with pytest.raises(HTTPException) as error:
        validate_wav(b"x" * size)
    assert error.value.status_code == (413 if size > 10485760 else 415)


def test_real_wav_at_exact_maximum_bytes(app):
    import struct
    raw = wav_bytes()
    extra = 10485760 - len(raw) - 8
    # Legal RIFF JUNK chunk before fmt/data, even-sized with exact whole-file limit.
    audio = raw[:4] + struct.pack("<I", 10485760 - 8) + raw[8:12] + b"JUNK" + struct.pack("<I", extra) + b"\x00" * extra + raw[12:]
    assert len(audio) == 10485760
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        assert upload(client, sid, audio).status_code == 201


def test_constraint_rejects_null_text_with_confirmation_timestamp(app):
    from sqlalchemy.exc import IntegrityError

    from src.api.models import PracticeSession, now
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        with pytest.raises(IntegrityError), app.state.db.sessions.begin() as db:
            db.get(PracticeSession, sid).transcript_confirmed_at = now()


def test_cleanup_concurrent_delete_is_idempotent(app, monkeypatch):
    from src.api.models import PracticeSession
    from src.worker import runner
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        with app.state.db.sessions.begin() as db:
            db.get(PracticeSession, sid).created_at -= timedelta(days=2)
        actual = runner.delete_practice
        def raced(database, settings, owner, session_id):
            actual(database, settings, owner, session_id)
            return actual(database, settings, owner, session_id)
        monkeypatch.setattr(runner, "delete_practice", raced)
        cleanup(app.state.db, app.state.settings)


def test_csrf_database_failure_sanitized_and_has_headers(app, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    import src.api.main as main_module
    with TestClient(app) as client:
        login(client)
        def broken(_request):
            raise SQLAlchemyError("sensitive internal DB error")
        monkeypatch.setattr(main_module, "check_csrf", broken)
        response = client.post("/api/auth/logout")
        assert response.status_code == 503
        assert "sensitive" not in response.text
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_alembic_upgrade_downgrade_upgrade_in_private_schema(private_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    config = Config("alembic.ini")
    with private_db.engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        tables = set(inspect(connection).get_table_names())
        assert len(tables) == 9  # 7 business + auth_sessions + alembic_version
        assert {"users", "analysis_jobs", "grammar_results", "alembic_version"} <= tables
        command.check(config)
        connection.commit()
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        connection.commit()
        command.upgrade(config, "head")
        assert "audio_assets" in inspect(connection).get_table_names()
        connection.commit()


def test_account_delete_interruption_is_durably_tombstoned(app, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from src.api import main as main_module
    from src.api.models import PracticeSession

    with TestClient(app) as client:
        login(client)
        sid = session(client)

        def interrupted(*_args):
            raise SQLAlchemyError("simulated interruption after account tombstone")

        monkeypatch.setattr(main_module, "delete_practice", interrupted)
        assert client.delete("/api/account").status_code == 503
        with app.state.db.sessions() as db:
            assert db.get(PracticeSession, sid).deletion_requested_at is not None
        cleanup(app.state.db, app.state.settings)
        with app.state.db.sessions() as db:
            assert db.get(PracticeSession, sid) is None


def test_cleanup_tolerates_orphan_disappearing_before_stat(app, monkeypatch):
    from pathlib import Path

    app.state.settings.storage_root.mkdir(parents=True)
    orphan = app.state.settings.storage_root / ("a" * 32 + ".wav")
    orphan.write_bytes(b"synthetic orphan")
    original = Path.stat

    def disappeared(path, *args, **kwargs):
        if path == orphan:
            raise FileNotFoundError("concurrent cleanup")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", disappeared)
    cleanup(app.state.db, app.state.settings)
