"""Opt-in database constraints for the unwired corpus content registry."""

import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_content_version_constraints_and_privileges():
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    root = Path(__file__).resolve().parents[2]
    migrations = root / "supabase/migrations"
    metadata = next(migrations.glob("*_corpus_metadata_registry.sql"))
    versions = next(migrations.glob("*_corpus_content_versions.sql"))
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    user = None
    with httpx.Client(timeout=15) as client, psycopg.connect(dsn, autocommit=True) as db:
        for table in (
            "corpora_corpus_metadata",
            "corpora_corpus_write_ops",
            "corpora_corpus_versions",
            "corpora_corpus_heads",
        ):
            assert db.execute("SELECT to_regclass(%s) IS NULL", ("public." + table,)).fetchone()[0]
        try:
            db.execute(metadata.read_text())
            db.execute(versions.read_text())
            signup = client.post(
                url + "/auth/v1/signup",
                headers={"apikey": anon},
                json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
            )
            signup.raise_for_status()
            user = signup.json()["user"]["id"]
            corpus, other, op1, op2 = [str(uuid4()) for _ in range(4)]
            for cid in (corpus, other):
                db.execute(
                    "INSERT INTO public.corpora_corpus_metadata "
                    "(id,owner_id,title,hf_repo_id,hf_repo_type) "
                    "VALUES (%s,%s,'Test',%s,'dataset')",
                    (cid, user, "fixture/corpus-" + cid),
                )

            def operation(op, cid, seq, parent):
                db.execute(
                    "INSERT INTO public.corpora_corpus_write_ops "
                    "(id,corpus_id,actor_id,session_id,expected_sequence,"
                    "expected_parent_sha,input_sha256) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (op, cid, user, str(uuid4()), seq, parent, "a" * 64),
                )

            operation(op1, corpus, 0, None)
            with pytest.raises(psycopg.errors.UniqueViolation):
                operation(str(uuid4()), corpus, 0, None)
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET state='completed' WHERE id=%s",
                    (op1,),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET session_id=%s WHERE id=%s",
                    (str(uuid4()), op1),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute("DELETE FROM public.corpora_corpus_write_ops WHERE id=%s", (op1,))
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='uncertain' WHERE id=%s", (op1,)
            )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET state='admitted' WHERE id=%s",
                    (op1,),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,retained_ref) "
                    "VALUES (%s,2,%s,%s,%s,%s)",
                    (corpus, "b" * 40, "c" * 40, op1, "corpora-op-" + op1),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_heads VALUES (%s,1,%s)", (other, "b" * 40)
                )
            with pytest.raises(psycopg.errors.RaiseException):
                with db.transaction():
                    db.execute(
                        "INSERT INTO public.corpora_corpus_versions "
                        "(corpus_id,sequence,commit_sha,parent_sha,operation_id,retained_ref) "
                        "VALUES (%s,1,%s,NULL,%s,%s)",
                        (corpus, "b" * 40, op1, "corpora-op-" + op1),
                    )
            with db.transaction():
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,retained_ref) "
                    "VALUES (%s,1,%s,NULL,%s,%s)",
                    (corpus, "b" * 40, op1, "corpora-op-" + op1),
                )
                db.execute(
                    "INSERT INTO public.corpora_corpus_heads VALUES (%s,1,%s)", (corpus, "b" * 40)
                )
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET state='completed' WHERE id=%s",
                    (op1,),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET state='failed' WHERE id=%s", (op1,)
                )
            operation(op2, corpus, 1, "b" * 40)
            with pytest.raises(psycopg.IntegrityError):
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,retained_ref) "
                    "VALUES (%s,2,%s,%s,%s,%s)",
                    (corpus, "b" * 40, "b" * 40, op2, "corpora-op-" + op2),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,retained_ref) "
                    "VALUES (%s,2,%s,%s,%s,%s)",
                    (corpus, "c" * 40, "d" * 40, op2, "corpora-op-" + op2),
                )
            with db.transaction():
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,retained_ref) "
                    "VALUES (%s,2,%s,%s,%s,%s)",
                    (corpus, "c" * 40, "b" * 40, op2, "corpora-op-" + op2),
                )
                db.execute(
                    "UPDATE public.corpora_corpus_heads SET sequence=2,commit_sha=%s "
                    "WHERE corpus_id=%s",
                    ("c" * 40, corpus),
                )
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET state='completed' WHERE id=%s",
                    (op2,),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_versions SET commit_sha=%s "
                    "WHERE corpus_id=%s AND sequence=1",
                    ("d" * 40, corpus),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "DELETE FROM public.corpora_corpus_versions WHERE corpus_id=%s AND sequence=1",
                    (corpus,),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute("DELETE FROM public.corpora_corpus_heads WHERE corpus_id=%s", (corpus,))
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_heads SET sequence=1,commit_sha=%s "
                    "WHERE corpus_id=%s",
                    ("b" * 40, corpus),
                )
            for table in (
                "corpora_corpus_write_ops",
                "corpora_corpus_versions",
                "corpora_corpus_heads",
            ):
                for role in ("anon", "authenticated"):
                    assert not db.execute(
                        "SELECT has_table_privilege(%s,%s,'SELECT')", (role, "public." + table)
                    ).fetchone()[0]
                assert db.execute(
                    "SELECT has_table_privilege('service_role',%s,'SELECT')", ("public." + table,)
                ).fetchone()[0]
                for privilege in ("INSERT", "UPDATE", "DELETE"):
                    assert not db.execute(
                        "SELECT has_table_privilege('service_role',%s,%s)",
                        ("public." + table, privilege),
                    ).fetchone()[0]
        finally:
            for table in (
                "corpora_corpus_heads",
                "corpora_corpus_versions",
                "corpora_corpus_write_ops",
                "corpora_corpus_metadata",
            ):
                db.execute("DROP TABLE IF EXISTS public." + table + " CASCADE")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_version()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_head()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_preserve_history()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_operation()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_finalized_version()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_metadata_stamp()")
            if user:
                client.delete(
                    url + "/auth/v1/admin/users/" + user,
                    headers={"apikey": service, "Authorization": "Bearer " + service},
                ).raise_for_status()
