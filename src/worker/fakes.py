"""Deterministic TEST DOUBLES, never models or quality measurements."""

import io
import struct
import wave
from dataclasses import dataclass
from typing import Protocol


class LanguageAdapter(Protocol):
    def asr(self, audio: bytes) -> str: ...
    def pronunciation(self, audio: bytes, reference: str) -> dict: ...
    def grammar(self, text: str) -> dict: ...


class AdapterFailure(Exception):
    def __init__(self, code="FAKE_STAGE_ERROR"):
        self.code = code


@dataclass
class FakeAdapters:
    """Only two canned grammar examples; unknown sentences are identity output."""

    fail_asr: bool = False
    fail_pronunciation: bool = False
    fail_grammar: bool = False

    @staticmethod
    def signal(audio: bytes):
        with wave.open(io.BytesIO(audio), "rb") as wav:
            values = struct.iter_unpack("<h", wav.readframes(wav.getnframes()))
            if not any(abs(value[0]) >= 64 for value in values):
                raise AdapterFailure("INSUFFICIENT_SIGNAL")

    def asr(self, audio: bytes) -> str:
        if self.fail_asr:
            raise AdapterFailure()
        self.signal(audio)
        return "He go home."

    def pronunciation(self, audio: bytes, reference: str) -> dict:
        if self.fail_pronunciation:
            raise AdapterFailure()
        self.signal(audio)
        return {
            "fake": True,
            "score": None,
            "method": "fake-no-assessment-v1",
            "warning": "No acoustic assessment, alignment, human labels, or calibrated score",
        }

    def grammar(self, text: str) -> dict:
        if self.fail_grammar:
            raise AdapterFailure()
        mapping = {
            "He go home.": ("He goes home.", 3, 5, "goes"),
            "She do not like coffee.": ("She does not like coffee.", 4, 6, "does"),
        }
        match = mapping.get(text)
        return {
            "corrected_text": match[0] if match else text,
            "edits": [
                {
                    "start": match[1],
                    "end": match[2],
                    "replacement": match[3],
                    "type": "canned-test-fixture",
                    "explanation": "FAKE subject-verb example",
                }
            ]
            if match
            else [],
            "warnings": [
                "FAKE canned rules; absence of edits does not establish grammatical correctness"
            ],
        }
