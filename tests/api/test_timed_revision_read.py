from __future__ import annotations

import base64
import hashlib
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from corpora_api.timed_revision_read import TimedRevisionReader


def test_worker_receives_only_parent_validated_fact_and_secret_via_stdin(monkeypatch):
    owner, revision = str(uuid4()), str(uuid4())
    data, key = b"verified bytes", "fixture-secret-key"
    fact = {
        "owner": owner,
        "filename": "one.corpus",
        "revision": revision,
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "version": "v1.0",
    }
    calls = []

    class Registry:
        def _rpc(self, name, payload):
            assert name == "corpora_stored_get"
            assert payload == {"p_owner": owner, "p_filename": "one.corpus", "p_revision": revision}
            return fact

        def _config(self):
            return "http://example.test", {"apikey": key}

    class Worker:
        returncode = 0

        def __init__(self, argv, **kwargs):
            assert key not in repr(argv)
            assert key not in repr(kwargs["env"])
            calls.append("spawn")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def communicate(self, payload, timeout):
            request = json.loads(payload)
            assert request["key"] == key and request["fact"] == fact
            assert timeout == 1
            return json.dumps({"archive": base64.b64encode(data).decode()}), None

    monkeypatch.setattr("corpora_api.timed_revision_read.subprocess.Popen", Worker)
    reader = TimedRevisionReader(Registry(), timeout=1)
    result = reader.get(owner, "one.corpus", revision)
    assert result.data == data and str(result.fact.revision) == revision
    fact["owner"] = str(uuid4())
    with pytest.raises(HTTPException) as foreign:
        reader.get(owner, "one.corpus", revision)
    assert foreign.value.status_code == 503 and calls == ["spawn"]


def test_timeout_configuration_is_bounded():
    for value in (0, -1, 31, float("inf"), True):
        with pytest.raises(ValueError):
            TimedRevisionReader(object(), timeout=value)
