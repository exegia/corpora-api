"""Cross-branch check of the actual session guard and timed Hub composition."""

from __future__ import annotations

import importlib.util
import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from corpora_api import timed_corpus_hub_provision as timed
from corpora_api.corpus_hub_provision import CorpusHubProvisionUncertain, VerifiedPrivateRepository

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN") or not os.getenv("CORPORA_SESSION_GUARD_ROOT"),
    reason="requires disposable PostgreSQL and the session guard checkout",
)


def test_timeout_keeps_real_session_lock_until_worker_stops(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    guard_root = Path(os.environ["CORPORA_SESSION_GUARD_ROOT"])
    spec = importlib.util.spec_from_file_location(
        "local_session_guard", guard_root / "src/corpora_api/session_read.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    with tempfile.TemporaryDirectory(prefix="hub-session-", dir="/tmp") as temporary:
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
            owner, session, corpus = (str(uuid4()) for _ in range(3))
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
                migration = (
                    guard_root / "supabase/migrations/20261115165514_stored_session_reads.sql"
                )
                db.execute(migration.read_text())
                db.execute("INSERT INTO auth.sessions(id,user_id) VALUES (%s,%s)", (session, owner))
            pids, calls, errors = [], [], []
            ready, release, revoked = threading.Event(), threading.Event(), threading.Event()

            def connect():
                db = psycopg.connect(dsn, autocommit=True)
                db.execute("SET ROLE service_role")
                pids.append(db.info.backend_pid)
                return db

            def slow_worker(actual_owner, actual_corpus, *, timeout, deadline, cancelled):
                assert (actual_owner, actual_corpus) == (owner, corpus)
                calls.append(True)
                ready.set()
                assert release.wait(15)
                return VerifiedPrivateRepository(
                    UUID(corpus), 1, "fixture-org/corpus-" + corpus, "dataset"
                )

            monkeypatch.setattr(timed, "_provision_with_deadline", slow_worker)
            lease = module.SessionBoundRead(connect)
            claims = {"sub": owner, "session_id": session, "exp": int(time.time()) + 120}
            with pytest.raises(CorpusHubProvisionUncertain):
                timed.provision_under_session(lease, claims, corpus, timeout=0.2)
            assert ready.is_set() and calls == [True]

            def logout():
                try:
                    with psycopg.connect(dsn, autocommit=True) as db:
                        db.execute("DELETE FROM auth.sessions WHERE id=%s", (session,))
                    revoked.set()
                except Exception as error:
                    errors.append(error)

            revoker = threading.Thread(target=logout)
            revoker.start()
            try:
                with psycopg.connect(dsn, autocommit=True) as db:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        blocked = db.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE %s = ANY(pg_blocking_pids(pid)))",
                            (pids[0],),
                        ).fetchone()[0]
                        if blocked:
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("logout was not held behind the live worker lease")
            finally:
                release.set()
                revoker.join(15)
            assert not revoker.is_alive() and revoked.is_set() and not errors
            with pytest.raises(HTTPException) as denied:
                timed.provision_under_session(lease, claims, corpus, timeout=5)
            assert denied.value.status_code == 401 and calls == [True]

            # Document the remaining distributed gap: a disconnected lock
            # holder cannot fence an external service that does not validate
            # a database fencing token. No real Hub request is made here.
            other_session = str(uuid4())
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "INSERT INTO auth.sessions(id,user_id) VALUES (%s,%s)", (other_session, owner)
                )
            pids.clear()
            ready.clear()
            release.clear()
            simulated_effects, failures = [], []

            def disconnected_worker(_owner, _corpus, *, timeout, deadline, cancelled):
                ready.set()
                assert release.wait(15)
                simulated_effects.append("after-logout")
                return VerifiedPrivateRepository(
                    UUID(corpus), 1, "fixture-org/corpus-" + corpus, "dataset"
                )

            monkeypatch.setattr(timed, "_provision_with_deadline", disconnected_worker)
            new_claims = {"sub": owner, "session_id": other_session, "exp": int(time.time()) + 120}

            def request():
                try:
                    timed.provision_under_session(lease, new_claims, corpus, timeout=5)
                except Exception as error:
                    failures.append(error)

            request_thread = threading.Thread(target=request)
            request_thread.start()
            try:
                assert ready.wait(10) and pids
                with psycopg.connect(dsn, autocommit=True) as db:
                    assert db.execute("SELECT pg_terminate_backend(%s)", (pids[0],)).fetchone()[0]
                    db.execute("DELETE FROM auth.sessions WHERE id=%s", (other_session,))
                assert not simulated_effects
            finally:
                release.set()
                request_thread.join(10)
            assert not request_thread.is_alive() and simulated_effects == ["after-logout"]
            assert len(failures) == 1 and isinstance(failures[0], CorpusHubProvisionUncertain)
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "immediate", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )
