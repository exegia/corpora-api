import copy
import hashlib
import importlib
import io
import json
import time
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from corpora_api import project_document, project_link_api
from corpora_api.browser_selection_models import BrowserSelectionRequest
from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.project_html_verify import MAX_MAPPED_ENDPOINT_BYTES, map_browser_in_corpus
from corpora_api.project_link_models import LinkSave

HTML = (
    "<!DOCTYPE html><html><head><title>Excluded</title></head><body>"
    "<p>😀 See <em>John</em><!-- comment --> 3:16.</p>"
    "<p>Next<br>line.</p><script>excluded</script></body></html>"
)


def case():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sources/book.html", HTML)
    capture = {
        "original": {
            "work_id": "work",
            "edition_id": "edition",
            "package_id": str(uuid4()),
            "revision": "a" * 40,
            "document_id": "sources/book.html",
            "locators": [],
        },
        "captured_nodes": [
            [[1, 1, 0, 0], "😀 See "],
            [[1, 1, 0, 1, 0], "John"],
            [[1, 1, 0, 3], " 3:16."],
            [[1, 1, 1, 0], "Next"],
            [[1, 1, 1, 2], "line."],
        ],
        "start_path": [1, 1, 0, 0],
        "start_utf16": 3,
        "end_path": [1, 1, 1, 2],
        "end_utf16": 5,
    }
    return output.getvalue(), capture


def test_browser_range_round_trips_source_paths_unicode_and_block_boundaries():
    archive, capture = case()
    mapped = run_corpus(archive, "map_browser", raw_capture=capture)
    assert mapped["text"] == "See John 3:16.\nNext\nline."
    endpoint = mapped["endpoint"]
    assert all(
        endpoint[key] == value for key, value in capture["original"].items() if key != "locators"
    )
    locator = endpoint["locators"][0]
    assert locator["start_path"] == capture["start_path"]
    assert locator["end_path"] == capture["end_path"]
    assert locator["start_offset"] == 2 and locator["end_offset"] == 5
    assert locator["text"]["start"] == 2
    assert run_corpus(archive, "verify_html", raw_endpoint=endpoint) == {"verified": True}
    assert run_corpus(archive, "retrieve_html", raw_endpoint=endpoint) == {"text": mapped["text"]}


@pytest.mark.parametrize(
    "change",
    [
        "text",
        "path",
        "missing",
        "order",
        "surrogate",
        "outside",
        "excluded",
        "reverse",
        "empty",
        "asset",
    ],
)
def test_unresolvable_browser_selection_rejects_without_relocation(change):
    archive, capture = case()
    if change == "text":
        capture["captured_nodes"][0][1] = "changed"
    elif change == "path":
        capture["captured_nodes"][0][0] = [2, 1, 0, 0]
    elif change == "missing":
        capture["captured_nodes"].pop()
    elif change == "order":
        capture["captured_nodes"].reverse()
    elif change == "surrogate":
        capture["start_utf16"] = 1
    elif change == "outside":
        capture["end_utf16"] = 6
    elif change == "excluded":
        capture["start_path"] = [1, 0, 0, 0]
    elif change == "reverse":
        capture["start_path"], capture["end_path"] = capture["end_path"], capture["start_path"]
    elif change == "empty":
        capture["end_path"] = capture["start_path"]
        capture["end_utf16"] = capture["start_utf16"]
    else:
        capture["original"]["document_id"] = "sources/missing.html"
    with pytest.raises(CorpusProcessError):
        run_corpus(archive, "map_browser", raw_capture=capture)


@pytest.mark.parametrize(
    "change", ["parser", "quote", "context", "offset", "stream", "normalization", "path"]
)
def test_saved_html_range_is_reverified(change):
    archive, capture = case()
    endpoint = run_corpus(archive, "map_browser", raw_capture=capture)["endpoint"]
    locator = endpoint["locators"][0]
    if change == "parser":
        locator["parser_version"] = "old"
    elif change == "quote":
        locator["text"]["exact"] = "wrong"
    elif change == "context":
        locator["text"]["prefix"] = "wrong"
    elif change == "offset":
        locator["start_offset"] += 1
    elif change == "stream":
        locator["text"]["stream_id"] = "other"
    elif change == "normalization":
        locator["text"]["normalization"] = "collapse-whitespace"
    else:
        locator["start_path"] = [1, 1, 0, 2]  # comment
    with pytest.raises(CorpusProcessError):
        run_corpus(archive, "retrieve_html", raw_endpoint=endpoint)


