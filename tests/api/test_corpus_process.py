"""A failed native worker cannot crash the API or expose stderr."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from corpora_api import ai_validation, corpus_process
from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.models import AINodeScope


def test_revision_pin_is_not_silently_ignored_for_an_archive_scope():
    with pytest.raises(ValidationError, match="corpus_revision applies only"):
        AINodeScope(
            corpus="old-archive",
            corpus_revision="a" * 40,
            level="word",
            node_id=1,
            label="word",
            version="1.0",
        )


def test_worker_output_limit_counts_encoded_bytes_and_returns_no_partial_json(monkeypatch):
    monkeypatch.setattr(corpus_process, "MAX_CORPUS_WORKER_RESPONSE_BYTES", 45)
    assert json.loads(corpus_process._bounded_response({"result": {"value": "ok"}})) == {
        "result": {"value": "ok"}
    }
    result = json.loads(corpus_process._bounded_response({"result": {"value": "😀" * 15}}))
    assert result == {"error": "too_large", "message": "Corpus result exceeds limit"}


@pytest.mark.parametrize("failure", ["crash", "timeout", "invalid-json"])
def test_worker_failure_is_sanitized(monkeypatch, failure):
    def run(*_args, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("worker", 60)
        if failure == "crash":
            return SimpleNamespace(returncode=-11, stdout="", stderr="private path")
        return SimpleNamespace(returncode=0, stdout="invalid JSON", stderr="private path")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(CorpusProcessError) as caught:
        run_corpus(b"archive", "view", view="manifest")
    assert caught.value.kind == "unavailable"
    assert "private path" not in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "attributes"),
    [
        ("invalid_scope", {"detail": "/private/path"}),
        ("scope_type_mismatch", {"actual": "/private/path"}),
        ("unknown_scope_node", {"node": 999}),
    ],
)
def test_untrusted_scope_worker_errors_are_sanitized(monkeypatch, kind, attributes):
    monkeypatch.setattr(ai_validation, "download_stored_archive", lambda *_args: b"archive")

    def reject(*_args, **_kwargs):
        raise CorpusProcessError(kind, "private path", attributes)

    monkeypatch.setattr(ai_validation, "run_corpus", reject)
    scope = AINodeScope(
        corpus="owned", level="word", node_id=1, node_type="word", label="", version="1"
    )
    with pytest.raises(HTTPException) as caught:
        ai_validation.validate_scope(None, None, "owner", scope, None)
    assert caught.value.status_code == 422
    assert caught.value.detail == "Invalid scope or unreadable corpus"


@pytest.mark.parametrize("verdict", [None, {}, {"valid": False}, {"valid": "true"}, {"valid": 1}])
def test_ai_scope_never_uses_conversion_without_literal_validation(monkeypatch, verdict):
    job = "b85f991a-8aaf-4f48-b26e-a9ef63e6a7f4"
    data = b"downloaded archive"
    jobs = SimpleNamespace(fetch=lambda owner, identifier: ("source.corpus", data))
    validator = SimpleNamespace(validate=lambda name, content: verdict)
    monkeypatch.setattr(
        ai_validation, "run_corpus", lambda *_args, **_kwargs: pytest.fail("unvalidated bytes used")
    )
    scope = AINodeScope(
        corpus="job:" + job, level="word", node_id=1, node_type="word", label="", version="1"
    )
    with pytest.raises(HTTPException) as caught:
        ai_validation.validate_scope(None, jobs, "owner", scope, validator)
    assert caught.value.status_code == 422


def test_ai_scope_passes_exact_validated_bytes_to_worker(monkeypatch):
    job = "b85f991a-8aaf-4f48-b26e-a9ef63e6a7f4"
    data = b"downloaded archive"
    seen = []
    jobs = SimpleNamespace(fetch=lambda owner, identifier: ("source.corpus", data))

    def validate(name, content):
        seen.append((name, content))
        return {"valid": True}

    def run(content, operation, **kwargs):
        assert content is data and operation == "scope"
        return {"accepted": True}

    monkeypatch.setattr(ai_validation, "run_corpus", run)
    scope = AINodeScope(
        corpus="job:" + job, level="word", node_id=1, node_type="word", label="", version="1"
    )
    assert ai_validation.validate_scope(
        None, jobs, "owner", scope, SimpleNamespace(validate=validate)
    ) == {"accepted": True}
    assert seen == [("source.corpus", data)]
