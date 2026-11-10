"""Stages 4-5 acceptance (.claude/plans/hoan-thien-du-an.md): adapter config, provenance,
CONFIG_MISMATCH, heartbeat, adapter output checks, edits_from_correction, architecture."""

import ast
import sys
import types
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_vertical_slice import (
    draft,
    grammar,
    login,
    retry,
    session,
    upload,
    wav_bytes,
)

from src.api.config import FAKE_WARNING, PARTIAL_WARNING, REAL_WARNING, Settings
from src.api.main import create_app
from src.api.models import GrammarResult, Job, PracticeSession, PronunciationResult
from src.api.services import adapter_config, canonical_hash
from src.worker import runner
from src.worker.contracts import GrammarOutput, edits_from_correction
from src.worker.fakes import AdapterFailure, FakeAdapters
from src.worker.runner import claim_job, complete_job, run_once

SRC = Path(__file__).resolve().parents[1] / "src"


REAL_KW = {
    "adapter_backend": "real", "asr_model": "m/base.en", "asr_revision": "r",
    "grammar_model": "m/gec-t5_small", "grammar_revision": "g1", "_env_file": None,
}


@pytest.fixture
def real_app(tmp_path, private_db):
    from src.api.models import Base

    settings = Settings(
        database_url=str(private_db.engine.url),
        storage_root=tmp_path / "private",
        origin="http://testserver",
        adapter_backend="real",
        asr_model="models/asr/base.en",
        asr_revision="rev123",
        grammar_model="models/grammar/gec-t5_small",
        grammar_revision="gram456",
    )
    Base.metadata.create_all(private_db.engine)
    yield create_app(settings, private_db)


def confirmed_grammar_job(client, sid, text="He go home."):
    draft(client, sid, text)
    client.post(f"/api/sessions/{sid}/confirm", json={"expected_revision": 1})
    return grammar(client, sid).json()["id"]


# ---------- adapter_config (pure) ----------


def test_adapter_config_fake_vs_real_differ_and_are_json_pure():
    fake = adapter_config(Settings())
    real = adapter_config(Settings(**(REAL_KW | {"asr_model": "a/b/base.en", "asr_revision": "r"})))
    assert fake["backend"] == "fake-v1"
    assert real["backend"] != fake["backend"]
    assert real["asr_model"] == "base.en" and real["asr_revision"] == "r"  # no local path leaks
    assert canonical_hash(fake) != canonical_hash(real)
    assert real["grammar_model"] == "gec-t5_small" and real["grammar_revision"] == "g1"
    assert real["backend"] == "real-asr-grammar-v1"
    other = adapter_config(Settings(**(REAL_KW | {"asr_model": "x/base.en", "asr_revision": "r2"})))
    assert canonical_hash(real) != canonical_hash(other)
    other = adapter_config(Settings(**(REAL_KW | {"grammar_revision": "g2"})))
    assert canonical_hash(real) != canonical_hash(other)
    assert adapter_config(Settings()) == fake  # deterministic


# ---------- idempotency hash shared by enqueue and save_audio ----------


@pytest.mark.postgres
@pytest.mark.parametrize("fixture", ["app", "real_app"])
def test_repeated_upload_and_grammar_replay_on_both_backends(fixture, request):
    application = request.getfixturevalue(fixture)
    with TestClient(application) as client:
        login(client)
        sid = session(client, "reference_reading")
        key, audio = str(uuid.uuid4()), wav_bytes()
        headers = {"Content-Type": "audio/wav", "Idempotency-Key": key}
        first = client.post(f"/api/sessions/{sid}/audio", content=audio, headers=headers)
        again = client.post(f"/api/sessions/{sid}/audio", content=audio, headers=headers)
        assert first.status_code == again.status_code == 201
        assert first.json()["job_id"] == again.json()["job_id"]
        other = client.post(
            f"/api/sessions/{sid}/audio", content=wav_bytes(frames=3200), headers=headers
        )
        assert other.status_code == 409
        gsid = session(client)
        gkey = str(uuid.uuid4())
        draft(client, gsid)
        client.post(f"/api/sessions/{gsid}/confirm", json={"expected_revision": 1})
        a = grammar(client, gsid, key=gkey)
        b = grammar(client, gsid, key=gkey)
        assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
        with application.state.db.sessions() as db:
            assert len(list(db.scalars(select(Job)))) == 2


