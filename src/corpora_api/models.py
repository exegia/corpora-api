"""Wire models reflecting the corpora-core JSON response contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CoreModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TextInput(BaseModel):
    text: str = Field(max_length=1_000_000, description="Exact decoded UTF-8 text to check.")


class StructureRequest(TextInput):
    source_id: str = Field(default="<input>", max_length=256)

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"source_id": "book.xml", "text": "<root/>"}]}
    )


class PurposeRequest(StructureRequest):
    expected_document_id: str | None = Field(default=None, max_length=256)
    allow_no_namespace: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "source_id": "book.xml",
                    "text": '<TEI xmlns="http://www.tei-c.org/ns/1.0"/>',
                    "expected_document_id": None,
                    "allow_no_namespace": False,
                }
            ]
        }
    )


class LanguageRequest(TextInput):
    locale: str | None = Field(default=None, max_length=64)

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"text": "A short English sentence.", "locale": "en-US"}]}
    )


class SpellingRequest(LanguageRequest):
    role: Literal["corpus", "metadata", "frontmatter", "footnote", "additional", "heading"] = (
        "corpus"
    )
    supplements: list[Literal["en_shared", "public-licenses"]] = Field(default_factory=list)


class SourcePosition(CoreModel):
    byte: int = Field(description="Zero-based UTF-8 byte offset.")
    scalar: int = Field(description="Zero-based Unicode scalar offset.")
    line: int = Field(description="One-based line number.")
    column: int = Field(description="One-based Unicode scalar column.")


class SourceRange(CoreModel):
    start: SourcePosition
    end: SourcePosition


class TextEdit(CoreModel):
    range: SourceRange
    before: str
    after: str


class Suggestion(CoreModel):
    message: str
    applicability: Literal["automatic", "review"]
    edits: list[TextEdit]


class Diagnostic(CoreModel):
    code: str
    category: Literal["structural", "spelling"]
    severity: Literal["warning", "error"]
    blocking: bool
    score: int | None
    certainty: Literal["certain", "possible"]
    message: str
    locations: list[SourceRange]
    suggestions: list[Suggestion]


class DiagnosticReport(CoreModel):
    schema_version: int = Field(description="Version 1 of the shared diagnostic contract.")
    source_id: str
    source_sha256: str = Field(description="SHA-256 of the exact input UTF-8 bytes.")
    diagnostics: list[Diagnostic]


class PurposeField(CoreModel):
    element_path: list[int]
    namespace: str
    name: str
    purpose: Literal["date", "identifier"]
    value: str
    range: SourceRange
    value_ranges: list[SourceRange]
    status: Literal["validated", "blocked"]
    diagnostic_index: int | None


class PurposeReport(DiagnosticReport):
    fields: list[PurposeField]
    policy_version: int
    expected_document_id: str | None
    allow_no_namespace: bool


class LanguageDetection(CoreModel):
    locale: str
    confidence: float | None = Field(description="Statistical confidence; null for a hint.")
    method: Literal["hint", "statistical", "undetermined"]
    dictionary: str | None
    alternatives: list[str]
    locale_is_default: bool
    note: str | None


class SpellDiagnostic(CoreModel):
    code: str
    text: str
    start: int = Field(description="Inclusive Unicode scalar offset.")
    end: int = Field(description="Exclusive Unicode scalar offset.")
    score: int = Field(description="Policy severity, not a confidence percentage.")
    blocking: bool
    review_optional: bool
    suggestions: list[str]
    replacement: str | None
    reason: str


class SpellReport(CoreModel):
    language: LanguageDetection
    passed: bool
    diagnostics: list[SpellDiagnostic]


class ErrorResponse(BaseModel):
    detail: str
