"""Exercise import against a Storage writer on a disposable local stack."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from corpora_api.stored_revisions import Revision, StoredRevisions

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_source_row_lock_blocks_writer_only_until_import_commits(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    service = os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    headers = {"apikey": service, "Authorization": "Bearer " + service}
    owner, bucket = str(uuid4()), "lock-test-" + uuid4().hex
    name, data = "lock.corpus", b"source snapshot"
    path = f"{url}/storage/v1/object/{bucket}/{owner}/{name}"
    with httpx.Client(timeout=30) as client:
        assert client.post(
            url + "/storage/v1/bucket",
            headers=headers,
            json={"id": bucket, "name": bucket, "public": False},
        ).status_code in (200, 201)
        assert client.post(path, headers=headers, content=data).status_code in (200, 201)
        store = StoredRevisions(client)
        stamp = store._rpc(
            "corpora_legacy_source_stamp",
            {"p_owner": owner, "p_bucket": bucket, "p_filename": name},
        )
        fact = Revision(
            owner=owner,
            filename=name,
            revision=uuid4(),
            digest="sha256:" + hashlib.sha256(data).hexdigest(),
            version="v1.0",
        )
        store.stage(fact, data)
        outcome, errors = [], []

        def replace():
            try:
                outcome.append(
                    client.put(path, headers=headers, content=b"legacy overwrite").status_code
                )
            except Exception as error:
                errors.append(error)

        writer = threading.Thread(target=replace)
        with psycopg.connect(dsn) as importer:
            importer.execute("SET ROLE service_role")
            importer.execute(
                "SELECT public.corpora_stored_import(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    owner,
                    bucket,
                    name,
                    stamp["id"],
                    stamp["version"],
                    str(fact.revision),
                    fact.digest,
                    fact.version,
                ),
            )
            try:
                writer.start()
                with psycopg.connect(dsn, autocommit=True) as monitor:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        blocked = monitor.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE %s = ANY(pg_blocking_pids(pid)))",
                            (importer.info.backend_pid,),
                        ).fetchone()[0]
                        if blocked:
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("Storage writer did not wait on the source row lock")
            finally:
                importer.commit()
                writer.join(20)
        assert not writer.is_alive() and not errors and outcome == [200]
        # Once the lock releases, an old writer can still change the flat object.
        # This proves cutover must coordinate legacy access; the registry retains
        # the verified bytes that were imported before that later overwrite.
        assert client.get(path, headers=headers).content == b"legacy overwrite"
        assert store.get(owner, name).data == data