# ---------- provenance / fake flag from the job; /api/me ----------


@pytest.mark.postgres
@pytest.mark.parametrize("fixture,backend", [("app", "fake"), ("real_app", "real")])
def test_me_reports_backend(fixture, backend, request):
    with TestClient(request.getfixturevalue(fixture)) as client:
        login(client)
        assert client.get("/api/me").json()["backend"] == backend


@pytest.mark.postgres
def test_real_backend_job_provenance_is_partial_fake(real_app):
    with TestClient(real_app) as client:
        login(client)
        sid = session(client, "reference_reading")
        jid = upload(client, sid).json()["job_id"]
        view = client.get(f"/api/jobs/{jid}").json()
        assert view["fake"] is True  # pronunciation stage is still fake => whole job fake
        assert view["warning"] == PARTIAL_WARNING != FAKE_WARNING
        with real_app.state.db.sessions() as db:
            prov = db.get(Job, jid).provenance_json
        assert prov["backend"] == "real-asr-grammar-v1"
        assert prov["model"] == "asr:base.en@rev123;grammar:gec-t5_small@gram456"
        assert prov["fake"] is True


@pytest.mark.postgres
def test_real_backend_pure_grammar_job_is_not_fake(real_app):
    with TestClient(real_app) as client:
        login(client)
        jid = confirmed_grammar_job(client, session(client))
        view = client.get(f"/api/jobs/{jid}").json()
        assert view["fake"] is False and view["warning"] == REAL_WARNING


