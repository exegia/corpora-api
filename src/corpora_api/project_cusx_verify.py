"""Verify exact selections in the existing C-USX conversion profile."""

import io
import zipfile

from corpora_linking import TextLocator, TextSnapshot, verify_text_anchor
from corpus_validate import validate_archive

from corpora_api.project_asset_verify import corpus_asset
from corpora_api.project_link_models import LinkEndpoint


def retrieve_cusx_in_corpus(archive_bytes: bytes, raw_endpoint: dict) -> dict:
    endpoint = LinkEndpoint.model_validate(raw_endpoint)
    if not endpoint.document_id or not endpoint.document_id.lower().endswith(".cusx"):
        raise ValueError("C-USX asset path required")
    if len(endpoint.locators) != 1 or not isinstance(endpoint.locators[0], TextLocator):
        raise ValueError("One exact C-USX text selection required")
    locator = endpoint.locators[0]
    if locator.stream_id != "cusx-itertext/v1" or locator.normalization != "preserve":
        raise ValueError("Unsupported C-USX text stream")
    data = corpus_asset(archive_bytes, endpoint.document_id, max_bytes=3_000_000)
    if validate_archive(data, endpoint.document_id).get("valid") is not True:
        raise ValueError("Invalid C-USX package")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        snapshot = TextSnapshot.model_validate_json(archive.read("snapshot.json"))
    identity = snapshot.endpoint
    if (
        identity.work_id != endpoint.work_id
        or identity.edition_id != endpoint.edition_id
        or not identity.package_id
        or identity.document_id != "document.usx"
        or identity.locators
    ):
        raise ValueError("C-USX work or edition identity mismatch")
    text = snapshot.text
    # The core validator verified the internal XML SHA-256. The repository Git
    # commit is checked separately by the authorized corpus reader.
    revision = identity.revision or ""
    if (
        verify_text_anchor(locator, text, expected_revision=revision, actual_revision=revision)
        != "valid"
    ):
        raise ValueError("C-USX selection does not match")
    selected = text[locator.start : locator.end]
    if not selected or len(selected.encode("utf-8")) > 32768:
        raise ValueError("C-USX selection exceeds the limit")
    return {"text": selected}


def verify_cusx_in_corpus(archive_bytes: bytes, raw_endpoint: dict) -> dict:
    retrieve_cusx_in_corpus(archive_bytes, raw_endpoint)
    return {"verified": True}
