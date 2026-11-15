"""Opt-in real PostgreSQL test for session revocation and linking writes."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from corpora_linking import Endpoint, Provenance, Reference
from psycopg.types.json import Jsonb

from corpora_api import linking_postgres
from corpora_api.auth import UserAuthError
from corpora_api.linking_access import endpoint_scope, evidence_endpoints

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"),
    reason="set CORPORA_TEST_POSTGRES_BIN for isolated real PostgreSQL tests",
)


@pytest.mark.parametrize("revocation", ["expire", "delete"])
def test_session_revocation_waits_for_linking_write(monkeypatch, revocation):
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    fixture = Path(__file__).resolve().parents[1] / "fixtures/reference-linking-proposal.sql"
    with tempfile.TemporaryDirectory(prefix="corpora-linking-pg-", dir="/tmp") as temp:
        cluster = Path(temp) / "data"
        init = [str(binaries / "initdb"), "-D", str(cluster), "-U", "postgres", "-A", "trust"]
        if share := os.getenv("CORPORA_TEST_POSTGRES_SHARE"):
            init.extend(["-L", share])
        subprocess.run(init, check=True, capture_output=True, text=True, timeout=60)
        ctl = str(binaries / "pg_ctl")
        options = shlex.join(["-k", temp, "-p", "55432", "-c", "listen_addresses="])
        subprocess.run(
            [ctl, "-D", str(cluster), "-l", str(Path(temp) / "server.log"), "-o", options, "start"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            dsn = f"host={temp} port=55432 dbname=postgres user=postgres"
            service_dsn = psycopg.conninfo.make_conninfo(dsn, options="-c role=service_role")
            space, actor, session = uuid4(), uuid4(), uuid4()
            reference = Reference(
                source=Endpoint(work_id="source"),
                target=Endpoint(work_id="target"),
                provenance=Provenance(origin="manual", agent_id="untrusted", method="test"),
            )
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS"
                )
                db.execute(
                    "CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY); "
                    "CREATE TABLE auth.sessions(id uuid PRIMARY KEY, "
                    "user_id uuid REFERENCES auth.users(id), "
                    "not_after timestamptz)"
                )
                db.execute(fixture.read_text())
                db.execute("INSERT INTO auth.users VALUES (%s)", (actor,))
                db.execute("INSERT INTO auth.sessions VALUES (%s,%s,NULL)", (session, actor))
                db.execute(
                    "INSERT INTO reference_working.spaces(id,authority_id) VALUES (%s,'test')",
                    (space,),
                )
                db.execute(
                    "INSERT INTO reference_working.memberships VALUES (%s,%s,'contribute')",
                    (space, actor),
                )
                for endpoint in evidence_endpoints(reference):
                    key, scope = endpoint_scope(endpoint)
                    db.execute(
                        "INSERT INTO reference_working.resource_access VALUES (%s,%s,%s,%s)",
                        (space, actor, key, Jsonb(scope)),
                    )
                permissions = db.execute(
                    "SELECT has_column_privilege("
                    "'service_role','auth.sessions','not_after','UPDATE'), "
                    "has_column_privilege('service_role','auth.sessions','user_id','UPDATE'), "
                    "has_table_privilege('service_role','auth.sessions','UPDATE')"
                ).fetchone()
                assert permissions == (True, False, False)
            monkeypatch.setattr(
                linking_postgres,
                "verify_user_token",
                lambda _: {
                    "sub": str(actor),
                    "session_id": str(session),
                },
            )
            store = linking_postgres.PostgreSQLReferenceStore(
                service_dsn, space_id=space, token="test"
            )
            original_check = store._check_access
            authorized, release, revoker_started = (threading.Event() for _ in range(3))
            errors, results, revoker_pid = [], [], []

            def check_and_wait(*args):
                original_check(*args)
                if not authorized.is_set():
                    authorized.set()
                    if not release.wait(15):
                        raise TimeoutError("write was not released")

            monkeypatch.setattr(store, "_check_access", check_and_wait)

            def write():
                try:
                    results.append(
                        store._write(reference, expected_version=None, reason="test", action="edit")
                    )
                except Exception as error:
                    errors.append(error)
                    authorized.set()

            def revoke():
                try:
                    with psycopg.connect(dsn) as db:
                        revoker_pid.append(db.info.backend_pid)
                        revoker_started.set()
                        if revocation == "delete":
                            db.execute("DELETE FROM auth.sessions WHERE id=%s", (session,))
                        else:
                            db.execute(
                                "UPDATE auth.sessions SET not_after=now()-interval '1 second' "
                                "WHERE id=%s",
                                (session,),
                            )
                except Exception as error:
                    errors.append(error)
                    revoker_started.set()

            writer = threading.Thread(target=write)
            revoker = threading.Thread(target=revoke)
            writer.start()
            try:
                assert authorized.wait(10) and not errors
                revoker.start()
                assert revoker_started.wait(10) and not errors
                deadline = time.monotonic() + 10
                with psycopg.connect(dsn, autocommit=True) as db:
                    while not db.execute(
                        "SELECT cardinality(pg_blocking_pids(%s)) > 0", (revoker_pid[0],)
                    ).fetchone()[0]:
                        if time.monotonic() >= deadline:
                            pytest.fail(
                                "session revocation did not wait for the linking transaction"
                            )
                        threading.Event().wait(0.02)
            finally:
                release.set()
                writer.join(15)
                if revoker.ident is not None:
                    revoker.join(15)
            assert not writer.is_alive() and not revoker.is_alive() and not errors
            assert results == [1]
            with pytest.raises(UserAuthError):
                store._write(
                    reference, expected_version=1, reason="after revocation", action="edit"
                )
            with psycopg.connect(dsn) as db:
                assert (
                    db.execute("SELECT count(*) FROM reference_working.revisions").fetchone()[0]
                    == 1
                )
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "fast", "stop"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
