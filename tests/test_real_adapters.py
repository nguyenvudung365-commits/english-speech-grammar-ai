"""RealAdapters: stubbed model (no download) plus one genuine faster-whisper run."""

import io
import sys
import types
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.config import Settings
from src.worker.contracts import GrammarOutput
from src.worker.fakes import AdapterFailure, FakeAdapters

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "asr" / "base.en"
GRAMMAR = ROOT / "models" / "grammar" / "gec-t5_small"
REAL_KW = {
    "adapter_backend": "real", "asr_model": str(MODEL), "asr_revision": "r",
    "grammar_model": str(GRAMMAR), "grammar_revision": "g", "_env_file": None,
}
SAMPLE = ROOT / "out" / "asr-check" / "sample.wav"


def wav_bytes(frames=16000, value=1000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(value.to_bytes(2, "little", signed=True) * frames)
    return buffer.getvalue()


@pytest.fixture
def stub_model(monkeypatch):
    """Install a fake faster_whisper module; yields a recorder of constructions/transcribes."""
    rec = SimpleNamespace(loads=[], calls=[], texts=[" hello ", "world "])

    class WhisperModel:
        def __init__(self, path, **kwargs):
            rec.loads.append((path, kwargs))

        def transcribe(self, samples, **kwargs):
            rec.calls.append((samples, kwargs))
            return iter(SimpleNamespace(text=t) for t in rec.texts), None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return rec


def make(model="some/dir"):
    from src.worker.real_adapters import RealAdapters

    return RealAdapters(Settings(**(REAL_KW | {"asr_model": model})))


def test_asr_joins_segments_and_feeds_float32_16k(stub_model):
    adapters = make()
    assert adapters.asr(wav_bytes(frames=8, value=16384)) == "hello world"
    samples, kwargs = stub_model.calls[0]
    assert samples.dtype.name == "float32" and len(samples) == 8
    assert float(samples[0]) == 0.5
    assert kwargs["language"] == "en"
    assert stub_model.loads[0][0] == "some/dir"


@pytest.mark.parametrize("texts", [[], ["  "], ["", " "]])
def test_asr_empty_result_is_no_speech(stub_model, texts):
    stub_model.texts = texts
    with pytest.raises(AdapterFailure) as error:
        make().asr(wav_bytes())
    assert error.value.code == "NO_SPEECH"


def test_pronunciation_still_fake_grammar_is_real(stub_model):
    adapters = make()
    audio = wav_bytes()
    assert adapters.pronunciation(audio, "ref") == FakeAdapters().pronunciation(audio, "ref")
    assert adapters.pronunciation(audio, "ref")["score"] is None
    assert adapters.grammar("He go home.") != FakeAdapters().grammar("He go home.")


@pytest.mark.parametrize("missing", ["asr_model", "asr_revision", "grammar_model", "grammar_revision"])
@pytest.mark.parametrize("value", ["", "  "])
def test_real_settings_missing_any_variable_is_error(missing, value):
    with pytest.raises(ValueError, match=missing.upper()):
        Settings(**(REAL_KW | {missing: value}))
    Settings(**(REAL_KW | {"adapter_backend": "fake", missing: value}))  # fake needs none


def test_grammar_input_too_long(stub_model):
    with pytest.raises(AdapterFailure) as error:
        make().grammar("He go to school every day. " * 40)
    assert error.value.code == "INPUT_TOO_LONG"


def test_negation_removed_by_model_is_blocked_by_validate_source(stub_model):
    adapters = make()
    adapters.gec = SimpleNamespace(generate=lambda **k: [[0]])
    for source in ("She does not like apples.", "I never go there.", "There is no milk."):
        adapters.tokenizer = _Tok(source.replace(" not", "").replace("never ", "").replace("no ", ""))
        out = adapters.grammar(source)
        with pytest.raises(ValueError, match="Negation"):
            GrammarOutput.model_validate(out).validate_source(source)


class _Tok:
    def __init__(self, decoded):
        self.decoded = decoded

    def __call__(self, text, return_tensors=None):
        class Enc(dict):
            input_ids = SimpleNamespace(shape=(1, 5))

        return Enc()

    def decode(self, ids, **k):
        return self.decoded


def test_model_loaded_once_in_main_not_per_job(monkeypatch):
    from src.worker import real_adapters, runner

    built, seen = [], []

    class Counting:
        def __init__(self, settings):
            built.append(settings)

    def fake_run_once(database, settings, adapters):
        seen.append(adapters)
        if len(seen) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(real_adapters, "RealAdapters", Counting)
    monkeypatch.setattr(runner, "run_once", fake_run_once)
    monkeypatch.setattr(runner, "Database", lambda url: object())
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["worker"])
    monkeypatch.setenv("ADAPTER_BACKEND", "real")
    for k, v in (("ASR_MODEL", "x"), ("ASR_REVISION", "r"), ("GRAMMAR_MODEL", "y"), ("GRAMMAR_REVISION", "g")):
        monkeypatch.setenv(k, v)
    with pytest.raises(KeyboardInterrupt):
        runner.main()
    assert len(built) == 1
    assert len(seen) == 3 and seen[0] is seen[1] is seen[2]


def test_main_fake_backend_uses_fake_adapters_and_cleanup_loads_nothing(monkeypatch):
    from src.worker import real_adapters, runner

    seen = []

    def boom(settings):
        raise AssertionError("RealAdapters must not be built")

    monkeypatch.setattr(real_adapters, "RealAdapters", boom)
    monkeypatch.setattr(runner, "run_once", lambda d, s, a: seen.append(a))
    monkeypatch.setattr(runner, "cleanup", lambda d, s: seen.append("cleanup"))
    monkeypatch.setattr(runner, "Database", lambda url: object())
    monkeypatch.setenv("ADAPTER_BACKEND", "fake")
    monkeypatch.setattr(sys, "argv", ["worker", "--once"])
    runner.main()
    assert isinstance(seen[0], FakeAdapters)
    monkeypatch.setenv("ADAPTER_BACKEND", "real")  # cleanup mode never loads a model
    for k in ("ASR_MODEL", "ASR_REVISION", "GRAMMAR_MODEL", "GRAMMAR_REVISION"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(sys, "argv", ["worker", "--cleanup", "--once"])
    runner.main()
    assert seen[1] == "cleanup"


# --- genuine model run: must not be skipped in this environment ---
try:
    import faster_whisper  # noqa: F401

    HAVE_LIB = True
except ImportError:
    HAVE_LIB = False

needs_model = pytest.mark.skipif(
    not (HAVE_LIB and (MODEL / "model.bin").exists()), reason="faster_whisper or base.en missing"
)


@needs_model
def test_real_base_en_on_sample_wav():
    from src.worker.real_adapters import RealAdapters

    adapters = RealAdapters(Settings(**REAL_KW))
    audio = SAMPLE.read_bytes() if SAMPLE.exists() else wav_bytes(frames=16000)
    if not SAMPLE.exists():
        pytest.skip("sample.wav missing")
    text = adapters.asr(audio)
    assert isinstance(text, str) and text.strip() and len(text) <= 3000
    # Silence has no speech: VAD + empty guard must surface NO_SPEECH, not a hallucination.
    with pytest.raises(AdapterFailure) as error:
        adapters.asr(wav_bytes(frames=32000, value=0))
    assert error.value.code == "NO_SPEECH"


HAVE_GRAMMAR = (GRAMMAR / "pytorch_model.bin").exists()


@pytest.fixture(scope="module")
def real_grammar():
    from src.worker.real_adapters import RealAdapters

    return RealAdapters(Settings(**REAL_KW))


def run_grammar(adapters, text):
    return GrammarOutput.model_validate(adapters.grammar(text)).validate_source(text)


@pytest.mark.skipif(not (HAVE_LIB and HAVE_GRAMMAR), reason="grammar model missing")
@pytest.mark.parametrize(
    "text,expected",
    [("He go to school every day.", "He goes to school every day."),
     ("She don't like apples.", "She doesn't like apples.")],
)
def test_real_grammar_corrects_and_passes_validate_source(real_grammar, text, expected):
    out = run_grammar(real_grammar, text)
    assert out["corrected_text"] == expected and out["edits"]


@pytest.mark.skipif(not (HAVE_LIB and HAVE_GRAMMAR), reason="grammar model missing")
@pytest.mark.parametrize("text", ["He goes to school every day.", "She does not like apples."])
def test_real_grammar_correct_sentence_has_no_edits(real_grammar, text):
    out = run_grammar(real_grammar, text)
    assert out["corrected_text"] == text and out["edits"] == []
