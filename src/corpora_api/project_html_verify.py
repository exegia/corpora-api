"""Map browser text nodes only when they exactly match the pinned HTML tree.

The native beautifulsoup-html.parser/v1 contract counts every child from the
document root, excludes non-reading subtrees and inserts LF at block boundaries.
Browser repair or dynamic changes that alter text-node paths reject, never relocate.
"""

import platform

import bs4
from bs4 import Comment, Declaration, Doctype, NavigableString, ProcessingInstruction, Tag
from corpora_linking import HtmlRangeLocator, TextLocator, verify_text_anchor

from corpora_api.browser_selection_models import BrowserSelectionRequest
from corpora_api.project_asset_verify import corpus_asset
from corpora_api.project_epub_verify import utf16_to_scalar
from corpora_api.project_link_models import LinkEndpoint

SKIP = {"head", "script", "style", "template", "noscript"}
BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "aside",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "ul",
    "ol",
    "table",
    "tr",
    "pre",
    "hr",
}


def parser_version() -> str:
    return f"beautifulsoup:{bs4.__version__};python:{platform.python_version()}"


def html_stream(data: bytes) -> tuple[str, list[tuple[tuple[int, ...], int, str]]]:
    soup = bs4.BeautifulSoup(data.decode("utf-8"), "html.parser")
    parts: list[str] = []
    spans: list[tuple[tuple[int, ...], int, str]] = []
    offset = 0

    def emit(value: str) -> None:
        nonlocal offset
        parts.append(value)
        offset += len(value)

    def newline() -> None:
        if parts and not parts[-1].endswith("\n"):
            emit("\n")

    def visit(node, path: tuple[int, ...]) -> None:
        if len(path) > 128:
            raise ValueError("HTML nesting exceeds the limit")
        if isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction)):
            return
        if isinstance(node, NavigableString):
            text = str(node)
            if text:
                spans.append((path, offset, text))
                if len(spans) > 10_000:
                    raise ValueError("HTML text-node count exceeds the limit")
                emit(text)
            return
        if not isinstance(node, Tag) or node.name in SKIP:
            return
        if node.name == "br":
            emit("\n")
            return
        if node.name in BLOCK:
            newline()
        for index, child in enumerate(node.contents):
            visit(child, (*path, index))
        if node.name in BLOCK:
            newline()

    for index, child in enumerate(soup.contents):
        visit(child, (index,))
    return "".join(parts), spans


def _bounds(spans, start_path, start_offset, end_path, end_offset, *, utf16=False):
    by_path = {path: (start, quote) for path, start, quote in spans}
    if start_path not in by_path or end_path not in by_path:
        raise ValueError("HTML selection endpoint is unavailable or excluded")
    start_base, start_text = by_path[start_path]
    end_base, end_text = by_path[end_path]
    if utf16:
        start_offset = utf16_to_scalar(start_text, start_offset)
        end_offset = utf16_to_scalar(end_text, end_offset)
    if not (0 <= start_offset <= len(start_text) and 0 <= end_offset <= len(end_text)):
        raise ValueError("HTML selection offset exceeds text node")
    start, end = start_base + start_offset, end_base + end_offset
    if start >= end:
        raise ValueError("HTML selection is empty or reversed")
    return start, end, start_offset, end_offset


def map_browser_in_corpus(archive_bytes: bytes, raw_capture: dict) -> dict:
    body = BrowserSelectionRequest.model_validate(raw_capture)
    asset = body.original.document_id
    assert asset is not None
    text, spans = html_stream(corpus_asset(archive_bytes, asset, max_bytes=350_000))
    if body.captured_nodes != tuple((path, quote) for path, _, quote in spans):
        raise ValueError("Browser text-node tree differs from the pinned HTML source")
    start, end, first, last = _bounds(
        spans, body.start_path, body.start_utf16, body.end_path, body.end_utf16, utf16=True
    )
    selected = text[start:end]
    if len(selected.encode("utf-8")) > 32768:
        raise ValueError("HTML selection exceeds the size limit")
    locator = HtmlRangeLocator(
        asset_id=asset,
        parser_version=parser_version(),
        start_path=body.start_path,
        start_offset=first,
        end_path=body.end_path,
        end_offset=last,
        text=TextLocator(
            stream_id="html-body",
            start=start,
            end=end,
            exact=selected,
            prefix=text[max(0, start - 32) : start],
            suffix=text[end : end + 32],
        ),
    )
    endpoint = LinkEndpoint.model_validate({**body.original.model_dump(), "locators": [locator]})
    return {"endpoint": endpoint.model_dump(mode="json"), "text": selected}


def retrieve_html_in_corpus(archive_bytes: bytes, raw_endpoint: dict) -> dict:
    endpoint = LinkEndpoint.model_validate(raw_endpoint)
    if not (endpoint.document_id or "").lower().endswith((".html", ".htm")):
        raise ValueError("HTML asset path required")
    if len(endpoint.locators) != 1 or not isinstance(endpoint.locators[0], HtmlRangeLocator):
        raise ValueError("One exact HTML range required")
    locator = endpoint.locators[0]
    if locator.parser_version != parser_version():
        raise ValueError("HTML parser version changed; recapture the selection")
    text, spans = html_stream(corpus_asset(archive_bytes, endpoint.document_id, max_bytes=350_000))
    start, end, _, _ = _bounds(
        spans, locator.start_path, locator.start_offset, locator.end_path, locator.end_offset
    )
    if (
        locator.text.stream_id != "html-body"
        or locator.text.normalization != "preserve"
        or (start, end) != (locator.text.start, locator.text.end)
        or verify_text_anchor(
            locator.text,
            text,
            expected_revision=endpoint.revision,
            actual_revision=endpoint.revision,
        )
        != "valid"
    ):
        raise ValueError("HTML selection does not match its source")
    selected = text[start:end]
    if len(selected.encode("utf-8")) > 32768:
        raise ValueError("HTML selection exceeds the size limit")
    return {"text": selected}


def verify_html_in_corpus(archive_bytes: bytes, raw_endpoint: dict) -> dict:
    retrieve_html_in_corpus(archive_bytes, raw_endpoint)
    return {"verified": True}
