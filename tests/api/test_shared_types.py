"""Shared Rust/Python schema consumption in the API response adapter."""

import pytest
from corpora_schema_types import TextRange
from pydantic import ValidationError

from corpora_api.models import SpellDiagnostic


def spell_diagnostic(*, start: int = 1, end: int = 2) -> SpellDiagnostic:
    return SpellDiagnostic(
        code="spelling",
        text="😀",
        start=start,
        end=end,
        score=1,
        blocking=False,
        review_optional=True,
        suggestions=[],
        replacement=None,
        reason="test",
    )


def test_spelling_offsets_use_generated_text_range() -> None:
    diagnostic = spell_diagnostic()

    assert diagnostic.text_range() == TextRange(start=1, end=2)
    assert diagnostic.text_range().model_dump_json() == '{"start":1,"end":2}'
    assert diagnostic.model_dump()["start"] == 1
    assert "text_range" not in diagnostic.model_dump()


@pytest.mark.parametrize("start,end", [(-1, 2), (0, 4294967296)])
def test_spelling_offsets_reject_values_outside_core_u32_range(start: int, end: int) -> None:
    with pytest.raises(ValidationError):
        spell_diagnostic(start=start, end=end)
