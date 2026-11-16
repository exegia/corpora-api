"""A failed native worker cannot crash the API or expose stderr."""

import io
import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from corpora_api import ai_validation, corpus_process
from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.models import AINodeScope


def test_worker_output_limit_counts_encoded_bytes_and_returns_no_partial_json(monkeypatch):
    monkeypatch.setattr(corpus_process, "MAX_CORPUS_WORKER_RESPONSE_BYTES", 45)
    assert json.loads(corpus_process._bounded_response({"result": {"value": "ok"}})) == {
        "result": {"value": "ok"}
    }
    result = json.loads(corpus_process._bounded_response({"result": {"value": "😀" * 15}}))
    assert result == {"error": "too_large", "message": "Corpus result exceeds limit"}


def test_internal_mutation_output_has_a_separate_bounded_encoding_budget():
    result = {"result": {"data": "x" * 4_000_000}}
    assert json.loads(corpus_process._bounded_response(result))["error"] == "too_large"
    encoded = corpus_process._bounded_response(
        result, max_bytes=corpus_process.MAX_MUTATION_WORKER_RESPONSE_BYTES
    )
    assert len(json.loads(encoded)["result"]["data"]) == 4_000_000


def test_mutation_worker_uses_internal_archive_limit(monkeypatch, capsys):
    from corpora_api import archive_mutation

    monkeypatch.setattr(
        archive_mutation, "run_archive_mutation", lambda *_a, **_k: {"data": "x" * 4_000_000}
    )
    monkeypatch.setattr(
        corpus_process.sys,
        "stdin",
        io.StringIO(json.dumps({"data": "", "operation": "mutation_edit", "args": {}})),
    )
    corpus_process._main()
    assert len(json.loads(capsys.readouterr().out)["result"]["data"]) == 4_000_000


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
        ai_validation.validate_scope(None, None, "owner", scope)
    assert caught.value.status_code == 422
    assert caught.value.detail == "Invalid scope or unreadable corpus"
