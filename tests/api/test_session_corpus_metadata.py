"""Default-suite checks for the unwired session-bound metadata adapter."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from corpora_api.corpus_metadata import CorpusMetadataConflict, CorpusMetadataNotFound
from corpora_api.session_corpus_metadata import SessionBoundCorpusMetadata


class Lease:
    def __init__(self, db, *, revoked=False, fail_after=False):
        self.db, self.revoked, self.fail_after = db, revoked, fail_after
        self.committed = False

    def run(self, claims, operation):
        if self.revoked:
            raise HTTPException(401, "Verified session is unavailable")
        result = operation(claims["sub"], self.db)
        if self.fail_after:
            raise HTTPException(401, "Verified session is unavailable")
        self.committed = True
        return result


class Database:
    def __init__(self, row=None, visible=False):
        self.row, self.visible = row, visible
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        value = (self.row,) if self.row is not None else None
        if query.startswith("SELECT"):
            value = (1,) if self.visible else None
        return SimpleNamespace(fetchone=lambda: value)


def record(owner, corpus, *, title="Edited", revision=2):
    return {
        "id": corpus,
        "owner_id": owner,
        "title": title,
        "description": None,
        "language": None,
        "properties": {},
        "hf_repo_id": f"fixture-org/corpus-{corpus}",
        "hf_repo_type": "dataset",
        "state": "provisioning",
        "revision": revision,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_session_metadata_update_returns_only_after_lease(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    monkeypatch.delenv("CORPORA_SUPERADMIN_USER_ID", raising=False)
    db = Database(record(owner, corpus))
    lease = Lease(db)
    updated = SessionBoundCorpusMetadata(lease).update(
        {"sub": owner}, corpus, 1, {"title": " Edited "}
    )
    assert updated.title == "Edited" and updated.revision == 2 and lease.committed
    assert db.calls[0][1][-4:] == (corpus, 1, owner, False)


@pytest.mark.parametrize("visible", [False, True])
def test_session_metadata_distinguishes_stale_from_inaccessible(monkeypatch, visible):
    owner, corpus = str(uuid4()), str(uuid4())
    monkeypatch.delenv("CORPORA_SUPERADMIN_USER_ID", raising=False)
    lease = Lease(Database(visible=visible))
    expected = CorpusMetadataConflict if visible else CorpusMetadataNotFound
    with pytest.raises(expected):
        SessionBoundCorpusMetadata(lease).update({"sub": owner}, corpus, 1, {"title": "Edited"})
    assert lease.committed


def test_invalid_result_aborts_lease_and_revoked_session_precedes_validation(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    monkeypatch.delenv("CORPORA_SUPERADMIN_USER_ID", raising=False)
    lease = Lease(Database(record(owner, corpus, title="Wrong")))
    with pytest.raises(HTTPException) as invalid:
        SessionBoundCorpusMetadata(lease).update({"sub": owner}, corpus, 1, {"title": "Edited"})
    assert invalid.value.status_code == 503 and not lease.committed
    with pytest.raises(HTTPException) as revoked:
        SessionBoundCorpusMetadata(Lease(Database(), revoked=True)).update(
            {"sub": owner}, corpus, 1, {"state": "ready"}
        )
    assert revoked.value.status_code == 401


def test_session_failure_after_callback_does_not_release_result(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    monkeypatch.delenv("CORPORA_SUPERADMIN_USER_ID", raising=False)
    lease = Lease(Database(record(owner, corpus)), fail_after=True)
    with pytest.raises(HTTPException) as revoked:
        SessionBoundCorpusMetadata(lease).update({"sub": owner}, corpus, 1, {"title": "Edited"})
    assert revoked.value.status_code == 401 and not lease.committed


def test_transaction_rejects_invalid_claims_before_connecting():
    from corpora_api.session_corpus_metadata import SessionBoundMetadataTransaction

    def connect():
        pytest.fail("invalid claims opened a database connection")

    transaction = SessionBoundMetadataTransaction(connect)
    with pytest.raises(HTTPException) as caught:
        transaction.run({"sub": "invalid"}, lambda *_: b"unused")
    assert caught.value.status_code == 401


@pytest.mark.parametrize("failure_on", [1, 2])
def test_transaction_never_returns_result_when_session_guard_fails(failure_on):
    from contextlib import contextmanager

    from corpora_api.session_corpus_metadata import SessionBoundMetadataTransaction

    class GuardError(Exception):
        sqlstate = "P0001"
        diag = SimpleNamespace(message_primary="AUTH_SESSION_INVALID")

    class DB:
        checks = 0
        committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        @contextmanager
        def transaction(self):
            yield
            self.committed = True

        def execute(self, query, _params=None):
            if "lock_verified_session" in query:
                self.checks += 1
                if self.checks == failure_on:
                    raise GuardError()

    db = DB()
    owner, session = str(uuid4()), str(uuid4())
    called = []
    transaction = SessionBoundMetadataTransaction(lambda: db)
    with pytest.raises(HTTPException) as caught:
        transaction.run(
            {"sub": owner, "session_id": session, "exp": 2_000_000_000},
            lambda *_: called.append(True) or b"result",
        )
    assert caught.value.status_code == 401 and not db.committed
    assert called == ([] if failure_on == 1 else [True])


def test_session_reservation_uses_verified_owner_and_stable_link(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    db = Database(record(owner, corpus, title="Original", revision=1))
    lease = Lease(db)
    reserved = SessionBoundCorpusMetadata(lease).reserve(
        {"sub": owner}, corpus, " Original ", repo_type="dataset"
    )
    assert str(reserved.owner_id) == owner and reserved.state == "provisioning" and lease.committed
    assert db.calls[0][1] == (
        corpus,
        owner,
        "Original",
        f"fixture-org/corpus-{corpus}",
        "dataset",
    )


def test_session_reservation_conflicts_without_disclosing_owner(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    with pytest.raises(CorpusMetadataConflict):
        SessionBoundCorpusMetadata(Lease(Database())).reserve(
            {"sub": owner}, corpus, "Original", repo_type="dataset"
        )


def test_session_reservation_bad_insert_result_aborts_lease(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    row = record(owner, corpus, title="Original", revision=1)
    row["state"] = "ready"
    lease = Lease(Database(row))
    with pytest.raises(HTTPException) as bad:
        SessionBoundCorpusMetadata(lease).reserve(
            {"sub": owner}, corpus, "Original", repo_type="dataset"
        )
    assert bad.value.status_code == 503 and not lease.committed


def test_session_metadata_get_authorizes_before_return(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    monkeypatch.delenv("CORPORA_SUPERADMIN_USER_ID", raising=False)
    row = record(owner, corpus)

    class ReadDB:
        def execute(self, query, params):
            assert "owner_id" in query
            value = (row,) if params[1] == owner and not params[2] else None
            return SimpleNamespace(fetchone=lambda: value)

    reader = SessionBoundCorpusMetadata(Lease(ReadDB()))
    assert str(reader.get({"sub": owner}, corpus).id) == corpus
    with pytest.raises(CorpusMetadataNotFound):
        reader.get({"sub": str(uuid4())}, corpus)


def test_session_metadata_list_requires_owner_or_superadmin(monkeypatch):
    owner, admin, foreign = str(uuid4()), str(uuid4()), str(uuid4())
    ids = sorted((str(uuid4()), str(uuid4())))
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", admin)

    class ListDB:
        calls = []

        def execute(self, query, params):
            self.calls.append((query, params))
            selected = [record(owner, identifier) for identifier in ids]
            if len(params) == 3:
                selected = [row for row in selected if row["id"] > params[1]]
            return SimpleNamespace(fetchall=lambda: [(row,) for row in selected[: params[-1]]])

    db = ListDB()
    reader = SessionBoundCorpusMetadata(Lease(db))
    first, cursor = reader.list_owned({"sub": owner}, limit=1)
    assert [str(row.id) for row in first] == ids[:1] and cursor == ids[0]
    second, next_cursor = reader.list_owned({"sub": owner}, limit=1, after=cursor)
    assert [str(row.id) for row in second] == ids[1:] and next_cursor is None
    admin_page, _ = reader.list_owned({"sub": admin}, owner_id=owner)
    assert [str(row.id) for row in admin_page] == ids
    calls_before = len(db.calls)
    with pytest.raises(CorpusMetadataNotFound):
        reader.list_owned({"sub": foreign}, owner_id=owner)
    assert len(db.calls) == calls_before


def test_revoked_session_precedes_invalid_list_input():
    reader = SessionBoundCorpusMetadata(Lease(Database(), revoked=True))
    with pytest.raises(HTTPException) as caught:
        reader.list_owned({"sub": str(uuid4())}, limit=0)
    assert caught.value.status_code == 401
