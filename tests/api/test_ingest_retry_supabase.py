"""Exercise concurrent retries with disposable local PostgREST and Storage."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from corpora_api.ingest import build_plain_graph
from corpora_api.ingest_requests import IngestRequestConflict, input_fingerprint, reserve_request
from corpora_api.ingest_store import IngestStore

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_concurrent_retry_reserves_one_job_and_one_graph(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    service = os.environ["SERVICE_ROLE_KEY"]
    bucket = "ingest-retry-" + uuid4().hex
    owner = str(uuid4())
    for name, value in {
        "SUPABASE_URL": url,
        "SUPABASE_SERVICE_ROLE_KEY": service,
        "SUPABASE_JOBS_BUCKET": bucket,
    }.items():
        monkeypatch.setenv(name, value)
    auth = {"apikey": service, "Authorization": "Bearer " + service}
    fingerprint = input_fingerprint("a.txt", b"hello", name="", language="und", corpus_id="")
    root = Path(__file__).resolve().parents[2]
    migration = next((root / "supabase/migrations").glob("*_ingest_request_reservations.sql"))
    jobs_sql = Path(os.environ["CORPORA_TEST_JOBS_SCHEMA"]).read_text()
    created_bucket = False
    job_ids = []
    with httpx.Client(timeout=15) as client, psycopg.connect(dsn, autocommit=True) as db:
        assert not db.execute(
            "select to_regclass('public.conversion_jobs') is not null"
        ).fetchone()[0]
        assert not db.execute(
            "select to_regclass('public.corpora_ingest_requests') is not null"
        ).fetchone()[0]
        try:
            db.execute(jobs_sql)
            db.execute(migration.read_text())
            db.execute("notify pgrst, 'reload schema'")
            client.post(
                url + "/storage/v1/bucket",
                headers=auth,
                json={"id": bucket, "name": bucket, "public": False},
            ).raise_for_status()
            created_bucket = True

            def attempt(_):
                with httpx.Client(timeout=15) as request_client:
                    store = IngestStore(request_client)
                    job = reserve_request(store, owner, "same-key", fingerprint)
                    graph = json.dumps(build_plain_graph("a.txt", b"hello").to_wire()).encode()
                    store.save(owner, job, "a.txt", "a", graph, retry=True)
                    return job

            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = list(pool.map(attempt, range(2)))
            assert jobs[0] == jobs[1]
            job_id = jobs[0]
            job_ids.append(job_id)
            store = IngestStore()
            assert store.download(owner, job_id)[0]
            assert (
                db.execute(
                    "select count(*) from public.conversion_jobs where owner=%s", (owner,)
                ).fetchone()[0]
                == 1
            )
            with pytest.raises(IngestRequestConflict):
                reserve_request(store, owner, "same-key", "f" * 64)

            class LostResponses(httpx.BaseTransport):
                def __init__(self):
                    self.inner = httpx.HTTPTransport()
                    self.lost = set()

                def handle_request(self, request):
                    response = self.inner.handle_request(request)
                    path = request.url.path
                    target = (
                        path.endswith("/corpora_ingest_requests")
                        or "/storage/v1/object/" in path
                        or path.endswith("/conversion_jobs")
                    )
                    if request.method == "POST" and target and path not in self.lost:
                        response.read()
                        assert response.status_code in (200, 201, 204)
                        response.close()
                        self.lost.add(path)
                        raise httpx.ReadTimeout("simulated lost response", request=request)
                    return response

                def close(self):
                    self.inner.close()

            transport = LostResponses()
            with httpx.Client(transport=transport, timeout=15) as simulated:
                retry_store = IngestStore(simulated)
                lost_job = reserve_request(retry_store, owner, "lost-responses", fingerprint)
                job_ids.append(lost_job)
                graph = json.dumps(build_plain_graph("a.txt", b"hello").to_wire()).encode()
                retry_store.save(owner, lost_job, "a.txt", "a", graph, retry=True)
                assert retry_store.download(owner, lost_job)[0] == graph
                assert len(transport.lost) == 3

            denied = client.get(
                url + "/rest/v1/corpora_ingest_requests",
                headers={
                    "apikey": os.environ["ANON_KEY"],
                    "Authorization": "Bearer " + os.environ["ANON_KEY"],
                },
            )
            assert denied.status_code in (401, 403, 404)
        finally:
            if created_bucket:
                for job_id in job_ids:
                    client.delete(
                        f"{url}/storage/v1/object/{bucket}/conversion-jobs/{job_id}.graph.json",
                        headers=auth,
                    ).raise_for_status()
                client.delete(url + "/storage/v1/bucket/" + bucket, headers=auth).raise_for_status()
            db.execute("drop table if exists public.corpora_ingest_requests")
            db.execute("drop table if exists public.conversion_jobs")
