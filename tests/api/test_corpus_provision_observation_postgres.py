"""Check provision-observation identity and privileges against disposable PostgreSQL."""

import os
import shlex
import socket
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from corpora_api.corpus_metadata import CorpusMetadataConflict
from corpora_api.corpus_provision_observation import CorpusProvisionObservationLookup

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"), reason="requires isolated PostgreSQL binaries"
)


def test_observation_loads_only_matching_unfinished_claim(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture")
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    migrations = Path(__file__).resolve().parents[2] / "supabase/migrations"
    with tempfile.TemporaryDirectory(prefix="corpus-observation-", dir="/tmp") as tmp:
        cluster = Path(tmp) / "data"
        init = [str(binaries / "initdb"), "-D", str(cluster), "-U", "postgres", "-A", "trust"]
        if share := os.getenv("CORPORA_TEST_POSTGRES_SHARE"):
            init.extend(["-L", share])
        subprocess.run(init, check=True, capture_output=True, timeout=60)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ctl = str(binaries / "pg_ctl")
        options = shlex.join(["-k", tmp, "-p", str(port), "-c", "listen_addresses="])
        subprocess.run(
            [ctl, "-D", str(cluster), "-l", str(Path(tmp) / "pg.log"), "-o", options, "start"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            dsn = f"host={tmp} port={port} dbname=postgres user=postgres"
            owner, corpus, operation, claim = (uuid4() for _ in range(4))
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS"
                )
                db.execute("CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY)")
                db.execute("INSERT INTO auth.users VALUES (%s)", (owner,))
                for suffix in ("corpus_metadata_registry", "corpus_content_versions"):
                    db.execute(next(migrations.glob(f"*_{suffix}.sql")).read_text())
                repo = f"fixture/corpus-{corpus}"
                db.execute(
                    "INSERT INTO public.corpora_corpus_metadata "
                    "(id,owner_id,title,hf_repo_id,hf_repo_type) "
                    "VALUES (%s,%s,'Test',%s,'dataset')",
                    (corpus, owner, repo),
                )
                db.execute(
                    "INSERT INTO public.corpora_corpus_provision_ops "
                    "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                    "VALUES (%s,%s,%s,%s,%s,'dataset')",
                    (operation, corpus, owner, uuid4(), repo),
                )
                lookup = CorpusProvisionObservationLookup(
                    lambda: psycopg.connect(dsn), assume_service_role=True
                )
                with pytest.raises(CorpusMetadataConflict):
                    lookup.load_trusted(operation)
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET state='executing',"
                    "worker_claim=%s WHERE id=%s",
                    (claim, operation),
                )
                observed = lookup.load_trusted(operation)
                assert (
                    observed.operation_id,
                    observed.claim_id,
                    observed.corpus_id,
                    observed.repo_id,
                    observed.state,
                ) == (operation, claim, corpus, repo, "executing")
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET state='uncertain' WHERE id=%s",
                    (operation,),
                )
                assert lookup.load_trusted(operation).state == "uncertain"
                with pytest.raises(CorpusMetadataConflict):
                    lookup.load_trusted(uuid4())
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "immediate", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )
