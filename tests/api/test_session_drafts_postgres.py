"""Exercise draft reads and revocation against a disposable PostgreSQL server."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi import HTTPException

from corpora_api import session_drafts
from corpora_api.drafts import DraftStore
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

            # Staging is outside the transaction. Revocation during staging
            # must prevent publication when the final transaction starts.
            job, new_session = str(uuid4()), str(uuid4())
            document = str(uuid5(NAMESPACE_URL, json.dumps(["ai-draft", owner, job])))
            digest = "sha256:" + "a" * 64
            revision = str(uuid5(UUID(document), digest))
            payload = {
                "document_id": document,
                "corpus": "draft:" + document,
                "bucket": "corpus-ai-drafts",
                "object_key": f"{owner}/ai/{document}/{revision}.corpus",
                "revision": revision,
                "digest": digest,
                "version": "1.0",
                "job_id": job,
                "name": "Mini",
            }
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute("CREATE TABLE public.test_draft_publications(id uuid PRIMARY KEY)")
                db.execute("GRANT INSERT, SELECT ON public.test_draft_publications TO service_role")
                db.execute("""
                    CREATE FUNCTION public.corpora_ai_storage(
                        action text, principal uuid, payload jsonb)
                    RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER AS $$
                    BEGIN
                        INSERT INTO public.test_draft_publications VALUES
                            ((payload->>'document_id')::uuid);
                        IF payload->>'name' = 'slow' THEN PERFORM pg_sleep(3); END IF;
                        RETURN (payload - 'job_id' - 'name') ||
                            jsonb_build_object('owner', principal, 'state', 'draft');
                    END $$
                """)
                db.execute(
                    "GRANT EXECUTE ON FUNCTION public.corpora_ai_storage(text,uuid,jsonb) "
                    "TO service_role"
                )
                db.execute(
                    "INSERT INTO auth.sessions(id,user_id) VALUES (%s,%s)", (new_session, owner)
                )
            ready.clear()
            release.clear()
            errors.clear()
            create_results = []

            def prepare(*_args, **_kwargs):
                ready.set()
                assert release.wait(15)
                return {"provision": payload}

            monkeypatch.setattr(session_drafts, "read_draft_timed", prepare)
            jobs = SimpleNamespace(_config=lambda: ("http://local", "placeholder", "jobs"))
            validator = SimpleNamespace(executable="placeholder")
            create_claims = {"sub": owner, "session_id": new_session, "exp": int(time.time()) + 120}

            def create():
                try:
                    create_results.append(
                        session_drafts.create_session_draft(
                            lease, DraftStore(), create_claims, job, jobs, validator
                        )
                    )
                except Exception as error:
                    errors.append(error)
                    ready.set()

            creator = threading.Thread(target=create)
            creator.start()
            try:
                assert ready.wait(15) and not errors
                with psycopg.connect(dsn, autocommit=True) as db:
                    db.execute("DELETE FROM auth.sessions WHERE id=%s", (new_session,))
            finally:
                release.set()
                creator.join(20)
            assert not creator.is_alive() and not create_results
            assert len(errors) == 1 and errors[0].status_code == 401
            with psycopg.connect(dsn, autocommit=True) as db:
                rows = db.execute("SELECT count(*) FROM public.test_draft_publications").fetchone()
                assert rows == (0,)
                db.execute(
                    "INSERT INTO auth.sessions(id,user_id) VALUES (%s,%s)", (new_session, owner)
                )

            # If a token expires while the registry write is running, the
            # second session check rolls the write back with the transaction.
            payload["name"] = "slow"
            create_claims["exp"] = int(time.time()) + 2
            with pytest.raises(HTTPException) as expired:
                session_drafts.create_session_draft(
                    lease, DraftStore(), create_claims, job, jobs, validator
                )
            assert expired.value.status_code == 401
            with psycopg.connect(dsn, autocommit=True) as db:
                rows = db.execute("SELECT count(*) FROM public.test_draft_publications").fetchone()
                assert rows == (0,)
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "immediate", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )
