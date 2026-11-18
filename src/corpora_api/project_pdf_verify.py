"""Verify exact whole-word PDF selections with bounded Poppler processes."""

import math
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time

from corpora_linking import PdfLocator, verify_text_anchor
from lxml import etree

from corpora_api.project_asset_verify import corpus_asset, sha256_revision
from corpora_api.project_document_errors import DocumentVerificationUnavailable
from corpora_api.project_link_models import LinkEndpoint

MAX_TOOL_BYTES = 2_000_000
MAX_TOOL_SECONDS = 15.0
MAX_WORDS = 10_000
MAX_PAGES = 1000
TOLERANCE = 0.02


def _capture(argv: list[str], data: bytes, *, max_output: int) -> bytes:
    """Bound process time and stdout without putting document bytes in argv."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C", "LANG": "C"}
    with tempfile.TemporaryFile() as source:
        source.write(data)
        source.seek(0)
        try:
            process = subprocess.Popen(
                argv,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except OSError:
            raise DocumentVerificationUnavailable("PDF verifier could not start") from None
        assert process.stdout is not None
        deadline = time.monotonic() + MAX_TOOL_SECONDS
        output = bytearray()
        try:
            os.set_blocking(process.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DocumentVerificationUnavailable("PDF verifier timed out")
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > max_output:
                                raise ValueError("PDF verifier output exceeds limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DocumentVerificationUnavailable("PDF verifier timed out")
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise DocumentVerificationUnavailable("PDF verifier timed out") from None
            if code != 0:
                raise ValueError("PDF verification failed")
            return bytes(output)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            process.stdout.close()


def _one(pattern: str, text: str) -> re.Match[str]:
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError("Ambiguous PDF information")
    return matches[0]


def _page_info(data: bytes, page: int) -> tuple[float, float]:
    info = _capture(
        ["pdfinfo", "-f", str(page), "-l", str(page), "-box", "-"], data, max_output=65536
    ).decode("utf-8", "replace")
    pages = int(_one(r"^Pages:\s*(\d+)\s*$", info)[1])
    encrypted = _one(r"^Encrypted:\s*(\S+)", info)[1]
    rotation = int(_one(rf"^Page\s+{page}\s+rot:\s*(-?\d+)\s*$", info)[1])
    number = r"(-?\d+(?:\.\d+)?)"
    box = _one(rf"^Page\s+{page}\s+CropBox:\s*{number}\s+{number}\s+{number}\s+{number}\s*$", info)
    coords = [float(value) for value in box.groups()]
    width, height = coords[2] - coords[0], coords[3] - coords[1]
    if (
        not 1 <= page <= pages <= MAX_PAGES
        or encrypted.lower() != "no"
        or rotation != 0
        or not all(math.isfinite(value) for value in coords)
        or not 0 < width <= 20_000
        or not 0 < height <= 20_000
    ):
        raise ValueError("Unsupported PDF page")
    return width, height


def _page_words(data: bytes, page: int, width: float, height: float):
    xml = _capture(
        [
            "pdftotext",
            "-f",
            str(page),
            "-l",
            str(page),
            "-cropbox",
            "-bbox",
            "-enc",
            "UTF-8",
            "-",
            "-",
        ],
        data,
        max_output=MAX_TOOL_BYTES,
    )
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False)
    root = etree.fromstring(xml, parser)
    pages = root.xpath("//*[local-name()='page']")
    if len(pages) != 1:
        raise ValueError("Ambiguous PDF page")
    item = pages[0]
    if (
        abs(float(item.get("width")) - width) > 0.05
        or abs(float(item.get("height")) - height) > 0.05
    ):
        raise ValueError("PDF page dimensions do not match")
    nodes = item.xpath(".//*[local-name()='word']")
    if not nodes or len(nodes) > MAX_WORDS:
        raise ValueError("PDF text unavailable or too large")
    words = []
    for node in nodes:
        text = "".join(node.itertext())
        box = tuple(float(node.get(key)) for key in ("xMin", "yMin", "xMax", "yMax"))
        if (
            not text
            or any(char.isspace() for char in text)
            or not all(math.isfinite(value) for value in box)
            or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)
        ):
            raise ValueError("Invalid PDF word geometry")
        words.append((text, box))
    return words


def verify_pdf_in_corpus(archive_bytes: bytes, raw_endpoint: dict) -> dict:
    endpoint = LinkEndpoint.model_validate(raw_endpoint)
    if not endpoint.document_id or not endpoint.document_id.lower().endswith(".pdf"):
        raise ValueError("PDF asset path required")
    if len(endpoint.locators) != 1 or not isinstance(endpoint.locators[0], PdfLocator):
        raise ValueError("One exact PDF selection required")
    locator = endpoint.locators[0]
    selector = locator.text
    if (
        selector is None
        or selector.stream_id != "poppler-page-words-v1"
        or selector.normalization != "preserve"
        or locator.quads
        or not locator.rectangles
        or locator.page > MAX_PAGES
    ):
        raise ValueError("Unsupported PDF selection")
    data = corpus_asset(archive_bytes, endpoint.document_id)
    width, height = _page_info(data, locator.page)
    words = _page_words(data, locator.page, width, height)
    stream = " ".join(word for word, _ in words)
    positions = []
    cursor = 0
    for word, box in words:
        start = cursor
        cursor += len(word)
        positions.append((start, cursor, box))
        cursor += 1
    selected = [
        value for value in positions if selector.start <= value[0] and value[1] <= selector.end
    ]
    if (
        not selected
        or selected[0][0] != selector.start
        or selected[-1][1] != selector.end
        or len(selected) != len(locator.rectangles)
    ):
        raise ValueError("PDF selection must cover complete words")
    for (_, _, actual), rectangle in zip(selected, locator.rectangles, strict=True):
        supplied = (rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1)
        if any(abs(a - b) > TOLERANCE for a, b in zip(actual, supplied, strict=True)):
            raise ValueError("PDF selection geometry does not match")
    digest = sha256_revision(data)
    if (
        verify_text_anchor(selector, stream, expected_revision=digest, actual_revision=digest)
        != "valid"
    ):
        raise ValueError("PDF selection text does not match")
    return {"verified": True}
