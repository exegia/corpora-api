"""Run against a fresh local PostgreSQL cluster when explicitly configured."""

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


def test_registry_atomic_history_owner_and_concurrent_writers():
    psycopg = pytest.importorskip("psycopg")
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    migration = (
        Path(__file__).resolve().parents[2]
        / "supabase/migrations/20261115160000_stored_archive_revision_registry.sql"
    )
    with tempfile.TemporaryDirectory(prefix="stored-registry-", dir="/tmp") as temporary:
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
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS"
                )
                db.execute(migration.read_text())
            service = psycopg.conninfo.make_conninfo(dsn, options="-c role=service_role")
            owner, other, initial, first, second = (str(uuid4()) for _ in range(5))
            digest = "sha256:" + "a" * 64
            filename = "example.corpus"
            with psycopg.connect(service) as db:
                db.execute(
                    "SELECT public.corpora_stored_create(%s,%s,%s,%s,%s)",
                    (owner, filename, initial, digest, "v1.0"),
                )
            for name, bad_digest in (("../other.corpus", digest), ("other.corpus", "bad")):
                with psycopg.connect(service) as db, pytest.raises(psycopg.errors.CheckViolation):
                    db.execute(
                        "SELECT public.corpora_stored_create(%s,%s,%s,%s,%s)",
                        (owner, name, str(uuid4()), bad_digest, "v1.0"),
                    )
            with psycopg.connect(service) as db, pytest.raises(psycopg.errors.UniqueViolation):
                db.execute(
                    "SELECT public.corpora_stored_create(%s,%s,%s,%s,%s)",
                    (owner, filename, str(uuid4()), digest, "v1.0"),
                )
            with (
                psycopg.connect(service) as db,
                pytest.raises(psycopg.errors.InsufficientPrivilege),
            ):
                db.execute("DELETE FROM public.corpora_stored_revisions WHERE owner=%s", (owner,))
            for role in ("anon", "authenticated"):
                role_dsn = psycopg.conninfo.make_conninfo(dsn, options=f"-c role={role}")
                with (
                    psycopg.connect(role_dsn) as db,
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                ):
                    db.execute("SELECT * FROM public.corpora_stored_revisions")
                with (
                    psycopg.connect(role_dsn) as db,
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                ):
                    db.execute(
                        "SELECT public.corpora_stored_publish(%s,%s,%s,%s,%s,%s)",
                        (owner, filename, initial, first, digest, "v1.1"),
                    )
            with (
                psycopg.connect(service) as db,
                pytest.raises(psycopg.errors.RaiseException, match="ARCHIVE_NOT_FOUND"),
            ):
                db.execute(
                    "SELECT public.corpora_stored_publish(%s,%s,%s,%s,%s,%s)",
                    (other, filename, initial, first, digest, "v1.1"),
                )

            locked, release, second_started = (threading.Event() for _ in range(3))
            failures, outcomes, second_pid = [], [], []

            def writer(revision, hold):
                try:
                    with psycopg.connect(service) as db:
                        if not hold:
                            second_pid.append(db.info.backend_pid)
                            second_started.set()
                        try:
                            db.execute(
                                "SELECT public.corpora_stored_publish(%s,%s,%s,%s,%s,%s)",
                                (owner, filename, initial, revision, digest, "v1.1"),
                            )
                            if hold:
                                locked.set()
                                if not release.wait(15):
                                    raise TimeoutError("first writer was not released")
                            outcomes.append("committed")
                        except psycopg.errors.RaiseException as error:
                            outcomes.append(str(error).splitlines()[0])
                except Exception as error:
                    failures.append(error)
                    locked.set()
                    second_started.set()

            one = threading.Thread(target=writer, args=(first, True))
            two = threading.Thread(target=writer, args=(second, False))
            one.start()
            try:
                assert locked.wait(15) and not failures
                two.start()
                assert second_started.wait(15) and not failures
                with psycopg.connect(dsn) as db:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        waiting = db.execute(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                            (second_pid[0],),
                        ).fetchone()
                        if waiting and waiting[0] == "Lock":
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("second writer did not wait for the head row")
            finally:
                release.set()
                one.join(20)
                if two.ident:
                    two.join(20)
            assert not one.is_alive() and not two.is_alive() and not failures
            assert sorted(outcomes) == ["ARCHIVE_CONFLICT", "committed"]
            with psycopg.connect(service) as db:
                rows = db.execute(
                    "SELECT revision FROM public.corpora_stored_revisions "
                    "WHERE owner=%s AND filename=%s",
                    (owner, filename),
                ).fetchall()
                head = db.execute(
                    "SELECT revision FROM public.corpora_stored_heads "
                    "WHERE owner=%s AND filename=%s",
                    (owner, filename),
                ).fetchone()
                assert {str(row[0]) for row in rows} == {initial, first}
                assert str(head[0]) == first
            with psycopg.connect(service) as db, pytest.raises(psycopg.errors.UniqueViolation):
                db.execute(
                    "SELECT public.corpora_stored_publish(%s,%s,%s,%s,%s,%s)",
                    (owner, filename, first, initial, digest, "v1.2"),
                )
            with psycopg.connect(service) as db:
                assert (
                    str(
                        db.execute(
                            "SELECT revision FROM public.corpora_stored_heads "
                            "WHERE owner=%s AND filename=%s",
                            (owner, filename),
                        ).fetchone()[0]
                    )
                    == first
                )
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "stop", "-m", "fast"],
                check=True,
                capture_output=True,
                timeout=30,
            )
