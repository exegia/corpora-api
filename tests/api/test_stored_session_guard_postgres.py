"""Isolated PostgreSQL checks for the draft session-locked publication."""

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

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"), reason="requires isolated PostgreSQL binaries"
)


def test_session_guard_blocks_revocation_until_publication_commits():
    psycopg = pytest.importorskip("psycopg")
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="stored-session-", dir="/tmp") as temporary:
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
            owner, other, session, initial, next_revision = (str(uuid4()) for _ in range(5))
            digest, name = "sha256:" + "a" * 64, "sample.corpus"
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS"
                )
                db.execute("CREATE SCHEMA auth; GRANT USAGE ON SCHEMA auth TO service_role")
                db.execute(
                    "CREATE TABLE auth.sessions (id uuid PRIMARY KEY, "
                    "user_id uuid NOT NULL, not_after timestamptz)"
                )
                db.execute(
                    (
                        root
                        / "supabase/migrations/20261115154949_stored_archive_revision_registry.sql"
                    ).read_text()
                )
                db.execute(
                    (
                        root / "supabase/migrations/20261115163840_stored_session_guard.sql"
                    ).read_text()
                )
                db.execute("INSERT INTO auth.sessions(id,user_id) VALUES (%s,%s)", (session, owner))
            service = psycopg.conninfo.make_conninfo(dsn, options="-c role=service_role")
            with psycopg.connect(service) as db:
                db.execute(
                    "SELECT public.corpora_stored_create(%s,%s,%s,%s,%s)",
                    (owner, name, initial, digest, "v1.0"),
                )

            def publish(
                db,
                principal=owner,
                session_id=session,
                expiry=None,
                expected=initial,
                revision=next_revision,
            ):
                return db.execute(
                    "SELECT public.corpora_stored_session_publish(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        principal,
                        session_id,
                        int(time.time()) + 60 if expiry is None else expiry,
                        name,
                        expected,
                        revision,
                        digest,
                        "v1.1",
                    ),
                )

            # No grant is applied by the migration; lack of access fails closed.
            with (
                psycopg.connect(service) as db,
                pytest.raises(psycopg.errors.InsufficientPrivilege),
            ):
                publish(db)
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute((root / "tests/fixtures/stored-session-grant-proposal.sql").read_text())
            with (
                psycopg.connect(service) as db,
                pytest.raises(psycopg.errors.InsufficientPrivilege),
            ):
                db.execute("UPDATE auth.sessions SET user_id=%s WHERE id=%s", (other, session))
            for principal, session_id, expiry in (
                (other, session, int(time.time()) + 60),
                (owner, str(uuid4()), int(time.time()) + 60),
                (owner, session, int(time.time()) - 1),
            ):
                with (
                    psycopg.connect(service) as db,
                    pytest.raises(psycopg.errors.RaiseException, match="AUTH_SESSION_INVALID"),
                ):
                    publish(db, principal, session_id, expiry)
            for role in ("anon", "authenticated"):
                browser = psycopg.conninfo.make_conninfo(dsn, options=f"-c role={role}")
                with (
                    psycopg.connect(browser) as db,
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                ):
                    publish(db)

            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "UPDATE auth.sessions SET not_after=clock_timestamp()-interval '1 second' "
                    "WHERE id=%s",
                    (session,),
                )
            with (
                psycopg.connect(service) as db,
                pytest.raises(psycopg.errors.RaiseException, match="AUTH_SESSION_INVALID"),
            ):
                publish(db)
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute("UPDATE auth.sessions SET not_after=NULL WHERE id=%s", (session,))

            # An access token can expire while waiting for the head-row lock.
            # The final expiry check must roll back the attempted publication.
            expiry, expired_outcome, expired_ready = int(time.time()) + 3, [], threading.Event()
            blocked_revision = str(uuid4())

            def expired_writer():
                try:
                    with psycopg.connect(service) as db:
                        expired_ready.set()
                        publish(db, expiry=expiry, revision=blocked_revision)
                    expired_outcome.append("published")
                except psycopg.errors.RaiseException as error:
                    expired_outcome.append(str(error).splitlines()[0])

            with psycopg.connect(dsn) as blocker:
                blocker.execute(
                    "SELECT revision FROM public.corpora_stored_heads "
                    "WHERE owner=%s AND filename=%s FOR UPDATE",
                    (owner, name),
                )
                waiting = threading.Thread(target=expired_writer)
                waiting.start()
                try:
                    assert expired_ready.wait(10)
                    with psycopg.connect(dsn, autocommit=True) as db:
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            if db.execute(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                                "WHERE %s = ANY(pg_blocking_pids(pid)))",
                                (blocker.info.backend_pid,),
                            ).fetchone()[0]:
                                break
                            time.sleep(0.05)
                        else:
                            pytest.fail("publication did not wait for the head row")
                    while time.time() <= expiry:
                        time.sleep(0.05)
                finally:
                    blocker.commit()
                    waiting.join(20)
            assert not waiting.is_alive() and expired_outcome == ["AUTH_SESSION_INVALID"]
            with psycopg.connect(service) as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM public.corpora_stored_revisions "
                        "WHERE owner=%s AND filename=%s",
                        (owner, name),
                    ).fetchone()[0]
                    == 1
                )

            ready, release, errors, outcome, writer_pid = (
                threading.Event(),
                threading.Event(),
                [],
                [],
                [],
            )

            def writer():
                try:
                    with psycopg.connect(service) as db:
                        writer_pid.append(db.info.backend_pid)
                        publish(db)
                        ready.set()
                        if not release.wait(15):
                            raise TimeoutError("writer was not released")
                    outcome.append("published")
                except Exception as error:
                    errors.append(error)
                    ready.set()

            def revoke():
                try:
                    with psycopg.connect(dsn) as db:
                        db.execute("DELETE FROM auth.sessions WHERE id=%s", (session,))
                    outcome.append("revoked")
                except Exception as error:
                    errors.append(error)

            first, second = threading.Thread(target=writer), threading.Thread(target=revoke)
            first.start()
            try:
                assert ready.wait(15) and not errors
                second.start()
                with psycopg.connect(dsn, autocommit=True) as db:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if db.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE %s = ANY(pg_blocking_pids(pid)))",
                            (writer_pid[0],),
                        ).fetchone()[0]:
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("revocation did not wait for the publication transaction")
            finally:
                release.set()
                first.join(20)
                if second.ident:
                    second.join(20)
            assert not first.is_alive() and not second.is_alive() and not errors
            assert sorted(outcome) == ["published", "revoked"]
            with (
                psycopg.connect(service) as db,
                pytest.raises(psycopg.errors.RaiseException, match="AUTH_SESSION_INVALID"),
            ):
                publish(db, expected=next_revision, revision=str(uuid4()))
            with psycopg.connect(service) as db:
                assert (
                    str(
                        db.execute(
                            "SELECT revision FROM public.corpora_stored_heads "
                            "WHERE owner=%s AND filename=%s",
                            (owner, name),
                        ).fetchone()[0]
                    )
                    == next_revision
                )
                assert (
                    db.execute(
                        "SELECT count(*) FROM public.corpora_stored_revisions "
                        "WHERE owner=%s AND filename=%s",
                        (owner, name),
                    ).fetchone()[0]
                    == 2
                )
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "stop", "-m", "fast"],
                check=True,
                capture_output=True,
                timeout=30,
            )
