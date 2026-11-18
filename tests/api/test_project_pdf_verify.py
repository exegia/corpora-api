import io
import shutil
import subprocess
import sys
import zipfile
from uuid import uuid4

import pytest
from lxml import etree

from corpora_api import project_pdf_verify as verifier
from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.project_document_errors import DocumentVerificationUnavailable

pytestmark = pytest.mark.skipif(
    not shutil.which("pdfinfo") or not shutil.which("pdftotext"), reason="requires Poppler"
)


def pdf(*, rotate=0, text="Hello world"):
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Rotate {rotate} "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def case(*, rotate=0, text="Hello world"):
    data = pdf(rotate=rotate, text=text)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("sources/book.pdf", data)
    result = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "1", "-cropbox", "-bbox", "-enc", "UTF-8", "-", "-"],
        input=data,
        capture_output=True,
        check=True,
        timeout=10,
    )
    root = etree.fromstring(
        result.stdout, parser=etree.XMLParser(resolve_entities=False, no_network=True)
    )
    words = root.xpath("//*[local-name()='word']")
    if words:
        word = words[0]
        rect = {
            name: float(word.get(attr))
            for name, attr in (("x0", "xMin"), ("y0", "yMin"), ("x1", "xMax"), ("y1", "yMax"))
        }
        quote = "".join(word.itertext())
    else:
        rect = {"x0": 1, "y0": 1, "x1": 2, "y1": 2}
        quote = "empty"
    endpoint = {
        "work_id": "work",
        "edition_id": "edition",
        "package_id": str(uuid4()),
        "revision": "a" * 40,
        "document_id": "sources/book.pdf",
        "locators": [
            {
                "kind": "pdf",
                "asset_id": "sources/book.pdf",
                "page": 1,
                "rectangles": [rect],
                "text": {
                    "kind": "text",
                    "stream_id": "poppler-page-words-v1",
                    "start": 0,
                    "end": len(quote),
                    "exact": quote,
                },
            }
        ],
    }
    return archive.getvalue(), endpoint


def test_pdf_whole_word_selection_in_killable_worker():
    archive, endpoint = case()
    assert run_corpus(archive, "verify_pdf", raw_endpoint=endpoint) == {"verified": True}


def test_pdf_contiguous_word_range_requires_each_matching_rectangle():
    archive, endpoint = case()
    raw = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "1", "-cropbox", "-bbox", "-enc", "UTF-8", "-", "-"],
        input=pdf(),
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    words = root.xpath("//*[local-name()='word']")
    assert ["".join(word.itertext()) for word in words] == ["Hello", "world"]
    locator = endpoint["locators"][0]
    locator["rectangles"] = [
        {
            name: float(word.get(attr))
            for name, attr in (("x0", "xMin"), ("y0", "yMin"), ("x1", "xMax"), ("y1", "yMax"))
        }
        for word in words
    ]
    locator["text"]["end"] = 11
    locator["text"]["exact"] = "Hello world"
    assert verifier.verify_pdf_in_corpus(archive, endpoint) == {"verified": True}
    locator["rectangles"].reverse()
    with pytest.raises(ValueError):
        verifier.verify_pdf_in_corpus(archive, endpoint)


@pytest.mark.parametrize("alter", ["quote", "geometry", "partial", "page", "no_text"])
def test_pdf_rejects_unverified_selection(alter):
    archive, endpoint = case()
    locator = endpoint["locators"][0]
    if alter == "quote":
        locator["text"]["exact"] = "Jello"
    elif alter == "geometry":
        locator["rectangles"][0]["x0"] += 1
    elif alter == "partial":
        locator["text"]["end"] = 4
        locator["text"]["exact"] = "Hell"
    elif alter == "page":
        locator["page"] = 2
    else:
        locator["text"] = None
    with pytest.raises((ValueError, OSError)):
        verifier.verify_pdf_in_corpus(archive, endpoint)


def test_pdf_rejects_rotated_and_empty_pages():
    for options in ({"rotate": 90}, {"text": ""}):
        archive, endpoint = case(**options)
        with pytest.raises(ValueError):
            verifier.verify_pdf_in_corpus(archive, endpoint)


def test_pdf_tool_output_and_time_are_bounded(monkeypatch):
    with pytest.raises(ValueError, match="output exceeds"):
        verifier._capture([sys.executable, "-c", "print('x' * 10000)"], b"", max_output=10)
    monkeypatch.setattr(verifier, "MAX_TOOL_SECONDS", 0.1)
    with pytest.raises(DocumentVerificationUnavailable):
        verifier._capture([sys.executable, "-c", "import time; time.sleep(10)"], b"", max_output=10)


def test_worker_rejects_changed_pdf_text():
    archive, endpoint = case()
    endpoint["locators"][0]["text"]["exact"] = "Jello"
    with pytest.raises(CorpusProcessError):
        run_corpus(archive, "verify_pdf", raw_endpoint=endpoint)
