"""Exercise draft reads and revocation against a disposable PostgreSQL server."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from corpora_api import session_drafts
from corpora_api.session_read import SessionBoundRead

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"), reason="requires isolated PostgreSQL binaries"
)


def test_logout_waits_for_draft_read_then_revoked_session_is_denied(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="draft-session-", dir="/tmp") as temporary:
        cluster = Path(temporary) / "data"
        init = [str(binaries / "initdb"), "-D", str(cluster), "-U", "postgres", "-A", "trust"]
        if share := os.getenv("CORPORA_TEST_POSTGRES_SHARE"):
            init.extend(["-L", share])
        subprocess.run(init, check=True, capture_output=True, timeout=60)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ctl = str(binaries / "pg_ctl")
        options = shlex.join(["-k", temporary, "-p", str(port), "-c", "listen_addresses="])
        subprocess.run(
            [
                ctl,
                "-D",
                str(cluster),
                "-l",
                str(Path(temporary) / "pg.log"),
                "-o",
                options,
                "start",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            dsn = f"host={temporary} port={port} dbname=postgres user=postgres"
            owner, session, draft = (str(uuid4()) for _ in range(3))
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute("CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role")
                db.execute("CREATE SCHEMA auth; GRANT USAGE ON SCHEMA auth TO service_role")
                db.execute(
                    "CREATE TABLE auth.sessions(id uuid PRIMARY KEY, "
                    "user_id uuid, not_after timestamptz)"
                )
                db.execute(
                    "GRANT SELECT(id,user_id,not_after), UPDATE(not_after) "
                    "ON auth.sessions TO service_role"
                )
                migration = root / "supabase/migrations/20261115165514_stored_session_reads.sql"
                db.execute(migration.read_text())
                db.execute("INSERT INTO auth.sessions(id,user_id) VALUES (%s,%s)", (session, owner))

            ready, release, revoked = threading.Event(), threading.Event(), threading.Event()
            results, errors = [], []
            backend_pids = []

            def connect():
                db = psycopg.connect(dsn, autocommit=True)
                db.execute("SET ROLE service_role")
                backend_pids.append(db.info.backend_pid)
                return db

            def worker(_store, principal, document, _view, _params):
                assert (principal, document) == (owner, draft)
                ready.set()
                assert release.wait(15)
                return {"draft": {"id": draft}, "data": {"name": "Mini"}}

            monkeypatch.setattr(session_drafts, "read_draft_timed", worker)
            lease = SessionBoundRead(connect)
            claims = {"sub": owner, "session_id": session, "exp": int(time.time()) + 120}

            def read():
                try:
                    result = session_drafts.read_session_draft(
                        lease, None, claims, draft, "manifest"
                    )
                    results.append(result)
                except Exception as error:
                    errors.append(error)
                    ready.set()

            def logout():
                try:
                    with psycopg.connect(dsn, autocommit=True) as db:
                        db.execute("DELETE FROM auth.sessions WHERE id=%s", (session,))
                    revoked.set()
                except Exception as error:
                    errors.append(error)

            reader, revoker = threading.Thread(target=read), threading.Thread(target=logout)
            reader.start()
            try:
                assert ready.wait(15) and not errors
                revoker.start()
                with psycopg.connect(dsn, autocommit=True) as monitor:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        blocked = monitor.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE %s = ANY(pg_blocking_pids(pid)))",
                            (backend_pids[0],),
                        ).fetchone()[0]
                        if blocked:
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("logout did not wait for the draft read")
            finally:
                release.set()
                reader.join(20)
                if revoker.ident:
                    revoker.join(20)
            assert not reader.is_alive() and not revoker.is_alive()
            assert not errors and revoked.is_set()
            assert results[0]["data"]["name"] == "Mini"
            with pytest.raises(HTTPException) as denied:
                session_drafts.read_session_draft(lease, None, claims, draft, "manifest")
            assert denied.value.status_code == 401
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "immediate", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )
