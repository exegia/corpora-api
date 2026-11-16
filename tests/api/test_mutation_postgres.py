"""Opt-in concurrent PostgreSQL test using a newly initialized local cluster.

Set CORPORA_TEST_POSTGRES_BIN to a PostgreSQL bin directory, and optionally
CORPORA_TEST_POSTGRES_SHARE if the installation needs an explicit share path.
Requires psycopg. No existing database or external service is used.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"),
    reason="set CORPORA_TEST_POSTGRES_BIN for isolated real PostgreSQL tests",
)


def test_concurrent_provision_reuses_one_owner_scoped_draft():
    psycopg = pytest.importorskip("psycopg")
    Jsonb = pytest.importorskip("psycopg.types.json").Jsonb
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    fixture = Path(__file__).resolve().parents[1] / "fixtures"
    root = Path(__file__).resolve().parents[2]

    with tempfile.TemporaryDirectory(prefix="corpora-pg-", dir="/tmp") as temp:
        path = Path(temp)
        cluster = path / "data"
        init = [str(binaries / "initdb"), "-D", str(cluster), "-U", "postgres", "-A", "trust"]
        if share := os.getenv("CORPORA_TEST_POSTGRES_SHARE"):
            init.extend(["-L", share])
        subprocess.run(init, check=True, capture_output=True, text=True, timeout=60)
        pg_ctl = str(binaries / "pg_ctl")
        options = shlex.join(["-k", temp, "-p", "55432", "-c", "listen_addresses="])
        subprocess.run(
            [pg_ctl, "-D", str(cluster), "-l", str(path / "server.log"), "-o", options, "start"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            dsn = f"host={temp} port=55432 dbname=postgres user=postgres"
            owner, other, document, job, first_revision, second_revision = (
                str(uuid4()) for _ in range(6)
            )
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS"
                )
                db.execute((fixture / "ai-hosted-bootstrap.sql").read_text())
                db.execute(
                    "CREATE TABLE auth.users(id uuid PRIMARY KEY); "
                    "ALTER TABLE public.corpus_documents ADD COLUMN name text, "
                    "ADD COLUMN source text, "
                    "ADD COLUMN path text, ADD COLUMN filename text, "
                    "ADD COLUMN job_id text, ADD COLUMN status text"
                )
                # Model a permissive legacy document policy that already exists
                # before the candidate migration adds its restrictive policy.
                db.execute(
                    "GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE "
                    "ON public.corpus_documents TO anon,authenticated; "
                    "CREATE POLICY fixture_document_allow ON public.corpus_documents "
                    "FOR ALL TO PUBLIC USING(true) WITH CHECK(true)"
                )
                db.execute("INSERT INTO auth.users VALUES (%s),(%s)", (owner, other))
                db.execute("INSERT INTO public.users VALUES (%s),(%s)", (owner, other))
                db.execute(
                    (
                        root / "supabase/migrations/20261115042900_legacy_ai_draft_registry.sql"
                    ).read_text()
                )

            def payload(revision):
                return {
                    "document_id": document,
                    "job_id": job,
                    "corpus": "draft:" + document,
                    "bucket": "corpus-ai-drafts",
                    "object_key": f"{owner}/ai/{document}/{revision}.corpus",
                    "revision": revision,
                    "digest": "digest-" + revision,
                    "version": revision,
                    "name": "fixture",
                }

            service_dsn = psycopg.conninfo.make_conninfo(dsn, options="-c role=service_role")
            first_ready, release, second_ready = (threading.Event() for _ in range(3))
            results, errors, second_pid = [], [], []

            def first():
                try:
                    with psycopg.connect(service_dsn) as db:
                        results.append(
                            db.execute(
                                "SELECT public.corpora_ai_storage('provision',%s,%s)",
                                (owner, Jsonb(payload(first_revision))),
                            ).fetchone()[0]
                        )
                        first_ready.set()
                        if not release.wait(15):
                            raise TimeoutError("first transaction was not released")
                except Exception as exc:
                    errors.append(exc)
                    first_ready.set()

            def second():
                try:
                    with psycopg.connect(service_dsn) as db:
                        second_pid.append(db.info.backend_pid)
                        second_ready.set()
                        results.append(
                            db.execute(
                                "SELECT public.corpora_ai_storage('provision',%s,%s)",
                                (owner, Jsonb(payload(second_revision))),
                            ).fetchone()[0]
                        )
                except Exception as exc:
                    errors.append(exc)
                    second_ready.set()

            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            try:
                assert first_ready.wait(10) and not errors
                second_thread.start()
                assert second_ready.wait(10) and not errors
                # Observe the actual PostgreSQL lock wait, not timing alone.
                deadline = time.monotonic() + 10
                with psycopg.connect(dsn, autocommit=True) as db:
                    while not db.execute(
                        "SELECT cardinality(pg_blocking_pids(%s)) > 0", (second_pid[0],)
                    ).fetchone()[0]:
                        if time.monotonic() >= deadline:
                            pytest.fail("second provision did not wait for the first transaction")
                        threading.Event().wait(0.02)
            finally:
                release.set()
                first_thread.join(15)
                if second_thread.ident is not None:
                    second_thread.join(15)
            assert not first_thread.is_alive() and not second_thread.is_alive()
            assert not errors
            assert len(results) == 2 and results[0] == results[1]
            assert results[0]["revision"] == first_revision
            with psycopg.connect(dsn) as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM public.corpus_documents WHERE id=%s", (document,)
                    ).fetchone()[0]
                    == 1
                )
                assert (
                    db.execute(
                        "SELECT count(*) FROM public.corpus_ai_drafts WHERE document_id=%s",
                        (document,),
                    ).fetchone()[0]
                    == 1
                )
            with (
                psycopg.connect(service_dsn) as db,
                pytest.raises(psycopg.errors.RaiseException, match="AI_CONFLICT"),
            ):
                db.execute(
                    "SELECT public.corpora_ai_storage('provision',%s,%s)",
                    (other, Jsonb(payload(second_revision))),
                )
            _assert_publish_and_rejection_race(
                psycopg, Jsonb, dsn, service_dsn, owner, document, first_revision
            )
            _assert_browser_roles_cannot_access_private_drafts(psycopg, dsn, owner, other, document)
            _assert_history_bytes_are_bounded(psycopg, Jsonb, dsn, service_dsn, owner, document)
        finally:
            subprocess.run(
                [pg_ctl, "-D", str(cluster), "-m", "fast", "stop"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )


def _assert_history_bytes_are_bounded(psycopg, Jsonb, dsn, service_dsn, owner, document):
    with psycopg.connect(dsn) as db:
        db.execute(
            "INSERT INTO public.corpus_changes "
            "(id,document_id,version,node_id,field,previous_value,new_value,kind,status,"
            "resp,applied_by,applied_at,ai_operation_id) "
            "SELECT %s,document_id,version,node_id,'other',previous_value,new_value,kind,status,"
            "resp,applied_by,applied_at,ai_operation_id FROM public.corpus_changes "
            "WHERE document_id=%s AND ai_operation_id IS NOT NULL LIMIT 1",
            (str(uuid4()), document),
        )
        db.execute(
            "UPDATE public.corpus_changes SET previous_value=repeat('x', 2000000) "
            "WHERE document_id=%s AND ai_operation_id IS NOT NULL",
            (document,),
        )
    payload = {"corpus": f"draft:{document}", "node_id": None, "limit": 3, "offset": 0}
    with psycopg.connect(service_dsn) as db:
        first = db.execute(
            "SELECT public.corpora_ai_storage('history',%s,%s)", (owner, Jsonb(payload))
        ).fetchone()[0]
        assert len(first["entries"]) == 1 and first["has_more"] is True
        payload["offset"] = 1
        second = db.execute(
            "SELECT public.corpora_ai_storage('history',%s,%s)", (owner, Jsonb(payload))
        ).fetchone()[0]
        assert len(second["entries"]) == 1 and second["has_more"] is False
        assert first["entries"][0]["change_id"] != second["entries"][0]["change_id"]
    with psycopg.connect(dsn) as db:
        db.execute(
            "UPDATE public.corpus_changes SET previous_value=repeat('x', 3900000) WHERE id=%s",
            (first["entries"][0]["change_id"],),
        )
    payload["offset"] = 0
    with psycopg.connect(service_dsn) as db:
        oversized = db.execute(
            "SELECT public.corpora_ai_storage('history',%s,%s)", (owner, Jsonb(payload))
        ).fetchone()[0]
        assert oversized == {"entries": [], "has_more": True}


def _assert_browser_roles_cannot_access_private_drafts(psycopg, dsn, owner, other, document):
    legacy_document, legacy_change, private_object, legacy_object = (str(uuid4()) for _ in range(4))
    with psycopg.connect(dsn) as db:
        db.execute("INSERT INTO public.corpus_documents(id) VALUES (%s)", (legacy_document,))
        db.execute(
            "INSERT INTO public.corpus_changes "
            "(id,document_id,version,node_id,field,previous_value,new_value,kind,resp,applied_by) "
            "VALUES (%s,%s,'1',1,'label','old','new','label',ARRAY['#legacy','#actor'],%s)",
            (legacy_change, legacy_document, owner),
        )
        db.execute("INSERT INTO storage.buckets(id,name) VALUES ('legacy','legacy')")
        db.execute(
            "INSERT INTO storage.objects(id,bucket_id) "
            "VALUES (%s,'corpus-ai-drafts'),(%s,'legacy')",
            (private_object, legacy_object),
        )

    for user in (owner, other):
        browser = psycopg.conninfo.make_conninfo(dsn, options="-c role=authenticated")
        with psycopg.connect(browser) as db:
            db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (user,))
            assert db.execute(
                "SELECT id::text FROM public.corpus_documents WHERE id IN (%s,%s)",
                (document, legacy_document),
            ).fetchall() == [(legacy_document,)]
            visible = {
                row[0]
                for row in db.execute(
                    "SELECT id::text FROM public.corpus_changes WHERE document_id IN (%s,%s)",
                    (document, legacy_document),
                ).fetchall()
            }
            assert legacy_change in visible
            if user == owner:
                assert len(visible) == 3
            else:
                assert visible == {legacy_change}
            assert db.execute(
                "SELECT id::text FROM storage.objects WHERE id IN (%s,%s)",
                (private_object, legacy_object),
            ).fetchall() == [(legacy_object,)]
        with psycopg.connect(browser) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT * FROM public.corpus_ai_drafts")
        with psycopg.connect(browser) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT * FROM public.corpus_ai_threads")
        with psycopg.connect(browser) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(
                "INSERT INTO storage.objects(id,bucket_id) VALUES (%s,'corpus-ai-drafts')",
                (str(uuid4()),),
            )
        with psycopg.connect(browser) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("TRUNCATE public.corpus_documents")
        with psycopg.connect(browser) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("TRUNCATE public.corpus_changes")
        with psycopg.connect(browser) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT public.corpora_ai_storage('capabilities',%s,'{}')", (user,))

    anonymous = psycopg.conninfo.make_conninfo(dsn, options="-c role=anon")
    with psycopg.connect(anonymous) as db:
        assert db.execute(
            "SELECT id::text FROM public.corpus_documents WHERE id IN (%s,%s)",
            (document, legacy_document),
        ).fetchall() == [(legacy_document,)]
        assert db.execute(
            "SELECT id::text FROM storage.objects WHERE id IN (%s,%s)",
            (private_object, legacy_object),
        ).fetchall() == [(legacy_object,)]
    with psycopg.connect(anonymous) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("SELECT * FROM public.corpus_ai_operations")
    with psycopg.connect(anonymous) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("SELECT public.corpora_ai_storage('capabilities',%s,'{}')", (owner,))


def _assert_publish_and_rejection_race(psycopg, Jsonb, dsn, service_dsn, owner, document, before):
    thread, operation, change, after = (str(uuid4()) for _ in range(4))
    corpus = "draft:" + document
    suggestion = {
        "id": thread + ":" + str(uuid4()),
        "scope": {"corpus": corpus, "node_type": "verse"},
        "target_node": "1",
        "kind": "label",
        "status": "pending",
        "diff": [{"field": "label", "old": "before", "new": "after"}],
    }
    intent = {
        "id": operation,
        "owner": owner,
        "suggestion": suggestion,
        "plan": {
            "before": {"revision": before, "digest": "digest-" + before, "version": before},
            "after": {"revision": after, "digest": "digest-" + after, "version": after},
        },
    }
    proof = "a" * 64
    entry = {
        "change_id": change,
        "corpus": corpus,
        "applied_by": owner,
        "resp": ["#corpora-ai", "#" + owner],
        "version": after,
        "node_id": "1",
        "field": "label",
        "previous_value": "before",
        "new_value": "after",
    }
    with psycopg.connect(service_dsn) as db:
        db.execute(
            "INSERT INTO public.corpus_ai_threads(id,owner,corpus,created_at,data) "
            "VALUES (%s,%s,%s,now(),%s)",
            (
                thread,
                owner,
                corpus,
                Jsonb(
                    {
                        "thread": {"id": thread, "corpus": corpus},
                        "suggestions": [{"suggestion": suggestion}],
                    }
                ),
            ),
        )
        db.execute(
            "SELECT public.corpora_ai_storage('insert',%s,%s)",
            (owner, Jsonb({"intent": intent, "proof": proof, "entries": [entry]})),
        )

    request = {
        "id": operation,
        "proof": proof,
        "object_key": f"{owner}/ai/{document}/{after}.corpus",
    }
    first_ready, release = threading.Event(), threading.Event()
    results, errors, waiting = [], [], {}

    def publish(first):
        try:
            with psycopg.connect(service_dsn) as db:
                if not first:
                    waiting["retry"] = db.info.backend_pid
                result = db.execute(
                    "SELECT public.corpora_ai_storage('publish',%s,%s)",
                    (owner, Jsonb(request)),
                ).fetchone()[0]
                results.append(result)
                if first:
                    first_ready.set()
                    if not release.wait(15):
                        raise TimeoutError("publication was not released")
        except Exception as exc:
            errors.append(exc)
            first_ready.set()

    def reject():
        try:
            with psycopg.connect(service_dsn) as db:
                waiting["reject"] = db.info.backend_pid
                # This is the same revision CAS used when rejecting a suggestion.
                results.append(
                    db.execute(
                        "UPDATE public.corpus_ai_threads SET revision=revision+1 "
                        "WHERE id=%s AND owner=%s AND revision=0",
                        (thread, owner),
                    ).rowcount
                )
        except Exception as exc:
            errors.append(exc)

    winner = threading.Thread(target=publish, args=(True,))
    retry = threading.Thread(target=publish, args=(False,))
    rejection = threading.Thread(target=reject)
    winner.start()
    try:
        assert first_ready.wait(10) and not errors
        retry.start()
        rejection.start()
        deadline = time.monotonic() + 10
        with psycopg.connect(dsn, autocommit=True) as db:
            while len(waiting) < 2 or not all(
                db.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", (pid,)).fetchone()[0]
                for pid in list(waiting.values())
            ):
                if errors or time.monotonic() >= deadline:
                    pytest.fail(f"competing transactions did not wait: {errors}")
                threading.Event().wait(0.02)
    finally:
        release.set()
        for worker in (winner, retry, rejection):
            if worker.ident is not None:
                worker.join(15)
    assert not any(worker.is_alive() for worker in (winner, retry, rejection))
    assert not errors
    assert results.count(True) == 2 and 0 in results
    with psycopg.connect(dsn) as db:
        assert (
            db.execute(
                "SELECT revision::text FROM public.corpus_ai_drafts WHERE document_id=%s",
                (document,),
            ).fetchone()[0]
            == after
        )
        receipt, applied = db.execute(
            "SELECT receipt,applied_at FROM public.corpus_ai_operations WHERE id=%s", (operation,)
        ).fetchone()
        assert receipt["proof"] == proof and applied is not None
        assert db.execute(
            "SELECT status,applied_at FROM public.corpus_changes WHERE id=%s", (change,)
        ).fetchone() == ("applied", applied)
        revision, data = db.execute(
            "SELECT revision,data FROM public.corpus_ai_threads WHERE id=%s", (thread,)
        ).fetchone()
        assert revision == 1 and data["suggestions"][0]["suggestion"]["status"] == "applied"

    # A second suggestion starts from the new HEAD. Rejection owns the thread
    # lock first, so the waiting publication must leave HEAD and history alone.
    second_operation, second_change, next_revision = (str(uuid4()) for _ in range(3))
    next_suggestion = {**suggestion, "id": thread + ":" + str(uuid4())}
    next_intent = {
        **intent,
        "id": second_operation,
        "suggestion": next_suggestion,
        "plan": {
            "before": {"revision": after, "digest": "digest-" + after, "version": after},
            "after": {
                "revision": next_revision,
                "digest": "digest-" + next_revision,
                "version": next_revision,
            },
        },
    }
    next_entry = {**entry, "change_id": second_change, "version": next_revision}
    next_request = {
        "id": second_operation,
        "proof": proof,
        "object_key": f"{owner}/ai/{document}/{next_revision}.corpus",
    }
    with psycopg.connect(service_dsn) as db:
        db.execute(
            "UPDATE public.corpus_ai_threads SET data=jsonb_set(data,'{suggestions}',%s) "
            "WHERE id=%s",
            (Jsonb([{"suggestion": next_suggestion}]), thread),
        )
        db.execute(
            "SELECT public.corpora_ai_storage('insert',%s,%s)",
            (owner, Jsonb({"intent": next_intent, "proof": proof, "entries": [next_entry]})),
        )

    rejected, proceed, publishing = threading.Event(), threading.Event(), threading.Event()
    loser_pid, failure = [], []

    def reject_first():
        try:
            with psycopg.connect(service_dsn) as db:
                assert (
                    db.execute(
                        "UPDATE public.corpus_ai_threads SET revision=revision+1, "
                        "data=jsonb_set(data,'{suggestions,0,suggestion,status}',%s) "
                        "WHERE id=%s AND owner=%s AND revision=1",
                        (Jsonb("rejected"), thread, owner),
                    ).rowcount
                    == 1
                )
                rejected.set()
                if not proceed.wait(15):
                    raise TimeoutError("rejection was not released")
        except Exception as exc:
            failure.append(exc)
            rejected.set()

    def publish_after_rejection():
        try:
            with psycopg.connect(service_dsn) as db:
                loser_pid.append(db.info.backend_pid)
                publishing.set()
                db.execute(
                    "SELECT public.corpora_ai_storage('publish',%s,%s)",
                    (owner, Jsonb(next_request)),
                )
            failure.append(AssertionError("rejected suggestion was published"))
        except psycopg.errors.RaiseException as exc:
            if "AI_CONFLICT" not in str(exc):
                failure.append(exc)
        except Exception as exc:
            failure.append(exc)

    reject_worker = threading.Thread(target=reject_first)
    publish_worker = threading.Thread(target=publish_after_rejection)
    reject_worker.start()
    try:
        assert rejected.wait(10) and not failure
        publish_worker.start()
        assert publishing.wait(10)
        deadline = time.monotonic() + 10
        with psycopg.connect(dsn, autocommit=True) as db:
            while not db.execute(
                "SELECT cardinality(pg_blocking_pids(%s)) > 0", (loser_pid[0],)
            ).fetchone()[0]:
                if failure or time.monotonic() >= deadline:
                    pytest.fail(f"publication did not wait for rejection: {failure}")
                threading.Event().wait(0.02)
    finally:
        proceed.set()
        for worker in (reject_worker, publish_worker):
            if worker.ident is not None:
                worker.join(15)
    assert not reject_worker.is_alive() and not publish_worker.is_alive() and not failure
    with psycopg.connect(dsn) as db:
        assert (
            db.execute(
                "SELECT revision::text FROM public.corpus_ai_drafts WHERE document_id=%s",
                (document,),
            ).fetchone()[0]
            == after
        )
        assert db.execute(
            "SELECT receipt,applied_at FROM public.corpus_ai_operations WHERE id=%s",
            (second_operation,),
        ).fetchone() == (None, None)
        assert db.execute(
            "SELECT status,applied_at FROM public.corpus_changes WHERE id=%s", (second_change,)
        ).fetchone() == ("pending", None)
