from __future__ import annotations

from contextlib import nullcontext
from uuid import uuid4

import pytest
from fastapi import HTTPException

from corpora_api.session_read import SessionBoundRead
from corpora_api.storage_detail import MAX_STORED_ARCHIVE_BYTES


def test_invalid_claims_fail_before_connecting_or_reading():
    connected, read = [], []
    guard = SessionBoundRead(lambda: connected.append(True))
    for claims in (
        {},
        {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": True},
        {"sub": str(uuid4()), "session_id": "bad", "exp": 123},
    ):
        with pytest.raises(HTTPException) as error:
            guard.read(claims, lambda _owner: read.append(True))
        assert error.value.status_code == 401
    assert not connected and not read


def test_oversized_read_fails_without_returning_bytes():
    class Database:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return nullcontext()

        def execute(self, *_args):
            self.calls += 1

    db = Database()
    guard = SessionBoundRead(lambda: db)
    claims = {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": 2_000_000_000}
    with pytest.raises(HTTPException) as error:
        guard.read(claims, lambda _owner: b"x" * (MAX_STORED_ARCHIVE_BYTES + 1))
    assert error.value.status_code == 413 and db.calls == 1
