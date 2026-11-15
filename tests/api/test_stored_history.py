from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from corpora_api.stored_history import stored_history


def test_history_preserves_rows_but_only_marks_unambiguous_owned_facts(monkeypatch):
    good, duplicate, wrong_version, unknown, head = (uuid4() for _ in range(5))
    rows = [
        {
            "id": "v1.0",
            "snapshot_revision": str(good),
            "notes": ["source"],
            "registered_revision": "forged",
        },
        {"id": "v1.1", "snapshot_revision": str(duplicate)},
        {"id": "v1.1", "snapshot_revision": str(duplicate)},
        {"id": "v1.9", "snapshot_revision": str(wrong_version)},
        {"id": "legacy", "snapshot_revision": str(unknown)},
        {"id": "imported", "registered_revision": "forged"},
        {"id": "v1.0", "snapshot_revision": str(good).upper()},
        "legacy opaque entry",
    ]
    monkeypatch.setattr(
        "corpora_api.stored_history.run_corpus", lambda *_a, **_k: {"versions": rows}
    )
    facts = {
        good: SimpleNamespace(revision=good, version="1.0"),
        wrong_version: SimpleNamespace(revision=wrong_version, version="v1.2"),
    }
    store = SimpleNamespace(
        get=lambda *_: SimpleNamespace(data=b"archive", fact=SimpleNamespace(revision=head)),
        facts=lambda _owner, _filename, selected: {
            key: facts[key] for key in selected if key in facts
        },
    )
    result = stored_history(store, str(uuid4()), "one.corpus")
    assert result["head_revision"] == str(head)
    assert result["versions"][0]["registered_revision"] == str(good)
    assert result["versions"][0]["notes"] == ["source"]
    assert all(result["versions"][i]["registered_revision"] is None for i in range(1, 7))
    assert result["versions"][-1] == "legacy opaque entry"


def test_history_rejects_unbounded_legacy_rows(monkeypatch):
    monkeypatch.setattr(
        "corpora_api.stored_history.run_corpus", lambda *_a, **_k: {"versions": [{}] * 501}
    )
    store = SimpleNamespace(get=lambda *_: SimpleNamespace(data=b"archive", fact=None))
    with pytest.raises(HTTPException) as error:
        stored_history(store, str(uuid4()), "one.corpus")
    assert error.value.status_code == 413


def test_history_resolves_facts_in_bounded_batches(monkeypatch):
    ids = [uuid4() for _ in range(205)]
    rows = [{"id": "v1.0", "snapshot_revision": str(value)} for value in ids]
    monkeypatch.setattr(
        "corpora_api.stored_history.run_corpus", lambda *_a, **_k: {"versions": rows}
    )
    batch_sizes = []

    def facts(_owner, _filename, selected):
        batch_sizes.append(len(selected))
        return {value: SimpleNamespace(revision=value, version="1.0") for value in selected}

    store = SimpleNamespace(
        get=lambda *_: SimpleNamespace(data=b"archive", fact=SimpleNamespace(revision=uuid4())),
        facts=facts,
    )
    result = stored_history(store, str(uuid4()), "one.corpus")
    assert batch_sizes == [100, 100, 5]
    assert [row["registered_revision"] for row in result["versions"]] == [
        str(value) for value in ids
    ]
