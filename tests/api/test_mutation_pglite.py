"""Opt-in isolated PGlite/PostgREST integration; object storage is simulated.

Set CORPORA_PGLITE_ROOT to the directory containing PGlite dependencies and
CORPORA_POSTGREST_BIN to a PostgREST executable. This single-connection
fixture does not establish real PostgreSQL concurrency or Supabase behavior.
"""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from corpora_api import app as api_module
from corpora_api.auth import UserAuthError
from corpora_api.drafts import DraftStore
from corpora_api.models import AIThreadSuggestion
from corpora_api.thread_store import ThreadStore
from corpora_api.validation import ValidationBridge

pytest_plugins = ("test_archive_mutation",)

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_PGLITE_ROOT") or not os.getenv("CORPORA_POSTGREST_BIN"),
    reason="set CORPORA_PGLITE_ROOT and CORPORA_POSTGREST_BIN for isolated database fixture",
)


def test_apply_recovery_undo_and_owner_isolation(archive, monkeypatch, tmp_path):
    psycopg = pytest.importorskip("psycopg")
    path, _head, source_suggestion = archive
    owner, other, job = (str(uuid4()) for _ in range(3))
    fixture = Path(__file__).resolve().parents[1] / "fixtures"
    root = Path(__file__).resolve().parents[2]
    pg = subprocess.Popen(
        ["node", str(fixture / "mutation_pglite_server.mjs")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    rest = None
    try:
        assert pg.stdout is not None and select.select([pg.stdout], [], [], 30)[0]
        pg_port = int(pg.stdout.readline().strip())
        dsn = f"host=127.0.0.1 port={pg_port} dbname=postgres user=postgres sslmode=disable"
        with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as db:
            db.execute(
                "CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS"
            )
            db.execute((fixture / "ai-hosted-bootstrap.sql").read_text())
            db.execute(
                "CREATE TABLE auth.users(id uuid PRIMARY KEY); "
                "ALTER TABLE public.corpus_documents ADD COLUMN name text, ADD COLUMN source text, "
                "ADD COLUMN path text, ADD COLUMN filename text, "
                "ADD COLUMN job_id text, ADD COLUMN status text"
            )
            db.execute("INSERT INTO auth.users VALUES (%s),(%s)", (owner, other))
            db.execute("INSERT INTO public.users VALUES (%s),(%s)", (owner, other))
            db.execute(
                (
                    root / "supabase/migrations/20261115042900_legacy_ai_draft_registry.sql"
                ).read_text()
            )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            rest_port = probe.getsockname()[1]
        config = tmp_path / "postgrest.conf"
        config.write_text(
            f'db-uri = "postgresql://postgres@127.0.0.1:{pg_port}/postgres?sslmode=disable"\n'
            'db-schemas = "public"\ndb-anon-role = "service_role"\n'
            "db-pool = 1\ndb-prepared-statements = false\n"
            "db-channel-enabled = false\ndb-config = false\n"
            f'server-host = "127.0.0.1"\nserver-port = {rest_port}\n'
        )
        rest = subprocess.Popen(
            [os.environ["CORPORA_POSTGREST_BIN"], str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        forward = httpx.Client(timeout=30)
        endpoint = f"http://127.0.0.1:{rest_port}"
        for _ in range(100):
            try:
                forward.get(endpoint + "/")
                break
            except httpx.ConnectError:
                assert rest.poll() is None
                time.sleep(0.05)
        else:
            pytest.fail("isolated PostgREST did not start")
        objects = {}
        lose_response = [True]
        reject_before_publish = [False]

        def transport(request):
            if request.url.path.startswith("/rest/v1/"):
                is_rpc = (
                    request.url.path.endswith("/rpc/corpora_ai_storage")
                    and request.method == "POST"
                )
                action = json.loads(request.content).get("p_action") if is_rpc else None
                if action == "publish" and reject_before_publish[0]:
                    reject_before_publish[0] = False
                    race_row = api_module.thread_store.get(owner, thread["id"])
                    race_row.data.suggestions[-1].suggestion.status = "rejected"
                    assert api_module.thread_store.replace(race_row)
                response = forward.request(
                    request.method,
                    endpoint + request.url.path.removeprefix("/rest/v1"),
                    params=request.url.params,
                    content=request.content,
                    headers={
                        key: value
                        for key, value in request.headers.items()
                        if key.lower() in ("content-type", "accept", "prefer")
                    },
                )
                if action == "publish" and response.status_code == 200 and lose_response[0]:
                    lose_response[0] = False
                    raise httpx.ReadTimeout("fixture response loss after commit", request=request)
                return httpx.Response(
                    response.status_code, headers=dict(response.headers), content=response.content
                )
            key = request.url.path.removeprefix("/storage/v1/object/corpus-ai-drafts/")
            if request.method == "POST":
                if key in objects:
                    return httpx.Response(409)
                objects[key] = request.content
                return httpx.Response(201)
            if request.method == "GET":
                return (
                    httpx.Response(200, content=objects[key])
                    if key in objects
                    else httpx.Response(404)
                )
            raise AssertionError("unexpected object request")

        client = httpx.Client(transport=httpx.MockTransport(transport))

        class Jobs:
            def fetch(self, requested_owner, requested_job):
                if (requested_owner, requested_job) != (owner, job):
                    raise HTTPException(404, "Job not found")
                return "source.corpus", path.read_bytes()

        def verify(token):
            if token in (owner, other):
                return {"sub": token}
            raise UserAuthError("invalid")

        monkeypatch.setenv("SUPABASE_URL", "https://fixture.invalid")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-service-key")
        monkeypatch.setenv("CORPORA_AI_MUTATIONS_ENABLED", "true")
        monkeypatch.setattr(api_module, "verify_user_token", verify)
        monkeypatch.setattr(api_module, "draft_store", DraftStore(client))
        monkeypatch.setattr(api_module, "thread_store", ThreadStore(client))
        monkeypatch.setattr(api_module, "conversion_artifact_store", Jobs())
        monkeypatch.setattr(api_module, "validator", ValidationBridge())
        a, b = {"Authorization": "Bearer " + owner}, {"Authorization": "Bearer " + other}
        with TestClient(api_module.app) as api:
            created = api.post("/v1/ai/drafts", json={"job_id": job}, headers=a)
            assert created.status_code == 200, created.text
            draft = created.json()
            scope = source_suggestion.scope.model_dump(mode="json")
            scope["corpus"] = draft["corpus"]
            thread = api.post("/v1/ai/threads", json={"scope": scope}, headers=a)
            assert thread.status_code == 200, thread.text
            thread = thread.json()
            suggestion_id = thread["id"] + ":" + str(uuid4())
            suggestion = source_suggestion.model_dump(mode="json")
            suggestion.update({"id": suggestion_id, "scope": scope})
            row = api_module.thread_store.get(owner, thread["id"])
            row.data.suggestions.append(
                AIThreadSuggestion.model_validate(
                    {
                        "section_id": thread["sections"][0]["id"],
                        "suggestion": suggestion,
                        "created_at": thread["created_at"],
                    }
                )
            )
            assert api_module.thread_store.replace(row)
            url = f"/v1/ai/suggestions/{suggestion_id}/apply"
            assert api.post(url, json={}, headers=b).status_code == 404
            assert api.post(url, json={}, headers=a).status_code == 503
            applied = api.post(url, json={}, headers=a)
            assert applied.status_code == 200, applied.text
            assert api.post(url, json={}, headers=a).json() == applied.json()
            change = applied.json()["change"]["change_id"]
            assert api.post(f"/v1/ai/changes/{change}/undo", headers=b).status_code == 404
            undone = api.post(f"/v1/ai/changes/{change}/undo", headers=a)
            assert undone.status_code == 200, undone.text
            assert api.post(f"/v1/ai/changes/{change}/undo", headers=a).json() == undone.json()
            history = api.get("/v1/ai/changes", params={"corpus": draft["corpus"]}, headers=a)
            assert history.status_code == 200 and len(history.json()["entries"]) == 2
            current = api.get("/v1/ai/drafts/" + draft["id"], headers=a).json()
            original_bytes = api.get("/v1/ai/drafts/" + draft["id"] + "/archive", headers=a).content
            second_id = thread["id"] + ":" + str(uuid4())
            second_scope = {**scope, "version": current["version"]}
            second = {
                **suggestion,
                "id": second_id,
                "scope": second_scope,
                "base_version": current["version"],
            }
            row = api_module.thread_store.get(owner, thread["id"])
            row.data.suggestions.append(
                AIThreadSuggestion.model_validate(
                    {
                        "section_id": thread["sections"][0]["id"],
                        "suggestion": second,
                        "created_at": thread["created_at"],
                    }
                )
            )
            assert api_module.thread_store.replace(row)
            reject_before_publish[0] = True
            assert (
                api.post(f"/v1/ai/suggestions/{second_id}/apply", json={}, headers=a).status_code
                == 409
            )
            assert not reject_before_publish[0]
            assert (
                api.get("/v1/ai/drafts/" + draft["id"] + "/archive", headers=a).content
                == original_bytes
            )
            assert (
                len(
                    api.get("/v1/ai/changes", params={"corpus": draft["corpus"]}, headers=a).json()[
                        "entries"
                    ]
                )
                == 2
            )

    finally:
        if rest is not None:
            rest.terminate()
            rest.wait(timeout=10)
        pg.terminate()
        pg.wait(timeout=10)
