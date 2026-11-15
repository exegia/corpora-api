"""Verify the timed revision worker under a real local session lock."""

from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from corpora_api.auth import verify_user_token
from corpora_api.session_connection import SessionDatabase
from corpora_api.session_read import SessionBoundRead, SessionBoundTimedRevisions

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE") or not os.getenv("CORPORA_STORED_ADAPTER_ROOT"),
    reason="requires disposable local Supabase and the draft revision adapter checkout",
)


@pytest.fixture
def dedicated_session_database():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    admin_dsn = os.getenv("CORPORA_TEST_LOCAL_ROLE_ADMIN_DSN")
    if not admin_dsn:
        pytest.skip("requires explicit disposable local role administrator")
    local = conninfo_to_dict(admin_dsn)
    assert local.get("host") in {"localhost", "127.0.0.1", "::1"}
    role, password = "test_timed_reader_" + uuid4().hex, uuid4().hex + uuid4().hex
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as admin:
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password))
        )
        try:
            admin.execute(sql.SQL("GRANT service_role TO {}").format(sql.Identifier(role)))
            yield SessionDatabase(
                host=local["host"],
                hostaddr="127.0.0.1" if local["host"] == "localhost" else local["host"],
                port=int(local.get("port", 5432)),
                database=local["dbname"],
                user=role,
                password=password,
                allow_insecure_local=True,
            )
        finally:
            admin.execute(sql.SQL("REVOKE service_role FROM {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_timed_worker_is_reaped_before_logout_and_validates_real_storage(
    tmp_path, monkeypatch, dedicated_session_database
):
    psycopg = pytest.importorskip("psycopg")
    import corpora_api
    import corpora_api.timed_revision_read as timed_module

    adapter = Path(os.environ["CORPORA_STORED_ADAPTER_ROOT"]) / "src/corpora_api"
    monkeypatch.setattr(corpora_api, "__path__", [*corpora_api.__path__, str(adapter)])
    revisions_module = importlib.import_module("corpora_api.stored_revisions")
    assert Path(revisions_module.__file__).resolve() == (adapter / "stored_revisions.py").resolve()
    Revision, StoredRevisions = revisions_module.Revision, revisions_module.StoredRevisions

    # Build a disposable package view combining the two unmerged draft branches.
    package = tmp_path / "worker/corpora_api"
    package.mkdir(parents=True)
    for item in adapter.iterdir():
        if item.name != "__pycache__":
            (package / item.name).symlink_to(item, target_is_directory=item.is_dir())
    (package / "timed_revision_read.py").symlink_to(Path(timed_module.__file__).resolve())
    monkeypatch.setenv("PYTHONPATH", str(package.parent))

    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    headers = {"apikey": service, "Authorization": "Bearer " + service}
    backend_pid, worker_pids = [], []
    original_popen = subprocess.Popen

    def capture_popen(argv, **kwargs):
        assert service not in repr(argv) and service not in repr(kwargs["env"])
        assert "SUPABASE_SERVICE_ROLE_KEY" not in kwargs["env"]
        process = original_popen(argv, **kwargs)
        worker_pids.append(process.pid)
        return process

    monkeypatch.setattr(timed_module.subprocess, "Popen", capture_popen)

    def connect():
        db = dedicated_session_database.connect()
        backend_pid.append(db.info.backend_pid)
        return db

    with httpx.Client(timeout=15) as client:
        response = client.post(
            url + "/auth/v1/signup",
            headers={"apikey": anon, "Authorization": "Bearer " + anon},
            json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
        )
        assert response.status_code == 200
        token = response.json()["access_token"]
        claims = verify_user_token(token)
        data = b"registered snapshot"
        fact = Revision(
            owner=claims["sub"],
            filename="timed.corpus",
            revision=uuid4(),
            digest="sha256:" + hashlib.sha256(data).hexdigest(),
            version="v1.0",
        )
        store = StoredRevisions(client)
        store.publish(fact, data, None)
        lease = SessionBoundRead(connect, assume_service_role=True)
        actual = SessionBoundTimedRevisions(lease, store, timeout=10)
        snapshot = actual.get(claims, "timed.corpus", str(fact.revision))
        assert snapshot.data == data and snapshot.fact.revision == fact.revision
        object_url = f"{url}/storage/v1/object/corpus-stored-revisions/{store._key(fact)}"
        assert client.put(object_url, headers=headers, content=b"tampered").status_code == 200
        with pytest.raises(HTTPException) as corrupted:
            actual.get(claims, "timed.corpus", str(fact.revision))
        assert corrupted.value.status_code == 409
        assert client.put(object_url, headers=headers, content=data).status_code == 200

        ready, results, logout_status, errors = threading.Event(), [], [], []

        class SlowStorage(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                ready.set()
                time.sleep(8)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowStorage)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        class SlowConfig:
            def _config(self):
                return f"http://127.0.0.1:{server.server_port}", {"apikey": "local-fixture-key"}

        guarded = SessionBoundTimedRevisions(lease, SlowConfig(), timeout=3)
        backend_pid.clear()
        start = time.monotonic()

        def read():
            try:
                guarded.get(claims, "timed.corpus", str(fact.revision))
                results.append("unexpected success")
            except HTTPException as error:
                results.append(error.status_code)
            except Exception as error:
                errors.append(error)

        def logout():
            try:
                response = client.post(
                    url + "/auth/v1/logout",
                    headers={"apikey": anon, "Authorization": "Bearer " + token},
                )
                logout_status.append(response.status_code)
            except Exception as error:
                errors.append(error)

        reader, revoker = threading.Thread(target=read), threading.Thread(target=logout)
        reader.start()
        try:
            assert ready.wait(10) and not errors
            revoker.start()
            with psycopg.connect(dsn, autocommit=True) as monitor:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    blocked = monitor.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE %s = ANY(pg_blocking_pids(pid)))",
                        (backend_pid[0],),
                    ).fetchone()[0]
                    if blocked:
                        break
                    time.sleep(0.02)
                else:
                    pytest.fail("logout did not wait while the timed reader held the session")
        finally:
            reader.join(10)
            if revoker.ident:
                revoker.join(10)
            server.shutdown()
            server.server_close()
        assert not reader.is_alive() and not revoker.is_alive() and not errors
        assert results == [504] and logout_status in ([200], [204])
        assert time.monotonic() - start < 8
        assert worker_pids
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pids[-1], 0)
        with pytest.raises(HTTPException) as denied:
            actual.get(claims, "timed.corpus", str(fact.revision))
        assert denied.value.status_code == 401
