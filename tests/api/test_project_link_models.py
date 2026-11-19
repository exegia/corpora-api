from uuid import uuid4

import pytest
from pydantic import ValidationError

from corpora_api.project_link_models import LinkSave, draft_reference


def sample():
    corpus, other, actor = uuid4(), uuid4(), uuid4()
    revision = "a" * 40
    source = {
        "work_id": "text-work",
        "edition_id": "edition",
        "package_id": str(corpus),
        "revision": revision,
        "document_id": ".corpora/source.corpus",
        "locators": [{"kind": "structural", "anchor_id": f"{corpus}@{revision}/Mini!word1"}],
    }
    target = {
        "work_id": "pdf-work",
        "edition_id": "edition",
        "package_id": str(other),
        "revision": "b" * 40,
        "document_id": "sources/book.pdf",
        "locators": [
            {
                "kind": "pdf",
                "asset_id": "sources/book.pdf",
                "page": 1,
                "rectangles": [{"x0": 0, "y0": 0, "x1": 10, "y1": 10}],
            }
        ],
    }
    return {
        "reference_id": str(uuid4()),
        "source": source,
        "target": target,
        "reason": "review",
    }, actor


def test_link_reference_has_both_corpus_ids_revisions_and_server_provenance():
    body, actor = sample()
    value = LinkSave.model_validate(body)
    ref = draft_reference(value, actor)
    assert ref.source.package_id == body["source"]["package_id"]
    assert ref.target.revision == "b" * 40
    assert ref.provenance.agent_id == str(actor)
    assert ref.review == "pending" and ref.resolution == "unresolved"


def test_link_accepts_revision_pinned_epub_selection():
    body, _ = sample()
    body["target"]["document_id"] = "sources/book.epub"
    body["target"]["locators"] = [
        {
            "kind": "epub",
            "asset_id": "sources/book.epub",
            "href": "chapter.xhtml",
            "cfi": "epubcfi(/6/2)",
        }
    ]
    assert LinkSave.model_validate(body).target.document_id == "sources/book.epub"


@pytest.mark.parametrize(
    "change", ["missing_corpus", "revision", "path", "asset", "short_anchor", "foreign_anchor"]
)
def test_link_rejects_ambiguous_or_mismatched_identities(change):
    body, _ = sample()
    source, target = body["source"], body["target"]
    if change == "missing_corpus":
        source["package_id"] = "name-only"
    elif change == "revision":
        target["revision"] = "latest"
    elif change == "path":
        target["document_id"] = "../secret.pdf"
        target["locators"][0]["asset_id"] = "../secret.pdf"
    elif change == "asset":
        target["locators"][0]["asset_id"] = "different.pdf"
    elif change == "short_anchor":
        source["locators"][0]["anchor_id"] = "Mini!word1"
    else:
        source["locators"][0]["anchor_id"] = f"{uuid4()}@{'a' * 40}/Mini!word1"
    with pytest.raises(ValidationError):
        LinkSave.model_validate(body)
