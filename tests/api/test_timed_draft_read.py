from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from corpora_api import timed_draft_read
from corpora_api.drafts import DraftStore


def test_worker_receives_credentials_only_via_stdin_and_checks_identity(monkeypatch):
    owner, draft, secret = str(uuid4()), str(uuid4()), "test-secret-placeholder"
    store = SimpleNamespace(_config=lambda: ("https://example.test", {"apikey": secret}))
    output = {"draft": {"id": draft}, "data": {"title": "sample"}}
    calls = []

    class Worker:
        returncode = 0

        def __init__(self, argv, **kwargs):
            assert secret not in repr(argv) and secret not in repr(kwargs["env"])
            calls.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def communicate(self, payload, timeout):
            request = json.loads(payload)
            assert (request["owner"], request["draft"], request["key"]) == (owner, draft, secret)
            assert timeout == 1
            return json.dumps(output), None

    monkeypatch.setattr(timed_draft_read.subprocess, "Popen", Worker)
    result = timed_draft_read.read_draft_timed(store, owner, draft, "manifest", {}, timeout=1)
    assert result == output
    output["draft"]["id"] = str(uuid4())
    with pytest.raises(HTTPException) as mismatch:
        timed_draft_read.read_draft_timed(store, owner, draft, "manifest", {}, timeout=1)
    assert mismatch.value.status_code == 503
    with pytest.raises(HTTPException) as bad_identity:
        timed_draft_read.read_draft_timed(store, owner, "not-a-uuid", "manifest", {}, timeout=1)
    assert bad_identity.value.status_code == 404 and len(calls) == 2


def test_timeout_kills_and_reaps_worker(monkeypatch):
    real_popen = subprocess.Popen
    workers = []

    def stalled_worker(_argv, **kwargs):
        worker = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(timed_draft_read.subprocess, "Popen", stalled_worker)
    store = SimpleNamespace(_config=lambda: ("https://example.test", {"apikey": "placeholder"}))
    with pytest.raises(HTTPException) as timeout:
        timed_draft_read.read_draft_timed(
            store, str(uuid4()), str(uuid4()), "manifest", {}, timeout=0.1
        )
    assert timeout.value.status_code == 504 and workers[0].poll() is not None


def test_real_worker_reads_owner_bound_archive(monkeypatch):
    owner, draft, revision = (str(uuid4()) for _ in range(3))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("manifest.yml", "name: Mini\nversion: '1.0'\n")
    data = archive.getvalue()
    fact = {
        "owner": owner,
        "document_id": draft,
        "corpus": f"draft:{draft}",
        "bucket": "corpus-ai-drafts",
        "object_key": f"{owner}/ai/{draft}/{revision}.corpus",
        "revision": revision,
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "version": "1.0",
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["apikey"] == "test-key-placeholder"
            assert request["p_owner"] == owner and request["p_action"] == "resolve"
            body = json.dumps(fact).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.path == "/storage/v1/object/corpus-ai-drafts/" + fact["object_key"]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SUPABASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key-placeholder")
    try:
        result = timed_draft_read.read_draft_timed(DraftStore(), owner, draft, "manifest", {})
        assert result["draft"]["id"] == draft
        assert result["data"]["name"] == "Mini"
        fact["digest"] = "sha256:" + "0" * 64
        with pytest.raises(HTTPException) as tampered:
            timed_draft_read.read_draft_timed(DraftStore(), owner, draft, "manifest", {})
        assert tampered.value.status_code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), True])
def test_timeout_is_bounded(timeout):
    with pytest.raises(ValueError):
        timed_draft_read.read_draft_timed(
            None, str(uuid4()), str(uuid4()), "manifest", {}, timeout=timeout
        )
