"""Opt-in isolated PostgreSQL tests for private revision evidence."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"),
    reason="set CORPORA_TEST_POSTGRES_BIN for isolated PostgreSQL tests",
)


def test_revision_lookup_scopes_facts_and_rejects_inconsistent_receipts():
    psycopg = pytest.importorskip("psycopg")
    Jsonb = pytest.importorskip("psycopg.types.json").Jsonb
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="corpora-revision-pg-", dir="/tmp") as temp:
        cluster = Path(temp) / "data"
        init = [str(binaries / "initdb"), "-D", str(cluster), "-U", "postgres", "-A", "trust"]
        if share := os.getenv("CORPORA_TEST_POSTGRES_SHARE"):
            init.extend(["-L", share])
        subprocess.run(init, check=True, capture_output=True, text=True, timeout=60)
        ctl = str(binaries / "pg_ctl")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            db_port = probe.getsockname()[1]
        options = shlex.join(["-k", temp, "-p", str(db_port), "-h", "127.0.0.1"])
        subprocess.run(
            [ctl, "-D", str(cluster), "-l", str(Path(temp) / "pg.log"), "-o", options, "start"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            dsn = f"host={temp} port={db_port} dbname=postgres user=postgres"
            owner, other, document, other_document, current, old, pending = (
                str(uuid4()) for _ in range(7)
            )
            moment = datetime.now(UTC)
            proof = "a" * 64
            current_digest, old_digest = ("sha256:" + ch * 64 for ch in ("b", "c"))
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS"
                )
                db.execute((root / "tests/fixtures/ai-hosted-bootstrap.sql").read_text())
                db.execute(
                    "CREATE TABLE auth.users(id uuid PRIMARY KEY); "
                    "ALTER TABLE public.corpus_documents ADD COLUMN name text, "
                    "ADD COLUMN source text, ADD COLUMN path text, ADD COLUMN filename text, "
                    "ADD COLUMN job_id text, ADD COLUMN status text"
                )
                db.execute("INSERT INTO auth.users VALUES (%s),(%s)", (owner, other))
                db.execute("INSERT INTO public.users VALUES (%s),(%s)", (owner, other))
                db.execute(
                    (
                        root / "supabase/migrations/20261115042900_legacy_ai_draft_registry.sql"
                    ).read_text()
                )
                migration = (
                    root / "supabase/migrations/20261115152158_ai_draft_revision_lookup.sql"
                ).read_text()
                _apply_with_running_postgrest(db, migration, Path(temp), db_port)
            service = psycopg.conninfo.make_conninfo(dsn, options="-c role=service_role")

            def provision(principal, doc, rev):
                payload = {
                    "document_id": doc,
                    "job_id": str(uuid4()),
                    "corpus": "draft:" + doc,
                    "bucket": "corpus-ai-drafts",
                    "object_key": f"{principal}/ai/{doc}/{rev}.corpus",
                    "revision": rev,
                    "digest": current_digest,
                    "version": "v1.1",
                    "name": "test",
                }
                with psycopg.connect(service) as db:
                    db.execute(
                        "SELECT public.corpora_ai_storage('provision',%s,%s)",
                        (principal, Jsonb(payload)),
                    )

            provision(owner, document, current)
            provision(other, other_document, str(uuid4()))
            operation = str(uuid4())
            intent = {
                "id": operation,
                "owner": owner,
                "suggestion": {"scope": {"corpus": "draft:" + document}},
                "plan": {
                    "before": {"revision": old, "digest": old_digest, "version": "v1.0"},
                    "after": {"revision": current, "digest": current_digest, "version": "v1.1"},
                },
            }
            receipt = {"proof": proof, "applied_at": moment.isoformat()}
            with psycopg.connect(dsn) as db:
                db.execute(
                    "INSERT INTO public.corpus_ai_operations"
                    "(id,document_id,owner,intent,proof,receipt,applied_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (operation, document, owner, Jsonb(intent), proof, Jsonb(receipt), moment),
                )
                pending_id = str(uuid4())
                pending_intent = {
                    **intent,
                    "id": pending_id,
                    "plan": {
                        **intent["plan"],
                        "after": {"revision": pending, "digest": old_digest, "version": "v1.2"},
                    },
                }
                db.execute(
                    "INSERT INTO public.corpus_ai_operations(id,document_id,owner,intent,proof) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (pending_id, document, owner, Jsonb(pending_intent), proof),
                )

            def lookup(principal, doc, rev):
                with psycopg.connect(service) as db:
                    return db.execute(
                        "SELECT public.corpora_ai_revision_lookup(%s,%s,%s)", (principal, doc, rev)
                    ).fetchone()[0]

            assert lookup(owner, document, old) == {
                "revision": old,
                "digest": old_digest,
                "version": "v1.0",
                "operations": [operation],
            }
            assert lookup(owner, document, current) == {
                "revision": current,
                "digest": current_digest,
                "version": "v1.1",
                "operations": [operation],
            }
            for principal, doc, rev in (
                (owner, document, pending),
                (other, document, old),
                (owner, other_document, old),
            ):
                with pytest.raises(psycopg.errors.RaiseException, match="AI_NOT_FOUND"):
                    lookup(principal, doc, rev)
            for role in ("anon", "authenticated"):
                browser = psycopg.conninfo.make_conninfo(dsn, options=f"-c role={role}")
                with (
                    psycopg.connect(browser) as db,
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                ):
                    db.execute(
                        "SELECT public.corpora_ai_revision_lookup(%s,%s,%s)", (owner, document, old)
                    )
            with psycopg.connect(dsn) as db:
                db.execute(
                    "UPDATE public.corpus_ai_operations SET receipt=%s WHERE id=%s",
                    (Jsonb({**receipt, "proof": "b" * 64}), operation),
                )
            with pytest.raises(psycopg.errors.RaiseException, match="AI_CONFLICT"):
                lookup(owner, document, old)
            with psycopg.connect(dsn) as db:
                db.execute(
                    "UPDATE public.corpus_ai_operations SET receipt=%s WHERE id=%s",
                    (Jsonb(receipt), operation),
                )
                second_id = str(uuid4())
                conflicting = {
                    **intent,
                    "id": second_id,
                    "plan": {
                        **intent["plan"],
                        "before": {"revision": old, "digest": current_digest, "version": "v1.0"},
                    },
                }
                db.execute(
                    "INSERT INTO public.corpus_ai_operations"
                    "(id,document_id,owner,intent,proof,receipt,applied_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (second_id, document, owner, Jsonb(conflicting), proof, Jsonb(receipt), moment),
                )
            with pytest.raises(psycopg.errors.RaiseException, match="AI_CONFLICT"):
                lookup(owner, document, old)
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "fast", "stop"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )


def _apply_with_running_postgrest(db, migration, temp, db_port):
    binary = os.getenv("CORPORA_POSTGREST_BIN")
    if not binary:
        db.execute(migration)
        return
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        rest_port = probe.getsockname()[1]
    config = temp / "postgrest.conf"
    config.write_text(
        f'db-uri = "postgresql://postgres@127.0.0.1:{db_port}/postgres?sslmode=disable"\n'
        'db-schemas = "public"\ndb-anon-role = "service_role"\n'
        'db-channel-enabled = true\ndb-config = false\nserver-host = "127.0.0.1"\n'
        f"server-port = {rest_port}\n"
    )
    process = subprocess.Popen(
        [binary, str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    endpoint = f"http://127.0.0.1:{rest_port}/rpc/corpora_ai_revision_lookup"
    payload = {"p_owner": str(uuid4()), "p_document": str(uuid4()), "p_revision": str(uuid4())}
    try:
        with httpx.Client(timeout=2) as client:
            for _ in range(100):
                try:
                    response = client.post(endpoint, json=payload)
                    if response.json().get("code") == "PGRST202":
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                assert process.poll() is None
                time.sleep(0.05)
            else:
                pytest.fail("PostgREST did not start with the old schema")
            db.execute(migration)
            for _ in range(100):
                response = client.post(endpoint, json=payload)
                if response.json().get("message") == "AI_NOT_FOUND":
                    break
                time.sleep(0.05)
            else:
                pytest.fail("PostgREST did not reload the new revision lookup")
    finally:
        process.terminate()
        process.wait(timeout=10)
