"""Opt-in real local Auth, PostgREST and Storage draft-scope test."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from tf.fabric import Fabric

from corpora_api import app as app_module
from corpora_api.auth import verify_user_token
from corpora_api.drafts import DraftStore
from corpora_api.thread_store import ThreadStore

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_draft_scope_uses_real_local_auth_and_storage(tmp_path, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    monkeypatch.setattr(app_module, "draft_store", DraftStore())
    monkeypatch.setattr(app_module, "thread_store", ThreadStore())
    root = tmp_path / "tf"
    root.mkdir()
    assert Fabric(locations=str(root), silent="deep").save(
        nodeFeatures={"otype": {1: "word", 2: "book"}, "book": {2: "Mini"}, "text": {1: "A"}},
        edgeFeatures={"oslots": {2: {1}}},
        metaData={
            "otype": {"valueType": "str"},
            "oslots": {"valueType": "str"},
            "book": {"valueType": "str"},
            "text": {"valueType": "str"},
            "otext": {
                "sectionTypes": "book",
                "sectionFeatures": "book",
                "fmt:text-orig-full": "{text}",
            },
        },
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.yml", "name: Mini\nversion: '1.0'\nformat: corpus\n")
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, f"corpora/{path.relative_to(root)}")
    data = stream.getvalue()
    doc, revision, users = str(uuid4()), str(uuid4()), []
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    object_url = None
    with httpx.Client(timeout=15) as client, psycopg.connect(dsn, autocommit=True) as db:
        try:
            for _ in range(2):
                response = client.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon, "Authorization": "Bearer " + anon},
                    json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
                )
                response.raise_for_status()
                token = response.json()["access_token"]
                users.append((verify_user_token(token)["sub"], token))
            owner, token = users[0]
            db.execute(
                "INSERT INTO public.users(id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (owner,)
            )
            key = f"{owner}/ai/{doc}/{revision}.corpus"
            object_url = f"{url}/storage/v1/object/corpus-ai-drafts/{key}"
            response = client.post(object_url, headers=service_headers, content=data)
            response.raise_for_status()
            db.execute(
                "INSERT INTO public.corpus_documents"
                "(id,name,source,path,filename,status,ai_private) "
                "VALUES (%s,'Mini','upload',%s,%s,'converted',true)",
                (doc, key, doc + ".corpus"),
            )
            db.execute(
                "INSERT INTO public.corpus_ai_drafts"
                "(document_id,owner,corpus,bucket,object_key,revision,digest,version) "
                "VALUES (%s,%s,%s,'corpus-ai-drafts',%s,%s,%s,'1.0')",
                (
                    doc,
                    owner,
                    "draft:" + doc,
                    key,
                    revision,
                    "sha256:" + hashlib.sha256(data).hexdigest(),
                ),
            )
            scope = {
                "corpus": "draft:" + doc,
                "level": "word",
                "node_id": 1,
                "label": "word",
                "version": "1.0",
            }
            with TestClient(app_module.app) as api:
                own_headers = {"Authorization": "Bearer " + token}
                foreign_headers = {"Authorization": "Bearer " + users[1][1]}
                own = api.post("/v1/ai/validate", json={"scope": scope}, headers=own_headers)
                foreign = api.post(
                    "/v1/ai/validate", json={"scope": scope}, headers=foreign_headers
                )
                assert own.status_code == 200 and own.json()["checked_nodes"] == 1
                assert foreign.status_code == 404
                create_headers = {**own_headers, "Idempotency-Key": str(uuid4())}
                created = api.post("/v1/ai/threads", json={"scope": scope}, headers=create_headers)
                assert created.status_code == 200, created.text
                thread = created.json()
                thread_url = "/v1/ai/threads/" + thread["id"]
                assert (
                    api.post("/v1/ai/threads", json={"scope": scope}, headers=create_headers).json()
                    == thread
                )
                assert api.get(thread_url, headers=foreign_headers).status_code == 404
                listed = api.get(
                    "/v1/ai/threads", params={"corpus": scope["corpus"]}, headers=own_headers
                )
                assert [item["id"] for item in listed.json()["threads"]] == [thread["id"]]
                forked = api.post(
                    thread_url + "/sections",
                    json={"scope": scope},
                    headers={**own_headers, "Idempotency-Key": str(uuid4())},
                )
                assert forked.status_code == 200 and len(forked.json()["sections"]) == 2
                message = {"section_id": thread["sections"][0]["id"], "content": "hello"}
                message_headers = {**own_headers, "Idempotency-Key": str(uuid4())}

                def post_message():
                    return api.post(thread_url + "/messages", json=message, headers=message_headers)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    replies = list(pool.map(lambda _: post_message(), range(2)))
                assert all(reply.status_code == 200 for reply in replies)
                assert replies[0].json() == replies[1].json()
                monkeypatch.setattr(app_module, "thread_store", ThreadStore())
                persisted = api.get(thread_url, headers=own_headers)
                messages = api.get(thread_url + "/messages", headers=own_headers)
                suggestions = api.get(thread_url + "/suggestions", headers=own_headers)
                assert persisted.status_code == 200 and len(persisted.json()["sections"]) == 2
                assert [item["content"] for item in messages.json()["messages"]] == ["hello"]
                assert suggestions.status_code == 200 and suggestions.json()["suggestions"] == []
                suggestion = {
                    "section_id": thread["sections"][0]["id"],
                    "created_at": thread["created_at"],
                    "suggestion": {
                        "id": "suggestion-1",
                        "scope": scope,
                        "kind": "text",
                        "target_node": 1,
                        "diff": [{"field": "text", "old": "A", "new": "B"}],
                        "rationale": "Example",
                        "base_version": "1.0",
                        "content_hash": "sha256:test",
                    },
                }
                db.execute(
                    "UPDATE public.corpus_ai_threads SET "
                    "data=jsonb_set(data,'{suggestions}',%s::jsonb), revision=revision+1 "
                    "WHERE id=%s AND owner=%s",
                    (Jsonb([suggestion]), thread["id"], owner),
                )
                found = api.get(thread_url + "/suggestions", headers=own_headers)
                hidden = api.get(thread_url + "/suggestions", headers=foreign_headers)
                assert found.status_code == 200
                assert found.json()["suggestions"][0]["suggestion"]["id"] == "suggestion-1"
                assert hidden.status_code == 404
                script = (
                    "import json,sys; from corpora_api.thread_store import ThreadStore; "
                    "r=ThreadStore().get(sys.argv[1],sys.argv[2]); "
                    "print(json.dumps({'messages':len(r.data.messages),"
                    "'suggestions':len(r.data.suggestions)}))"
                )
                child = subprocess.run(
                    [sys.executable, "-c", script, owner, thread["id"]],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                )
                assert json.loads(child.stdout) == {"messages": 1, "suggestions": 1}
                response = client.put(object_url, headers=service_headers, content=b"tampered")
                response.raise_for_status()
                tampered = api.post("/v1/ai/validate", json={"scope": scope}, headers=own_headers)
                assert tampered.status_code == 409
        finally:
            if users:
                db.execute("DELETE FROM public.corpus_ai_threads WHERE owner=%s", (users[0][0],))
            db.execute("DELETE FROM public.corpus_ai_drafts WHERE document_id=%s", (doc,))
            db.execute("DELETE FROM public.corpus_documents WHERE id=%s", (doc,))
            if users:
                db.execute("DELETE FROM public.users WHERE id=%s", (users[0][0],))
            if object_url:
                client.delete(object_url, headers=service_headers).raise_for_status()
            for user, _ in users:
                client.delete(
                    url + "/auth/v1/admin/users/" + user, headers=service_headers
                ).raise_for_status()
