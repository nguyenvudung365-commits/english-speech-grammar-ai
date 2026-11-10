"""Validate test-double outputs before admitting them into durable results."""
from difflib import SequenceMatcher

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    replacement: str = Field(max_length=3000)
    type: str = Field(min_length=1, max_length=100)
    explanation: str = Field(max_length=500)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("Invalid span")
        return self


class GrammarOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    corrected_text: str = Field(min_length=1, max_length=6000)
    edits: list[Edit] = Field(max_length=100)
    warnings: list[str] = Field(max_length=20)

    def validate_source(self, source: str) -> dict:
        cursor, rebuilt = 0, ""
        for edit in self.edits:
            if edit.start < cursor or edit.end > len(source):
                raise ValueError("Span exceeds immutable source or overlaps")
            rebuilt += source[cursor:edit.start] + edit.replacement
            cursor = edit.end
        rebuilt += source[cursor:]
        if rebuilt != self.corrected_text:
            raise ValueError("Edits do not reconstruct correction")
        # Conservative demo guard, not a claim of semantic preservation.
        for negative in ("not", "never", "no"):
            if negative in source.lower().split() and negative not in rebuilt.lower().split():
                raise ValueError("Negation removed; human review required")
        if any(len(warning) > 500 for warning in self.warnings):
            raise ValueError("Warning too long")
        return self.model_dump()


def edits_from_correction(source: str, corrected: str) -> list[dict]:
    """Character-level non-overlapping edits that rebuild `corrected` from `source`."""
    matcher = SequenceMatcher(None, source, corrected, autojunk=False)
    edits = [
        {
            "start": i1,
            "end": i2,
            "replacement": corrected[j1:j2],
            "type": "model-edit",
            "explanation": "Suggested by the grammar model",
        }
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]
    if len(edits) > 100:  # GrammarOutput cap: fall back to one whole-text replacement
        edits = [{**edits[0], "start": 0, "end": len(source), "replacement": corrected}]
    return edits
