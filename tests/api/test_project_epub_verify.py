import io
import zipfile
from uuid import uuid4

import pytest

from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.project_epub_verify import _xml, verify_epub_in_corpus


def epub(extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="EPUB/package.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "EPUB/package.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
            '<item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="first"/></spine></package>',
        )
        archive.writestr(
            "EPUB/first.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<p>😀 See <em>John</em> 3:16.</p></body></html>",
        )
        if extra:
            archive.writestr(*extra)
    return output.getvalue()


def case(extra=None):
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sources/book.epub", epub(extra))
    endpoint = {
        "work_id": "work",
        "edition_id": "edition",
        "package_id": str(uuid4()),
        "revision": "a" * 40,
        "document_id": "sources/book.epub",
        "locators": [
            {
                "kind": "epub",
                "asset_id": "sources/book.epub",
                "href": "first.xhtml",
                "cfi": "epubcfi(/4/2!/2/2/2,/1:0,/1:4)",
                "text": {
                    "kind": "text",
                    "stream_id": "epub-cfi-dom",
                    "start": 6,
                    "end": 10,
                    "exact": "John",
                },
            }
        ],
    }
    return outer.getvalue(), endpoint


def test_valid_epub_selection_is_verified_in_killable_worker():
    archive, endpoint = case()
    assert run_corpus(archive, "verify_epub", raw_endpoint=endpoint) == {"verified": True}


@pytest.mark.parametrize("alter", ["quote", "href", "missing", "utf16"])
def test_invalid_epub_selection_is_rejected(alter):
    archive, endpoint = case()
    locator = endpoint["locators"][0]
    if alter == "quote":
        locator["text"]["exact"] = "Nope"
    elif alter == "href":
        locator["href"] = "other.xhtml"
    elif alter == "missing":
        endpoint["document_id"] = "missing.epub"
        locator["asset_id"] = "missing.epub"
    else:
        locator["cfi"] = "epubcfi(/4/2!/2/2,/1:1,/1:4)"
    with pytest.raises(ValueError):
        verify_epub_in_corpus(archive, endpoint)


def test_nested_epub_rejects_expansion_and_unsafe_paths():
    archive, endpoint = case(("huge", b"x" * 16_777_217))
    with pytest.raises(ValueError, match="Unsafe EPUB entry"):
        verify_epub_in_corpus(archive, endpoint)
    archive, endpoint = case(("../escape", b"content"))
    with pytest.raises(ValueError, match="Unsafe EPUB entry"):
        verify_epub_in_corpus(archive, endpoint)


def test_epub_xml_rejects_document_types_and_external_entities():
    data = b'<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]><html>&x;</html>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        _xml(data)


def test_worker_rejects_unverifiable_selection():
    archive, endpoint = case()
    endpoint["locators"][0]["text"]["exact"] = "Nope"
    with pytest.raises(CorpusProcessError):
        run_corpus(archive, "verify_epub", raw_endpoint=endpoint)


def test_epub_resolution_requires_current_authorized_digest(monkeypatch):
    import asyncio
    import hashlib
    from types import SimpleNamespace

    from fastapi import HTTPException

    from corpora_api import project_epub
    from corpora_api.project_link_models import LinkEndpoint

    archive, raw = case()
    endpoint = LinkEndpoint.model_validate(raw)
    head = SimpleNamespace(
        commit_sha="a" * 40,
        archive_size=len(archive),
        archive_sha256=hashlib.sha256(archive).hexdigest(),
    )
    access = SimpleNamespace(row=SimpleNamespace(state="ready"), head=head)
    monkeypatch.setattr(project_epub, "read_access", lambda *_: access)
    transfers = []

    class Reader:
        async def transfer_authorized(self, _row, revision, path, *, sink, **_kwargs):
            transfers.append((revision, path))
            await sink(archive)

    monkeypatch.setattr(project_epub.corpus_detail_api, "hub_reader", Reader())
    asyncio.run(project_epub.verify_epub({"sub": "test"}, endpoint))
    assert transfers == [("a" * 40, ".corpora/source.corpus")]
    head.commit_sha = "b" * 40
    with pytest.raises(HTTPException) as stale:
        asyncio.run(project_epub.verify_epub({"sub": "test"}, endpoint))
    assert stale.value.status_code == 409 and len(transfers) == 1
    head.commit_sha = "a" * 40
    head.archive_sha256 = "0" * 64
    with pytest.raises(HTTPException) as corrupt:
        asyncio.run(project_epub.verify_epub({"sub": "test"}, endpoint))
    assert corrupt.value.status_code == 503