def test_capture_transport_limits():
    _, capture = case()
    for field, value in (("start_utf16", True), ("start_path", [-1]), ("end_path", [0] * 129)):
        changed = {**capture, field: value}
        with pytest.raises(ValueError):
            BrowserSelectionRequest.model_validate(changed)
    capture["captured_nodes"][0][1] = "😀" * 100_000
    with pytest.raises(ValueError, match="size limit"):
        BrowserSelectionRequest.model_validate(capture)


@pytest.mark.parametrize("character", ["x", '"', "\\", "😀", "\tx"])
def test_largest_mapped_endpoints_fit_reference_save_with_maximum_metadata(character):
    # Derive an actual boundary, including escape expansion and source metadata.
    def mapped(count):
        text = character * count
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "sources/book.html", "<html><head></head><body><p>" + text + "</p></body></html>"
            )
        _, capture = case()
        capture.update(
            captured_nodes=[[[0, 1, 0, 0], text]],
            start_path=[0, 1, 0, 0],
            end_path=[0, 1, 0, 0],
            start_utf16=0,
            end_utf16=len(text.encode("utf-16-le")) // 2,
        )
        return map_browser_in_corpus(output.getvalue(), capture)["endpoint"]

    low, high = 1, 32768
    while low < high:
        middle = (low + high + 1) // 2
        try:
            mapped(middle)
            low = middle
        except ValueError:
            high = middle - 1
    endpoint = mapped(low)
    assert len(json.dumps(endpoint, ensure_ascii=False).encode()) <= MAX_MAPPED_ENDPOINT_BYTES
    with pytest.raises(ValueError, match="reference-save size budget"):
        mapped(low + 1)
    saved = LinkSave(
        reference_id=uuid4(),
        source=endpoint,
        target=endpoint,
        reason="\u0001" * 2000,
        relationship="\u0001" * 128,
        expected_version=2**63 - 2,
    )
    assert len(json.dumps(saved.model_dump(mode="json"), ensure_ascii=False).encode()) <= 32768


def test_api_checks_project_access_before_loading_and_preserves_identity(monkeypatch):
    app = importlib.import_module("corpora_api.app")
    archive, capture = case()
    project, actor, session = uuid4(), uuid4(), uuid4()
    claims = {"sub": str(actor), "session_id": str(session), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app, "verify_user_token", lambda _: claims)
    calls = []
    admitted = {"status": "ok"}

    def rpc(_self, name, params):
        assert name == "corpora_admit_browser_selection"
        assert params == {
            "p_actor": str(actor),
            "p_session": str(session),
            "p_token_exp": claims["exp"],
            "p_project": str(project),
            "p_corpus": capture["original"]["package_id"],
        }
        calls.append("admission")
        return admitted

    access = SimpleNamespace(
        row=SimpleNamespace(state="ready"),
        head=SimpleNamespace(
            commit_sha="a" * 40,
            archive_size=len(archive),
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        ),
    )

    def read(actual, corpus):
        assert actual == claims and str(corpus) == capture["original"]["package_id"]
        calls.append("read")
        return access

    class Hub:
        async def transfer_authorized(self, row, revision, path, *, sink, **_kwargs):
            assert row == access.row and revision == "a" * 40 and path == ".corpora/source.corpus"
            calls.append("transfer")
            await sink(archive)

    monkeypatch.setattr(project_link_api.CreationRPC, "call", rpc)
    monkeypatch.setattr(project_document, "read_access", read)
    monkeypatch.setattr(project_document.corpus_detail_api, "hub_reader", Hub())
    client = TestClient(app.app)
    path = f"/v1/projects/{project}/browser-selection"
    headers = {"Authorization": "Bearer valid"}
    assert client.post(path, json=capture).status_code == 401
    response = client.post(path, headers=headers, json=capture)
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "See John 3:16.\nNext\nline."
    assert response.headers["cache-control"] == "no-store"
    assert calls == ["admission", "read", "transfer"]
    admitted = {"status": "not_found"}
    calls.clear()
    assert client.post(path, headers=headers, json=capture).status_code == 404
    assert calls == ["admission"]
    admitted = {"status": "ok"}
    stale = copy.deepcopy(capture)
    stale["original"]["revision"] = "b" * 40
    assert client.post(path, headers=headers, json=stale).status_code == 409
    changed = copy.deepcopy(capture)
    changed["captured_nodes"][0][1] = "wrong"
    invalid = client.post(path, headers=headers, json=changed)
    assert invalid.status_code == 422 and "recapture" in invalid.json()["detail"]
    access.head.archive_sha256 = "0" * 64
    assert client.post(path, headers=headers, json=capture).status_code == 503