@pytest.mark.postgres
def test_fake_backend_provenance_unchanged(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        jid = confirmed_grammar_job(client, sid)
        view = client.get(f"/api/jobs/{jid}").json()
        assert view["fake"] is True and view["warning"] == FAKE_WARNING
        with app.state.db.sessions() as db:
            assert db.get(Job, jid).provenance_json["model"] == "no-model-test-double"


class StubReal:
    """A 'real' adapter double: valid outputs, fake=False, no model."""

    def asr(self, audio):
        return "He go home."

    def pronunciation(self, audio, reference):
        return {"fake": False, "score": None, "method": "stub"}

    def grammar(self, text):
        return FakeAdapters().grammar(text)


@pytest.mark.postgres
def test_fake_flag_and_warning_come_from_job_provenance(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client, "reference_reading")
        jid = upload(client, sid).json()["job_id"]
        with app.state.db.sessions.begin() as db:
            job = db.get(Job, jid)
            job.provenance_json = {**job.provenance_json, "fake": False, "warning": "from-job"}
        run_once(app.state.db, app.state.settings, StubReal())
        view = client.get(f"/api/jobs/{jid}").json()
        assert view["fake"] is False and view["warning"] == "from-job"
        report = client.get(f"/api/sessions/{sid}/report").json()
        assert report["fake"] is False
        assert report["pronunciation"][0]["fake"] is False
        assert report["pronunciation"][0]["score"] is None
        gsid = session(client)
        gid = confirmed_grammar_job(client, gsid)
        with app.state.db.sessions.begin() as db:
            job = db.get(Job, gid)
            job.provenance_json = {**job.provenance_json, "fake": False}
        run_once(app.state.db, app.state.settings, StubReal())
        assert client.get(f"/api/sessions/{gsid}/report").json()["grammar"][0]["fake"] is False


# ---------- pronunciation output check ----------


@pytest.mark.postgres
@pytest.mark.parametrize(
    "output,status,code",
    [
        ({"fake": True, "score": 0.9}, "partial", "INVALID_ADAPTER_OUTPUT"),
        ({"fake": False, "score": 0.0}, "partial", "INVALID_ADAPTER_OUTPUT"),  # 0 is not None
        ({"score": None}, "partial", "INVALID_ADAPTER_OUTPUT"),  # fake missing
        ({"fake": "yes", "score": None}, "partial", "INVALID_ADAPTER_OUTPUT"),  # fake not bool
        ({"fake": 1, "score": None}, "partial", "INVALID_ADAPTER_OUTPUT"),
        ("not a dict", "partial", "INVALID_ADAPTER_OUTPUT"),
        ({"fake": False, "score": None}, "succeeded", None),
        ({"fake": True, "score": None}, "succeeded", None),
    ],
)
def test_pronunciation_requires_bool_fake_and_no_score(app, output, status, code):
    class Adapter(StubReal):
        def pronunciation(self, audio, reference):
            return output

    with TestClient(app) as client:
        login(client)
        sid = session(client, "reference_reading")
        jid = upload(client, sid).json()["job_id"]
        run_once(app.state.db, app.state.settings, Adapter())
        view = client.get(f"/api/jobs/{jid}").json()
        assert view["status"] == status and view["error_code"] == code
        with app.state.db.sessions() as db:
            stored = list(db.scalars(select(PronunciationResult)))
        assert len(stored) == (status == "succeeded")
        assert all(row.score is None for row in stored)


# ---------- CONFIG_MISMATCH ----------


@pytest.mark.postgres
def test_config_mismatch_fails_job_and_retry_runs_under_new_config(app):
    settings = app.state.settings
    with TestClient(app) as client:
        login(client)
        sid = session(client, "reference_reading")
        old = upload(client, sid).json()["job_id"]
        gsid = session(client)
        gold = confirmed_grammar_job(client, gsid)
        # Operator switches backend: API and worker now run config B; both queued jobs carry A.
        settings.adapter_backend, settings.asr_model = "real", "models/asr/base.en"
        settings.grammar_model = "models/grammar/gec-t5_small"
        assert run_once(app.state.db, settings, FakeAdapters())
        assert run_once(app.state.db, settings, FakeAdapters())
        for jid in (old, gold):
            view = client.get(f"/api/jobs/{jid}").json()
            assert view["status"] == "failed" and view["error_code"] == "CONFIG_MISMATCH"
            assert set(view["stages"].values()) <= {"failed", "not_requested"}
        with app.state.db.sessions() as db:
            assert not list(db.scalars(select(GrammarResult)))
            assert not list(db.scalars(select(PronunciationResult)))
            assert db.get(PracticeSession, sid).asr_draft is None
        # Retry creates a new job stamped with the current config; it completes.
        for jid, owner_sid in ((old, sid), (gold, gsid)):
            new = retry(client, jid)
            assert new.status_code == 201, new.text
            assert new.json()["attempt_no"] == 2
        while run_once(app.state.db, settings, FakeAdapters()):
            pass
        with app.state.db.sessions() as db:
            jobs = list(db.scalars(select(Job).where(Job.attempt_no == 2)))
            assert len(jobs) == 2
            for job in jobs:
                assert job.status == "succeeded", (job.error_code, job.stage_state_json)
                assert job.config_sha256 == canonical_hash(adapter_config(settings))
            assert len(list(db.scalars(select(GrammarResult)))) == 1


@pytest.mark.postgres
def test_matching_config_is_not_flagged(real_app):
    with TestClient(real_app) as client:
        login(client)
        sid = session(client)
        jid = confirmed_grammar_job(client, sid)
        run_once(real_app.state.db, real_app.state.settings, FakeAdapters())
        assert client.get(f"/api/jobs/{jid}").json()["status"] == "succeeded"


# ---------- heartbeat ----------


@pytest.mark.postgres
def test_heartbeat_called_before_each_requested_stage(app, monkeypatch):
    calls = []
    real = runner.heartbeat
    monkeypatch.setattr(runner, "heartbeat", lambda *a: calls.append(1) or real(*a))
    with TestClient(app) as client:
        login(client)
        sid = session(client, "reference_reading")
        upload(client, sid)
        run_once(app.state.db, app.state.settings)
        assert len(calls) == 2  # asr + pronunciation
        calls.clear()
        gsid = session(client)
        confirmed_grammar_job(client, gsid)
        run_once(app.state.db, app.state.settings)
        assert len(calls) == 1  # grammar only


@pytest.mark.postgres
def test_heartbeat_extends_lease_before_slow_stage(app):
    seen = []

    class Slow(StubReal):
        def asr(self, audio):
            with app.state.db.sessions() as db:
                seen.append(db.get(Job, jid_box[0]).lease_expires_at)
            return super().asr(audio)

    jid_box = []
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        jid_box.append(upload(client, sid).json()["job_id"])
        claim_job(app.state.db, "w")
        with app.state.db.sessions.begin() as db:  # lease nearly gone but still valid
            db.get(Job, jid_box[0]).lease_expires_at = runner.now() + timedelta(seconds=5)
        complete_job(app.state.db, app.state.settings, jid_box[0], "w", Slow())
        assert seen[0] > runner.now() + timedelta(seconds=60)


@pytest.mark.postgres
def test_lost_lease_mid_job_writes_nothing(app):
    class LoseLease(StubReal):
        def asr(self, audio):
            with app.state.db.sessions.begin() as db:
                db.get(Job, jid_box[0]).lease_expires_at -= timedelta(hours=1)
            return "He go home."

    jid_box = []
    with TestClient(app) as client:
        login(client)
        sid = session(client, "reference_reading")
        jid_box.append(upload(client, sid).json()["job_id"])
        owner = "w"
        assert claim_job(app.state.db, owner) == jid_box[0]
        assert complete_job(app.state.db, app.state.settings, jid_box[0], owner, LoseLease()) is False
        with app.state.db.sessions() as db:
            assert not list(db.scalars(select(PronunciationResult)))
            assert db.get(PracticeSession, sid).asr_draft is None
            assert db.get(PracticeSession, sid).draft_transcript is None
            assert db.get(Job, jid_box[0]).status == "running"  # left for expire_leases


@pytest.mark.postgres
def test_other_worker_cannot_complete(app):
    with TestClient(app) as client:
        login(client)
        sid = session(client)
        jid = upload(client, sid).json()["job_id"]
        claim_job(app.state.db, "a")
        assert complete_job(app.state.db, app.state.settings, jid, "b") is False


# ---------- RealAdapters (stubbed model) through the worker ----------


@pytest.mark.postgres
def test_real_adapters_no_speech_fails_job_with_code(real_app, monkeypatch):
    class Model:
        def __init__(self, *a, **k): ...

        def transcribe(self, samples, **k):
            return iter([]), None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = Model
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    from src.worker.real_adapters import RealAdapters

    adapters = RealAdapters(real_app.state.settings)
    with TestClient(real_app) as client:
        login(client)
        sid = session(client, "reference_reading")
        jid = upload(client, sid).json()["job_id"]
        run_once(real_app.state.db, real_app.state.settings, adapters)
        view = client.get(f"/api/jobs/{jid}").json()
        assert view["stages"]["asr"] == "failed"
        assert view["status"] == "partial" and view["error_code"] == "NO_SPEECH"
        assert view["fake"] is True


# ---------- edits_from_correction ----------


def check(source, corrected):
    edits = edits_from_correction(source, corrected)
    out = GrammarOutput(corrected_text=corrected, edits=edits, warnings=[]).validate_source(source)
    return edits, out


def test_edits_substitution_he_goes_home():
    edits, out = check("He go home.", "He goes home.")
    assert edits and out["corrected_text"] == "He goes home."
    assert all(e["end"] > e["start"] or e["replacement"] for e in edits)


def test_edits_insertion_and_deletion():
    ins, _ = check("She likes coffee", "She really likes coffee.")
    assert any(e["start"] == e["end"] for e in ins)
    dele, _ = check("She really likes coffee.", "She likes coffee")
    assert any(e["replacement"] == "" for e in dele)


def test_edits_identical_is_empty_and_unicode_ok():
    assert check("Same text.", "Same text.")[0] == []
    check("Café déjà vu 日本語 😀 go", "Café déjà vu 日本語 😀 goes")
    check("naïve résumé", "naive resume")


def test_edits_many_changes_stay_within_cap_and_rebuild():
    source = "ab" * 150
    corrected = "ba" * 150
    edits, _ = check(source, corrected)
    assert len(edits) <= 100


def test_edits_non_overlapping_ordered():
    edits, _ = check("a cat sat on mat", "the cat sits on the mat")
    cursor = 0
    for e in edits:
        assert e["start"] >= cursor
        cursor = e["end"]


@pytest.mark.parametrize(
    "source,corrected",
    [
        ("He does not like it.", "He does like it."),
        ("I will never go.", "I will go."),
        ("There is no milk.", "There is milk."),
    ],
)
def test_dropped_negation_still_blocked(source, corrected):
    edits = edits_from_correction(source, corrected)
    model = GrammarOutput(corrected_text=corrected, edits=edits, warnings=[])
    with pytest.raises(ValueError, match="Negation"):
        model.validate_source(source)


# ---------- architecture ----------


def imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_api_never_imports_worker_or_ml_libraries():
    banned = ("src.worker", "torch", "transformers", "faster_whisper", "ctranslate2", "numpy")
    offenders = [
        (path.name, name)
        for path in (SRC / "api").glob("*.py")
        for name in imports(path)
        if name.startswith(banned)
    ]
    assert offenders == []


def test_ml_imports_are_lazy_in_worker():
    for path in (SRC / "worker").glob("*.py"):
        top = [n for n in ast.parse(path.read_text(encoding="utf-8")).body]
        for node in top:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            )
            assert not any(n.split(".")[0] in {"faster_whisper", "torch", "numpy"} for n in names), path


