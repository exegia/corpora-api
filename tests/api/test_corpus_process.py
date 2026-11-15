"""A failed native worker cannot crash the API or expose stderr."""

import subprocess
from types import SimpleNamespace

import pytest

from corpora_api.corpus_process import CorpusProcessError, run_corpus


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
