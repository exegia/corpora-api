"""Opt-in isolated PostgreSQL regression for private draft relations."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"),
    reason="set CORPORA_TEST_POSTGRES_BIN for isolated PostgreSQL tests",
)


def test_private_draft_relations_are_hidden_and_legacy_relations_still_work():
    psycopg = pytest.importorskip("psycopg")
    Jsonb = pytest.importorskip("psycopg.types.json").Jsonb
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="corpora-commit-pg-", dir="/tmp") as temp:
        cluster = Path(temp) / "data"
        init = [str(binaries / "initdb"), "-D", str(cluster), "-U", "postgres", "-A", "trust"]
        if share := os.getenv("CORPORA_TEST_POSTGRES_SHARE"):
            init.extend(["-L", share])
        subprocess.run(init, check=True, capture_output=True, text=True, timeout=60)
        ctl = str(binaries / "pg_ctl")
        options = shlex.join(["-k", temp, "-p", "55432", "-c", "listen_addresses="])
        subprocess.run(
            [ctl, "-D", str(cluster), "-l", str(Path(temp) / "pg.log"), "-o", options, "start"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            dsn = f"host={temp} port=55432 dbname=postgres user=postgres"
            owner, other, document, legacy, revision, private_commit, private_project = (
                str(uuid4()) for _ in range(7)
            )
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS"
                )
                db.execute((root / "tests/fixtures/draft-relations-bootstrap.sql").read_text())
                db.execute("CREATE TABLE auth.users(id uuid PRIMARY KEY)")
                db.execute("INSERT INTO auth.users VALUES (%s),(%s)", (owner, other))
                db.execute("INSERT INTO public.users VALUES (%s),(%s)", (owner, other))
                db.execute(
                    (
                        root / "supabase/migrations/20261115042900_legacy_ai_draft_registry.sql"
                    ).read_text()
                )
                db.execute(
                    "INSERT INTO public.corpus_documents(id,name,source,path) "
                    "VALUES (%s,'legacy','upload','legacy')",
                    (legacy,),
                )
            service = psycopg.conninfo.make_conninfo(dsn, options="-c role=service_role")
            payload = {
                "document_id": document,
                "corpus": "draft:" + document,
                "bucket": "corpus-ai-drafts",
                "object_key": f"{owner}/ai/{document}/{revision}.corpus",
                "revision": revision,
                "digest": "sha256:" + "a" * 64,
                "version": "1",
                "name": "test",
                "job_id": str(uuid4()),
            }
            with psycopg.connect(service) as db:
                db.execute(
                    "SELECT public.corpora_ai_storage('provision',%s,%s)", (owner, Jsonb(payload))
                )
                db.execute(
                    "INSERT INTO public.corpus_commits(id,document_id,sha,message) "
                    "VALUES (%s,%s,'private','private')",
                    (private_commit, document),
                )
                db.execute(
                    "INSERT INTO public.projects(id,name,corpus_document_id) "
                    "VALUES (%s,'private',%s)",
                    (private_project, document),
                )
            for role, subject in (("anon", ""), ("authenticated", owner), ("authenticated", other)):
                browser = psycopg.conninfo.make_conninfo(dsn, options=f"-c role={role}")
                with psycopg.connect(browser) as db:
                    db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (subject,))
                    assert (
                        db.execute(
                            "SELECT id FROM public.corpus_commits WHERE id=%s", (private_commit,)
                        ).fetchall()
                        == []
                    )
                    assert (
                        db.execute(
                            "DELETE FROM public.corpus_commits WHERE id=%s RETURNING id",
                            (private_commit,),
                        ).fetchall()
                        == []
                    )
                    assert not db.execute(
                        "SELECT has_table_privilege("
                        "current_user,'public.corpus_commits','TRUNCATE')"
                    ).fetchone()[0]
                    assert (
                        db.execute(
                            "SELECT id FROM public.projects WHERE id=%s", (private_project,)
                        ).fetchall()
                        == []
                    )
                    assert (
                        db.execute(
                            "DELETE FROM public.projects WHERE id=%s RETURNING id",
                            (private_project,),
                        ).fetchall()
                        == []
                    )
                with (
                    psycopg.connect(browser) as db,
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                ):
                    db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (subject,))
                    db.execute(
                        "INSERT INTO public.corpus_commits(document_id,sha,message) "
                        "VALUES (%s,%s,'foreign')",
                        (document, str(uuid4())),
                    )
                with (
                    psycopg.connect(browser) as db,
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                ):
                    db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (subject,))
                    db.execute(
                        "INSERT INTO public.projects(name,corpus_document_id) "
                        "VALUES ('foreign',%s)",
                        (document,),
                    )
                with psycopg.connect(browser) as db:
                    db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (subject,))
                    commit = db.execute(
                        "INSERT INTO public.corpus_commits(document_id,sha,message) "
                        "VALUES (%s,%s,'legacy') RETURNING id",
                        (legacy, str(uuid4())),
                    ).fetchone()[0]
                    assert db.execute(
                        "SELECT id FROM public.corpus_commits WHERE id=%s", (commit,)
                    ).fetchone()
                    assert db.execute(
                        "DELETE FROM public.corpus_commits WHERE id=%s RETURNING id", (commit,)
                    ).fetchone()
                    normal = db.execute(
                        "INSERT INTO public.projects(name) VALUES ('legacy') RETURNING id"
                    ).fetchone()[0]
                    assert db.execute(
                        "UPDATE public.projects SET corpus_document_id=%s WHERE id=%s RETURNING id",
                        (legacy, normal),
                    ).fetchone()
                with (
                    psycopg.connect(browser) as db,
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                ):
                    db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (subject,))
                    db.execute(
                        "UPDATE public.projects SET corpus_document_id=%s WHERE id=%s",
                        (document, normal),
                    )
                with psycopg.connect(browser) as db:
                    db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (subject,))
                    assert db.execute(
                        "DELETE FROM public.projects WHERE id=%s RETURNING id", (normal,)
                    ).fetchone()
            with psycopg.connect(service) as db:
                assert db.execute(
                    "SELECT id FROM public.corpus_commits WHERE id=%s", (private_commit,)
                ).fetchone()
                assert db.execute(
                    "SELECT id FROM public.projects WHERE id=%s", (private_project,)
                ).fetchone()
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "fast", "stop"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
