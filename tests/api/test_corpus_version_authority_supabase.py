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
            "corpora_corpus_bootstrap",
            "corpora_corpus_provision_ops",
            "corpora_corpus_write_ops",
            "corpora_corpus_staged_inputs",
            "corpora_corpus_publication_claims",
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
            provision, claim = str(uuid4()), str(uuid4())
            db.execute(
                "INSERT INTO public.corpora_corpus_provision_ops "
                "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                "VALUES (%s,%s,%s,%s,%s,'dataset')",
                (provision, corpus, user, str(uuid4()), "fixture/corpus-" + corpus),
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                db.execute(
                    "INSERT INTO public.corpora_corpus_provision_ops "
                    "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                    "VALUES (%s,%s,%s,%s,%s,'dataset')",
                    (str(uuid4()), corpus, user, str(uuid4()), "fixture/corpus-" + corpus),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "DELETE FROM public.corpora_corpus_provision_ops WHERE id=%s", (provision,)
                )
            db.execute(
                "UPDATE public.corpora_corpus_provision_ops SET state='executing', "
                "worker_claim=%s WHERE id=%s",
                (claim, provision),
            )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET state='completed' WHERE id=%s",
                    (provision,),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_bootstrap "
                    "(corpus_id,operation_id,worker_claim,commit_sha) VALUES (%s,%s,%s,%s)",
                    (corpus, provision, str(uuid4()), "9" * 40),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET worker_claim=%s WHERE id=%s",
                    (str(uuid4()), provision),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_bootstrap "
                    "(corpus_id,operation_id,worker_claim,commit_sha) VALUES (%s,%s,%s,%s)",
                    (corpus, provision, claim, "9" * 40),
                )
            with db.transaction():
                db.execute(
                    "INSERT INTO public.corpora_corpus_bootstrap "
                    "(corpus_id,operation_id,worker_claim,commit_sha) VALUES (%s,%s,%s,%s)",
                    (corpus, provision, claim, "9" * 40),
                )
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET state='completed' WHERE id=%s",
                    (provision,),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET state='executing' WHERE id=%s",
                    (provision,),
                )
            uncertain, uncertain_claim = str(uuid4()), str(uuid4())
            db.execute(
                "INSERT INTO public.corpora_corpus_provision_ops "
                "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                "VALUES (%s,%s,%s,%s,%s,'dataset')",
                (uncertain, other, user, str(uuid4()), "fixture/corpus-" + other),
            )
            db.execute(
                "UPDATE public.corpora_corpus_provision_ops SET state='executing', "
                "worker_claim=%s WHERE id=%s",
                (uncertain_claim, uncertain),
            )
            db.execute(
                "UPDATE public.corpora_corpus_provision_ops SET state='uncertain' WHERE id=%s",
                (uncertain,),
            )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET state='executing' WHERE id=%s",
                    (uncertain,),
                )
            with pytest.raises(psycopg.errors.UniqueViolation):
                db.execute(
                    "INSERT INTO public.corpora_corpus_provision_ops "
                    "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                    "VALUES (%s,%s,%s,%s,%s,'dataset')",
                    (str(uuid4()), other, user, str(uuid4()), "fixture/corpus-" + other),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_bootstrap SET commit_sha=%s WHERE corpus_id=%s",
                    ("8" * 40, corpus),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "DELETE FROM public.corpora_corpus_bootstrap WHERE corpus_id=%s", (corpus,)
                )

            def operation(op, cid, seq, parent):
                db.execute(
                    "INSERT INTO public.corpora_corpus_write_ops "
                    "(id,corpus_id,actor_id,session_id,expected_sequence,"
                    "expected_parent_sha,input_sha256,input_format) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'zip')",
                    (op, cid, user, str(uuid4()), seq, parent, "a" * 64),
                )

            db.execute(
                "SELECT pg_catalog.set_config('test.publication_claim',%s,false)", (str(uuid4()),)
            )

            def seed_publication(op, cid, stage_claim):
                publication = str(uuid4())
                db.execute(
                    "INSERT INTO public.corpora_corpus_publication_claims "
                    "(operation_id,corpus_id,stage_claim,publication_claim) VALUES (%s,%s,%s,%s)",
                    (op, cid, stage_claim, publication),
                )
                db.execute(
                    "SELECT pg_catalog.set_config('test.publication_claim',%s,false)",
                    (publication,),
                )
                return publication

            missing_bootstrap_op = str(uuid4())
            operation(missing_bootstrap_op, other, 0, None)
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='executing',worker_claim=%s "
                "WHERE id=%s",
                (str(uuid4()), missing_bootstrap_op),
            )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_publication_claims "
                    "(operation_id,corpus_id,stage_claim,publication_claim) "
                    "SELECT id,corpus_id,worker_claim,%s FROM public.corpora_corpus_write_ops "
                    "WHERE id=%s",
                    (str(uuid4()), missing_bootstrap_op),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) "
                    "VALUES (%s,1,%s,NULL,%s,%s,"
                    "current_setting('test.publication_claim')::uuid)",
                    (other, "d" * 40, missing_bootstrap_op, "corpora-op-" + missing_bootstrap_op),
                )

            operation(op1, corpus, 0, None)
            for bad_format, expected_error in (
                (None, psycopg.errors.NotNullViolation),
                ("rar", psycopg.errors.CheckViolation),
            ):
                with pytest.raises(expected_error):
                    db.execute(
                        "INSERT INTO public.corpora_corpus_write_ops "
                        "(id,corpus_id,actor_id,session_id,expected_sequence,"
                        "expected_parent_sha,input_sha256,input_format) "
                        "VALUES (%s,%s,%s,%s,0,NULL,%s,%s)",
                        (str(uuid4()), corpus, user, str(uuid4()), "a" * 64, bad_format),
                    )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET input_format='corpus' WHERE id=%s",
                    (op1,),
                )
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
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET state='executing' WHERE id=%s",
                    (op1,),
                )
            claim1 = str(uuid4())
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='executing',worker_claim=%s "
                "WHERE id=%s",
                (claim1, op1),
            )
            db.execute(
                "INSERT INTO public.corpora_corpus_staged_inputs "
                "(operation_id,corpus_id,worker_claim,repo_id,repo_type,input_format,"
                "input_sha256,archive_size,requested_base_sha,stage_sha,stage_ref,stage_path) "
                "VALUES (%s,%s,%s,%s,'dataset','zip',%s,100,%s,%s,%s,%s)",
                (
                    op1,
                    corpus,
                    claim1,
                    "fixture/corpus-" + corpus,
                    "a" * 64,
                    "9" * 40,
                    "8" * 40,
                    "corpora-stage-" + op1,
                    ".corpora-staging/" + op1 + "/" + claim1 + ".zip",
                ),
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute(
                    "INSERT INTO public.corpora_corpus_publication_claims "
                    "(operation_id,corpus_id,stage_claim,publication_claim) VALUES (%s,%s,%s,%s)",
                    (op1, corpus, claim1, claim1),
                )
            publication1 = seed_publication(op1, corpus, claim1)
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) VALUES (%s,1,%s,NULL,%s,%s,%s)",
                    (corpus, "d" * 40, op1, "corpora-op-" + op1, str(uuid4())),
                )
            with pytest.raises(psycopg.errors.RaiseException) as staging_version:
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) VALUES (%s,1,%s,NULL,%s,%s,%s)",
                    (corpus, "8" * 40, op1, "corpora-op-" + op1, publication1),
                )
            assert staging_version.value.diag.message_primary == "CORPUS_VERSION_OPERATION_INVALID"
            assert publication1 != claim1
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET worker_claim=%s WHERE id=%s",
                    (str(uuid4()), op1),
                )

            class RollBackUncertainProbe(Exception):
                pass

            with pytest.raises(RollBackUncertainProbe):
                with db.transaction():
                    db.execute(
                        "UPDATE public.corpora_corpus_write_ops SET state='uncertain' WHERE id=%s",
                        (op1,),
                    )
                    with pytest.raises(psycopg.errors.RaiseException):
                        with db.transaction():
                            db.execute(
                                "UPDATE public.corpora_corpus_write_ops SET state='admitted' "
                                "WHERE id=%s",
                                (op1,),
                            )
                    with pytest.raises(psycopg.errors.RaiseException):
                        with db.transaction():
                            db.execute(
                                "INSERT INTO public.corpora_corpus_versions "
                                "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                                "retained_ref,publication_claim) VALUES (%s,1,%s,NULL,%s,%s,%s)",
                                (corpus, "b" * 40, op1, "corpora-op-" + op1, publication1),
                            )
                    raise RollBackUncertainProbe()
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) "
                    "VALUES (%s,2,%s,%s,%s,%s,current_setting('test.publication_claim')::uuid)",
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
                        "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                        "retained_ref,publication_claim) "
                        "VALUES (%s,1,%s,NULL,%s,%s,"
                        "current_setting('test.publication_claim')::uuid)",
                        (corpus, "b" * 40, op1, "corpora-op-" + op1),
                    )
            with db.transaction():
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) "
                    "VALUES (%s,1,%s,NULL,%s,%s,"
                    "current_setting('test.publication_claim')::uuid)",
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
            db.execute(
                "UPDATE public.corpora_corpus_metadata SET state='ready' WHERE id=%s", (corpus,)
            )
            operation(op2, corpus, 1, "b" * 40)
            claim2 = str(uuid4())
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='executing',worker_claim=%s "
                "WHERE id=%s",
                (claim2, op2),
            )
            db.execute(
                "INSERT INTO public.corpora_corpus_staged_inputs "
                "(operation_id,corpus_id,worker_claim,repo_id,repo_type,input_format,"
                "input_sha256,archive_size,requested_base_sha,stage_sha,stage_ref,stage_path) "
                "VALUES (%s,%s,%s,%s,'dataset','zip',%s,100,%s,%s,%s,%s)",
                (
                    op2,
                    corpus,
                    claim2,
                    "fixture/corpus-" + corpus,
                    "a" * 64,
                    "b" * 40,
                    "8" * 40,
                    "corpora-stage-" + op2,
                    ".corpora-staging/" + op2 + "/" + claim2 + ".zip",
                ),
            )
            seed_publication(op2, corpus, claim2)
            with pytest.raises(psycopg.IntegrityError):
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) "
                    "VALUES (%s,2,%s,%s,%s,%s,current_setting('test.publication_claim')::uuid)",
                    (corpus, "b" * 40, "b" * 40, op2, "corpora-op-" + op2),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) "
                    "VALUES (%s,2,%s,%s,%s,%s,current_setting('test.publication_claim')::uuid)",
                    (corpus, "c" * 40, "d" * 40, op2, "corpora-op-" + op2),
                )
            with db.transaction():
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) "
                    "VALUES (%s,2,%s,%s,%s,%s,current_setting('test.publication_claim')::uuid)",
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
                "corpora_corpus_bootstrap",
                "corpora_corpus_provision_ops",
                "corpora_corpus_write_ops",
                "corpora_corpus_staged_inputs",
                "corpora_corpus_publication_claims",
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
                "corpora_corpus_publication_claims",
                "corpora_corpus_staged_inputs",
                "corpora_corpus_write_ops",
                "corpora_corpus_bootstrap",
                "corpora_corpus_provision_ops",
                "corpora_corpus_metadata",
            ):
                db.execute("DROP TABLE IF EXISTS public." + table + " CASCADE")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_version()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_head()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_preserve_history()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_operation()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_finalized_version()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_provision()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_staged_input()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_publication_claim()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_validate_bootstrap()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_bootstrap_finalized()")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_metadata_stamp()")
            if user:
                client.delete(
                    url + "/auth/v1/admin/users/" + user,
                    headers={"apikey": service, "Authorization": "Bearer " + service},
                ).raise_for_status()
