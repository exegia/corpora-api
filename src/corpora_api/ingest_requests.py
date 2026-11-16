"""Reserve retry identities before generating nondeterministic ingestion graphs."""

from __future__ import annotations

import hashlib
import json
import uuid

from corpora_api.ingest_store import IngestStore, IngestStoreError


class IngestRequestConflict(Exception):
    """A retry key was reused with different input."""


def input_fingerprint(
    filename: str, source: bytes, *, name: str, language: str, corpus_id: str
) -> str:
    """Hash all effective input with unambiguous framing and an explicit version."""
    options = json.dumps(
        ["corpora-ingest-v1", filename, name, language, corpus_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(len(options).to_bytes(8, "big"))
    digest.update(options)
    digest.update(source)
    return digest.hexdigest()


def reserve_request(store: IngestStore, owner: str, key: str, input_hash: str) -> str:
    """Return the unique job ID for an owner's retry key and exact input.

    Failure to read back an uncertain insert fails closed; a later retry can
    recover the reservation if the original insert eventually committed.
    """
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or not key.isascii():
        raise ValueError("Idempotency-Key must contain 1 to 128 ASCII characters")
    if any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("Idempotency-Key must contain visible ASCII characters")
    owner = store._owner(owner)
    url, credentials, _bucket = store._config()
    key_hash = hashlib.sha256(key.encode("ascii")).hexdigest()
    job_id = str(uuid.uuid4())
    row = {
        "owner_id": owner,
        "request_key_hash": key_hash,
        "input_hash": input_hash,
        "job_id": job_id,
    }
    endpoint = f"{url}/rest/v1/corpora_ingest_requests"
    try:
        response = store._request(
            "POST",
            endpoint,
            headers={**store._headers(credentials), "Prefer": "return=minimal"},
            json=row,
        )
        if response.status_code in (200, 201, 204):
            return job_id
    except IngestStoreError:
        pass
    response = store._request(
        "GET",
        endpoint,
        headers=store._headers(credentials),
        params={
            "owner_id": f"eq.{owner}",
            "request_key_hash": f"eq.{key_hash}",
            "select": "owner_id,request_key_hash,input_hash,job_id",
            "limit": "1",
        },
    )
    try:
        rows = response.json()
        if response.status_code != 200 or not isinstance(rows, list) or len(rows) != 1:
            raise ValueError
        saved = rows[0]
        if (
            not isinstance(saved, dict)
            or saved.get("owner_id") != owner
            or saved.get("request_key_hash") != key_hash
        ):
            raise ValueError
        if saved.get("input_hash") != input_hash:
            raise IngestRequestConflict("Idempotency-Key already used for different input")
        return str(uuid.UUID(saved["job_id"]))
    except (ValueError, KeyError, TypeError) as error:
        raise IngestStoreError("Could not confirm ingestion request reservation") from error
