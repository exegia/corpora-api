"""Wire models reflecting the corpora-core JSON response contract."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from corpora_schema_types import TextRange
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CoreModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TFCheckIssue(BaseModel):
    code: str
    severity: Literal["error"]
    file: str
    line: int
    message: str


class TFCheckReport(BaseModel):
    status: Literal["passed", "failed"]
    check_scope: Literal["static-warp-preflight"] = Field(
        description="Checks core TF node and containment invariants; "
        "not a loader or source-fidelity test."
    )
    source_sha256: str = Field(description="SHA-256 of the exact uploaded ZIP bytes.")
    tf_root: str
    slot_type: str | None
    max_slot: int
    max_node: int
    issues: list[TFCheckIssue]
    issue_count: int

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "passed",
                    "check_scope": "static-warp-preflight",
                    "source_sha256": "0" * 64,
                    "tf_root": "corpus/tf",
                    "slot_type": "word",
                    "max_slot": 2,
                    "max_node": 3,
                    "issues": [],
                    "issue_count": 0,
                }
            ]
        }
    )


class ValidationResponse(BaseModel):
    """Legacy-compatible verdict for a caller-owned uploaded corpus archive."""

    corpus: str
    path: str = Field(description="Uploaded filename, never an internal staging path.")
    valid: bool
    stats: dict[str, int] | None
    reasons: list[str]
    checks: dict[str, bool | None]
    source_verified: bool | None = Field(
        default=None, description="CTEI source fidelity is outside archive validation."
    )


class StoredCorpusResponse(BaseModel):
    """Legacy-compatible metadata for an owned corpus archive."""

    filename: str
    size_bytes: int | None
    repo_id: str
    url: str


class CorpusNodeTypeStat(BaseModel):
    type: str
    count: int
    avg_slots: float
    is_slot: bool


class CorpusIndexResponse(BaseModel):
    toc: Any = None
    sections: dict[str, Any] | None
    node_types: list[CorpusNodeTypeStat]


class CorpusSectionPageResponse(BaseModel):
    parent: str | None
    levels: list[str]
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int
    next_offset: int | None


class CorpusPassage(BaseModel):
    node: int
    ref: str
    text: str
    tokens: list[dict[str, Any]]


class CorpusContentResponse(BaseModel):
    ref: str | None
    format: str
    passages: list[CorpusPassage]
    total: int
    offset: int
    limit: int
    next_offset: int | None


class CorpusNodeContext(BaseModel):
    node: int
    otype: str
    ref: str


class CorpusNodeDetailResponse(BaseModel):
    node: int
    otype: str
    is_slot: bool
    slot_type: str
    first_slot: int | None
    last_slot: int | None
    section_ref: str
    text: str
    features: dict[str, Any]
    annotation: dict[str, Any] | None
    node_types: list[str]
    context: list[CorpusNodeContext]
    occurrences: int
    occurrences_in_section: int


class DraftCreateRequest(BaseModel):
    job_id: str


class DraftInfo(BaseModel):
    id: str
    corpus: str
    revision: str
    digest: str
    version: str
    state: Literal["draft", "locked", "published"]
    archive_url: str


class DraftListResponse(BaseModel):
    drafts: list[DraftInfo]
    next_offset: int | None


class AIUnitRange(BaseModel):
    start: int
    end: int


class AINodeScope(BaseModel):
    corpus: str
    level: Literal["word", "passage", "section", "document", "corpus"]
    node_id: int | None = None
    node_type: str | None = None
    label: str
    unit_range: AIUnitRange | None = None
    version: str
    content_hash: str | None = None

    @model_validator(mode="after")
    def require_node(self) -> AINodeScope:
        if self.level != "corpus" and self.node_id is None:
            raise ValueError(f"{self.level}-level scope requires node_id")
        return self


class AIScopeRequest(BaseModel):
    scope: AINodeScope


class AIFinding(BaseModel):
    node_id: int
    node_type: str
    rule: str
    severity: Literal["error", "warn", "info"]
    message: str
    consequence: str
    fixable: bool
    unfixable_reason: str | None = None
    suggestion_id: str | None = None


class AIValidationResponse(BaseModel):
    corpus: str
    version: str
    checked_nodes: int
    findings: list[AIFinding]


class AIProvider(BaseModel):
    id: str
    label: str
    models: list[str]


class AIProvidersResponse(BaseModel):
    gateway: Literal["vercel-ai-gateway"] = "vercel-ai-gateway"
    providers: list[AIProvider]


class AIChatUnitRange(AIUnitRange):
    """Node IDs follow corpus reading order, not necessarily numeric order."""


class AIChatNodeScope(AINodeScope):
    unit_range: AIChatUnitRange | None = None


class AIChatRequest(BaseModel):
    thread_id: str | None = None
    scope: AIChatNodeScope
    message: str = Field(min_length=1)
    mode: Literal["ask", "fix"] = "ask"


class AIChatDiffRow(BaseModel):
    field: str
    old: str | None
    new: str | None


class AIChatSuggestion(BaseModel):
    id: str
    scope: AIChatNodeScope
    kind: Literal["annotation", "label", "boundary", "formatting", "reference", "text"]
    target_node: int
    diff: list[AIChatDiffRow] = Field(min_length=1)
    rationale: str
    generated: Literal[True] = True
    base_version: str
    content_hash: str
    status: Literal["pending", "applied", "rejected", "stale"] = "pending"
    finding: AIFinding | None = None


class AIChatErrorInfo(BaseModel):
    code: Literal["locked", "stale", "confirmation_required", "model_unavailable", "forbidden"]
    reason: str
    retryable: bool
    current_version: str | None = None


class AIChatTokenEvent(BaseModel):
    type: Literal["token"] = "token"
    text: str


class AIChatToolEvent(BaseModel):
    type: Literal["tool"] = "tool"
    tool: str
    status: Literal["started", "finished", "failed"]
    payload: dict[str, Any] | None = None


class AIChatSuggestionEvent(BaseModel):
    type: Literal["suggestion"] = "suggestion"
    suggestion: AIChatSuggestion


class AIChatDoneEvent(BaseModel):
    type: Literal["done"] = "done"
    thread_id: str
    message_id: str


class AIChatErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    error: AIChatErrorInfo


AIChatEvent = (
    AIChatTokenEvent | AIChatToolEvent | AIChatSuggestionEvent | AIChatDoneEvent | AIChatErrorEvent
)


class ReferenceCreate(BaseModel):
    corpus: str
    node: int = Field(ge=1)
    end_node: int | None = Field(default=None, ge=1)
    lang: str | None = None


class ShortcodeCreate(ReferenceCreate):
    url_template: str | None = None


class StoragePublishRequest(BaseModel):
    """Publish a completed legacy conversion by ID, never by server path."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID = Field(description="Succeeded legacy /convert job owned by the caller.")
    filename: str | None = Field(
        default=None,
        max_length=255,
        description="Optional flat .corpus or .cusx name for the published archive.",
    )

    @model_validator(mode="after")
    def validate_filename(self) -> StoragePublishRequest:
        if self.filename is None:
            return self
        if (
            not self.filename
            or len(self.filename.encode("utf-8")) > 255
            or self.filename in {".", ".."}
            or "/" in self.filename
            or "\\" in self.filename
            or any(ord(char) < 32 for char in self.filename)
            or not self.filename.endswith((".corpus", ".cusx"))
        ):
            raise ValueError("Provide a plain .corpus or .cusx filename")
        return self


class IngestCreated(BaseModel):
    job_id: str
    status_url: str
    download_url: str


class IngestJobStatus(BaseModel):
    """Legacy-compatible polling fields for a durable ingestion job."""

    id: str
    source_format: str
    output_format: str
    name: str
    display_name: str | None
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: float
    started_at: float | None
    finished_at: float | None
    error: str | None
    logs: list[str]
    last_log: str | None
    events: list[dict[str, object]]
    steps: list[dict[str, object]]
    validation: dict[str, object] | None
    result_filename: str
    download_ready: bool


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

    def text_range(self) -> TextRange:
        """Expose the core schema's Unicode scalar range without changing the HTTP wire shape."""
        return TextRange(start=self.start, end=self.end)

    @model_validator(mode="after")
    def validate_text_range(self) -> SpellDiagnostic:
        self.text_range()
        return self


class SpellReport(CoreModel):
    language: LanguageDetection
    passed: bool
    diagnostics: list[SpellDiagnostic]


class ErrorResponse(BaseModel):
    detail: str
