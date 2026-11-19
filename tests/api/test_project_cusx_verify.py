"""Actual C-USX 0.1 selection verification inside corpus archives."""

import hashlib
import io
import json
import zipfile
from uuid import uuid4
from xml.etree import ElementTree as ET

import pytest
from corpus_validate import validate_archive

from corpora_api.corpus_process import run_corpus
from corpora_api.project_cusx_verify import retrieve_cusx_in_corpus


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def cusx_package(*, references=None, identity_override=None):
    """Create a small self-contained package that the real core validator accepts."""
    profile = "corpora-cusx-package/0.1.0"
    namespace = "urn:corpora:usx-extension:0.1"
    ET.register_namespace("cx", namespace)
    root = ET.Element("usx", {"version": "3.1", f"{{{namespace}}}profile": profile})
    unit = ET.SubElement(root, f"{{{namespace}}}unit", {"type": "paragraph", "attributes": "{}"})
    ET.SubElement(unit, "para", {"style": "p"}).text = "😀 Selected words. John 3:16."
    unit.tail = "\n\n"
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    text = "".join(root.itertext())
    identity = {
        "work_id": "urn:work:test",
        "edition_id": "urn:edition:test",
        "package_id": "urn:uuid:" + str(uuid4()),
        "revision": "sha256:" + hashlib.sha256(xml).hexdigest(),
        "document_id": "document.usx",
        "locators": [],
    }
    if identity_override:
        identity.update(identity_override)
    members = {
        "document.usx": xml,
        "document.json": encoded(
            {
                "metadata": {},
                "units": [
                    {
                        "type": "paragraph",
                        "tokens": [{"text": "😀 Selected words. John 3:16.", "after": ""}],
                    }
                ],
            }
        ),
        "snapshot.json": encoded(
            {"endpoint": identity, "stream_id": "cusx-itertext/v1", "text": text}
        ),
        "references.json": encoded([] if references is None else references),
        "mappings.json": encoded({"mappings": []}),
    }
    manifest = {
        "format": profile,
        "identity": identity,
        "stream": {
            "id": "cusx-itertext/v1",
            "offset_unit": "unicode-scalar",
            "normalization": "preserve",
        },
        "files": {
            name: "sha256:" + hashlib.sha256(data).hexdigest() for name, data in members.items()
        },
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in {"manifest.json": encoded(manifest), **members}.items():
            archive.writestr(name, data)
    return out.getvalue(), identity, text


def case(*, references=None, identity_override=None):
    package, identity, text = cusx_package(
        references=references, identity_override=identity_override
    )
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sources/book.cusx", package)
    start = text.index("Selected words")
    endpoint = {
        "work_id": identity["work_id"],
        "edition_id": identity["edition_id"],
        "package_id": str(uuid4()),
        "revision": "a" * 40,
        "document_id": "sources/book.cusx",
        "locators": [
            {
                "kind": "text",
                "stream_id": "cusx-itertext/v1",
                "start": start,
                "end": start + len("Selected words"),
                "exact": "Selected words",
            }
        ],
    }
    return outer.getvalue(), endpoint, package


def test_real_cusx_profile_selection_is_verified_in_worker():
    archive, endpoint, package = case()
    assert validate_archive(package, "book.cusx")["valid"] is True
    assert run_corpus(archive, "verify_cusx", raw_endpoint=endpoint) == {"verified": True}
    assert run_corpus(archive, "retrieve_cusx", raw_endpoint=endpoint) == {"text": "Selected words"}


@pytest.mark.parametrize(
    "change", ["quote", "offset", "stream", "normalization", "work", "edition", "asset"]
)
def test_stale_or_incomplete_cusx_selection_is_rejected(change):
    archive, endpoint, _ = case()
    locator = endpoint["locators"][0]
    if change == "quote":
        locator["exact"] = "Different text"
    elif change == "offset":
        locator["start"] += 1
    elif change == "stream":
        locator["stream_id"] = "another-stream"
    elif change == "normalization":
        locator["normalization"] = "NFC"
    elif change == "work":
        endpoint["work_id"] = "different work"
    elif change == "edition":
        endpoint["edition_id"] = "different edition"
    else:
        endpoint["document_id"] = "missing.cusx"
    with pytest.raises(ValueError):
        retrieve_cusx_in_corpus(archive, endpoint)


def test_unsupported_reference_claims_and_missing_package_identity_are_rejected():
    archive, endpoint, _ = case(references=[{"id": "invented"}])
    with pytest.raises(ValueError):
        retrieve_cusx_in_corpus(archive, endpoint)
    archive, endpoint, _ = case(identity_override={"package_id": None})
    with pytest.raises(ValueError):
        retrieve_cusx_in_corpus(archive, endpoint)


def test_nested_package_duplicate_member_is_rejected():
    _, endpoint, package = case()
    nested = io.BytesIO(package)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(nested, "a") as archive:
            archive.writestr("snapshot.json", b"{}")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sources/book.cusx", nested.getvalue())
    with pytest.raises(ValueError):
        retrieve_cusx_in_corpus(outer.getvalue(), endpoint)
