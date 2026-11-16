"""Verify the proposed metadata schema against disposable PostgreSQL."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"), reason="requires isolated PostgreSQL binaries"
)


def test_registry_enforces_private_unique_immutable_links_and_revision():
    psycopg = pytest.importorskip("psycopg")
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="corpus-metadata-", dir="/tmp") as temporary:
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
            owner, foreign, corpus = (str(uuid4()) for _ in range(3))
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS"
                )
                db.execute("CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY)")
                db.execute("INSERT INTO auth.users VALUES (%s), (%s)", (owner, foreign))
                migration = root / "supabase/migrations/20261116043420_corpus_metadata_registry.sql"
                db.execute(migration.read_text())
                db.execute("SET ROLE service_role")
                db.execute(
                    "INSERT INTO public.corpora_corpus_metadata "
                    "(id,owner_id,title,hf_repo_id,hf_repo_type) VALUES (%s,%s,%s,%s,%s)",
                    (corpus, owner, "Mini", "Exegia/corpus-one", "dataset"),
                )
                with pytest.raises(psycopg.errors.UniqueViolation):
                    db.execute(
                        "INSERT INTO public.corpora_corpus_metadata "
                        "(id,owner_id,title,hf_repo_id,hf_repo_type) VALUES (%s,%s,%s,%s,%s)",
                        (str(uuid4()), foreign, "Other", "exegia/CORPUS-one", "model"),
                    )
                row = db.execute(
                    "UPDATE public.corpora_corpus_metadata SET title=%s WHERE id=%s AND revision=1 "
                    "RETURNING revision",
                    ("First", corpus),
                ).fetchone()
                assert row == (2,)
                stale = db.execute(
                    "UPDATE public.corpora_corpus_metadata SET title=%s WHERE id=%s AND revision=1 "
                    "RETURNING revision",
                    ("Stale", corpus),
                ).fetchall()
                assert stale == []
                barrier = threading.Barrier(2)

                def concurrent_edit(title):
                    with psycopg.connect(dsn, autocommit=True) as concurrent:
                        concurrent.execute("SET ROLE service_role")
                        barrier.wait(timeout=5)
                        return concurrent.execute(
                            "UPDATE public.corpora_corpus_metadata SET title=%s "
                            "WHERE id=%s AND revision=2 RETURNING revision",
                            (title, corpus),
                        ).fetchall()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(concurrent_edit, ("Concurrent A", "Concurrent B")))
                assert sorted(map(len, results)) == [0, 1]
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    db.execute(
                        "UPDATE public.corpora_corpus_metadata SET owner_id=%s WHERE id=%s",
                        (foreign, corpus),
                    )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    db.execute("DELETE FROM public.corpora_corpus_metadata WHERE id=%s", (corpus,))
                db.execute("RESET ROLE")
                for role in ("anon", "authenticated"):
                    db.execute("SET ROLE " + role)
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        db.execute("SELECT * FROM public.corpora_corpus_metadata")
                    db.execute("RESET ROLE")
                with pytest.raises(
                    psycopg.errors.RaiseException, match="CORPUS_IDENTITY_IMMUTABLE"
                ):
                    db.execute(
                        "UPDATE public.corpora_corpus_metadata SET hf_repo_id=%s WHERE id=%s",
                        ("Exegia/other", corpus),
                    )
                db.execute("DELETE FROM public.corpora_corpus_metadata WHERE id=%s", (corpus,))
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "immediate", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )
