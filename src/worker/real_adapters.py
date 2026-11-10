"""Real ASR (faster-whisper) and grammar (T5 GEC). Pronunciation stays a FAKE: no score."""

import io
import wave

from src.api.config import Settings
from src.worker.contracts import edits_from_correction
from src.worker.fakes import AdapterFailure, FakeAdapters

MAX_INPUT_TOKENS = 128  # Unbabel/gec-t5_small was trained with 128-token sequences
MAX_NEW_TOKENS = 160


class RealAdapters:
    def __init__(self, settings: Settings):
        import numpy as np
        import torch
        from faster_whisper import WhisperModel
        from transformers import AutoTokenizer, T5ForConditionalGeneration

        self.np, self.torch = np, torch
        # local_files_only: a missing/incomplete directory fails at startup, never downloads.
        self.model = WhisperModel(
            settings.asr_model, device="cpu", compute_type="int8", local_files_only=True
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            settings.grammar_model, local_files_only=True
        )
        self.gec = T5ForConditionalGeneration.from_pretrained(
            settings.grammar_model, local_files_only=True
        ).eval()
        self.fake = FakeAdapters()

    def asr(self, audio: bytes) -> str:
        with wave.open(io.BytesIO(audio), "rb") as wav:  # already validated 16 kHz mono PCM16
            frames = wav.readframes(wav.getnframes())
        samples = self.np.frombuffer(frames, dtype="<i2").astype(self.np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            samples, language="en", beam_size=1, vad_filter=True
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            raise AdapterFailure("NO_SPEECH")
        return text

    def pronunciation(self, audio: bytes, reference: str) -> dict:
        return self.fake.pronunciation(audio, reference)

    def grammar(self, text: str) -> dict:
        encoded = self.tokenizer("gec: " + text, return_tensors="pt")
        if encoded.input_ids.shape[1] > MAX_INPUT_TOKENS:
            raise AdapterFailure("INPUT_TOO_LONG")
        with self.torch.inference_mode():
            out = self.gec.generate(**encoded, num_beams=1, do_sample=False, max_new_tokens=MAX_NEW_TOKENS)
        corrected = self.tokenizer.decode(out[0], skip_special_tokens=True).strip()
        return {
            "corrected_text": corrected,
            "edits": edits_from_correction(text, corrected),
            "warnings": ["Model suggestion; absence of edits does not establish correctness"],
        }
