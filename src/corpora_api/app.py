"""FastAPI and FastMCP frontends for the same corpora-core calls."""

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

from corpora_api.core import CoreBridge, CoreProtocolError, CoreRejected, CoreUnavailable
from corpora_api.models import (
    DiagnosticReport,
    ErrorResponse,
    LanguageDetection,
    LanguageRequest,
    PurposeReport,
    PurposeRequest,
    SpellingRequest,
    SpellReport,
    StructureRequest,
)

bridge = CoreBridge()
mcp = FastMCP("Corpora text checks")
mcp_app = mcp.http_app(path="/")
app = FastAPI(
    title="Corpora API",
    summary="Source-bound text checks from corpora-core",
    description=(
        "Checks decoded text without changing it. Structural and metadata reports are bound "
        "to the exact input by SHA-256. Spelling requires installed dictionary data. "
        "The same operations are available as tools at `/mcp/`."
    ),
    version="0.1.0",
    lifespan=mcp_app.lifespan,
    servers=[{"url": "http://localhost:8000", "description": "Local API"}],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://docs.exegia.co", "http://localhost:4321"],
    allow_methods=["POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def bearer_auth(request: Request, call_next: Any) -> Any:
    """Allow local use without a token; require it when configured for a public host."""
    token = os.getenv("CORPORA_API_TOKEN")
    if token and request.url.path.startswith(("/v1/", "/mcp")):
        if request.headers.get("Authorization") != f"Bearer {token}":
            return JSONResponse(status_code=401, content={"detail": "Invalid bearer token"})
    return await call_next(request)


@app.exception_handler(CoreUnavailable)
async def unavailable(_request: Request, error: CoreUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(error)})


@app.exception_handler(CoreRejected)
async def rejected(_request: Request, error: CoreRejected) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(error)})


@app.exception_handler(CoreProtocolError)
async def protocol_error(_request: Request, _error: CoreProtocolError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": "Core response contract failed"})


def structure(request: StructureRequest) -> dict[str, Any]:
    return bridge.run({"operation": "structure", **request.model_dump()})


def purpose(request: PurposeRequest) -> dict[str, Any]:
    return bridge.run({"operation": "purpose", **request.model_dump()})


def language(request: LanguageRequest) -> dict[str, Any]:
    return bridge.run({**request.model_dump(), "detect_only": True})


def spelling(request: SpellingRequest) -> dict[str, Any]:
    return bridge.run(request.model_dump(), needs_dictionaries=True)


@app.get("/health", tags=["service"], summary="Service health")
def health() -> dict[str, str]:
    return {"status": "ok"}


ERRORS = {
    422: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}
STRUCTURE_EXAMPLE = {
    "schema_version": 1,
    "source_id": "book.xml",
    "source_sha256": "6238462c4873df4c50bc1a901ece576b2d166b0156802c88a05a85b1f5d511c7",
    "diagnostics": [],
}
PURPOSE_EXAMPLE = {
    "schema_version": 1,
    "source_id": "sample.xml",
    "source_sha256": "59c5e2b0f1f5b2b6deb60d825a57f1b6c6ad9bd8c085ddd6c2a8435476ca3924",
    "diagnostics": [],
    "fields": [],
    "policy_version": 1,
    "expected_document_id": None,
    "allow_no_namespace": False,
}
LANGUAGE_EXAMPLE = {
    "locale": "en-US",
    "confidence": None,
    "method": "hint",
    "dictionary": "en_US",
    "alternatives": ["en_GB", "en_CA"],
    "locale_is_default": False,
    "note": None,
}


@app.post(
    "/v1/checks/structure",
    response_model=DiagnosticReport,
    responses={**ERRORS, 200: {"content": {"application/json": {"example": STRUCTURE_EXAMPLE}}}},
    tags=["checks"],
    summary="Check XML structure",
    description=(
        "Strict, namespace-aware XML check. Reports source-bound diagnostics; no edits are applied."
    ),
)
async def check_structure(request: StructureRequest) -> dict[str, Any]:
    return await run_in_threadpool(structure, request)


@app.post(
    "/v1/checks/tei-metadata",
    response_model=PurposeReport,
    responses={**ERRORS, 200: {"content": {"application/json": {"example": PURPOSE_EXAMPLE}}}},
    tags=["checks"],
    summary="Check TEI metadata",
    description="Validate bounded TEI date and primary identifier policy against an exact source.",
)
async def check_tei_metadata(request: PurposeRequest) -> dict[str, Any]:
    return await run_in_threadpool(purpose, request)


@app.post(
    "/v1/language/detect",
    response_model=LanguageDetection,
    responses={**ERRORS, 200: {"content": {"application/json": {"example": LANGUAGE_EXAMPLE}}}},
    tags=["language"],
    summary="Detect or select language",
    description=(
        "A locale hint takes precedence over statistical detection. No dictionary data is needed."
    ),
)
async def detect_language(request: LanguageRequest) -> dict[str, Any]:
    return await run_in_threadpool(language, request)


@app.post(
    "/v1/checks/spelling",
    response_model=SpellReport,
    responses={
        **ERRORS,
        200: {
            "content": {
                "application/json": {
                    "example": {"language": LANGUAGE_EXAMPLE, "passed": True, "diagnostics": []}
                }
            }
        },
    },
    tags=["checks"],
    summary="Check spelling",
    description=(
        "Dictionary-backed findings for a decoded text segment. "
        "Suggestions never change source text."
    ),
)
async def check_spelling(request: SpellingRequest) -> dict[str, Any]:
    return await run_in_threadpool(spelling, request)


@mcp.tool
def check_xml_structure(text: str, source_id: str = "<input>") -> dict[str, Any]:
    """Check strict XML and return exact, source-hashed diagnostic locations."""
    return structure(StructureRequest(text=text, source_id=source_id))


@mcp.tool
def check_tei_purpose(
    text: str,
    source_id: str = "<input>",
    expected_document_id: str | None = None,
    allow_no_namespace: bool = False,
) -> dict[str, Any]:
    """Check TEI dates and the caller-expected primary document identifier."""
    return purpose(
        PurposeRequest(
            text=text,
            source_id=source_id,
            expected_document_id=expected_document_id,
            allow_no_namespace=allow_no_namespace,
        )
    )


@mcp.tool
def detect_text_language(text: str, locale: str | None = None) -> dict[str, Any]:
    """Detect language or select an explicit locale using corpora-core evidence."""
    return language(LanguageRequest(text=text, locale=locale))


@mcp.tool
def check_text_spelling(
    text: str,
    locale: str | None = None,
    role: str = "corpus",
) -> dict[str, Any]:
    """Check spelling using installed dictionaries; never rewrite the text."""
    return spelling(SpellingRequest(text=text, locale=locale, role=role))


app.mount("/mcp", mcp_app)