def test_adapter_failure_code_default():
    assert AdapterFailure().code == "FAKE_STAGE_ERROR"


# ---------- real-backend labels (not FAKE_WARNING) ----------


@pytest.mark.postgres
def test_me_labels_by_backend(app, real_app):
    with TestClient(real_app) as client:
        login(client)
        me = client.get("/api/me").json()
        assert me["fake"] is False and me["warning"] == PARTIAL_WARNING
    with TestClient(app) as client:
        login(client, "other@example.com")  # both apps share one DB
        me = client.get("/api/me").json()
        assert me["fake"] is True and me["warning"] == FAKE_WARNING


@pytest.mark.postgres
def test_real_upload_report_export_pronunciation_labels(real_app):
    with TestClient(real_app) as client:
        login(client)
        sid = session(client, "reference_reading")
        up = upload(client, sid).json()
        with real_app.state.db.sessions() as db:
            assert up["fake"] is db.get(Job, up["job_id"]).provenance_json["fake"] is True
        run_once(real_app.state.db, real_app.state.settings, StubReal())
        report = client.get(f"/api/sessions/{sid}/report").json()
        export = client.get(f"/api/sessions/{sid}/export").json()
        for body in (report, export):
            assert body["warning"] == PARTIAL_WARNING
            assert "no real ASR" not in body["warning"]
        assert report["pronunciation"], report
        assert "no real ASR" not in report["pronunciation"][0]["feedback"]["warning"]


@pytest.mark.postgres
def test_real_grammar_only_audio_job_is_not_fake(real_app):
    with TestClient(real_app) as client:
        login(client)
        sid = session(client, "grammar_only")
        up = upload(client, sid).json()
        assert up["fake"] is False
        view = client.get(f"/api/jobs/{up['job_id']}").json()
        assert view["fake"] is False and view["warning"] == REAL_WARNING
