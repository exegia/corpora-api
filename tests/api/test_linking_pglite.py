"""Opt-in integration against a newly spawned, in-memory loopback database.

Set CORPORA_PGLITE_ROOT to a directory containing installed, pinned
@electric-sql/pglite and @electric-sql/pglite-socket packages. This is a
single-connection fixture and does not establish PostgreSQL concurrency safety.
"""

from __future__ import annotations

import os
import select
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from corpora_linking import CatalogEntry, Endpoint, Provenance, Reference, TextLocator, TextSnapshot
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from corpora_api import app as app_module
from corpora_api import linking_postgres
from corpora_api.auth import UserAuthError
from corpora_api.linking_access import endpoint_scope, evidence_endpoints
from corpora_api.linking_html_ranges import _stream
from corpora_api.linking_pdf import sha256_revision
from corpora_api.linking_runtime import AssetRecord, LinkingInventory, LinkingRuntime

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_PGLITE_ROOT"), reason="set CORPORA_PGLITE_ROOT for isolated PG fixture"
)


def test_reference_route_checks_session_space_grants_and_history(monkeypatch):
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures"
    process = subprocess.Popen(
        ["node", str(fixture_dir / "linking_pglite_server.mjs")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 30)[0], "isolated PostgreSQL did not start"
        port = int(process.stdout.readline().strip())
        assert 1 <= port <= 65535 and process.poll() is None
        dsn = f"host=127.0.0.1 port={port} dbname=postgres user=postgres sslmode=disable"
        # PGlite multiplexes sessions over one connection; prepared statements
        # persist between clients there, unlike separate PostgreSQL sessions.
        original_connect = psycopg.connect

        def connect(*args, **kwargs):
            return original_connect(*args, prepare_threshold=None, **kwargs)

        monkeypatch.setattr(psycopg, "connect", connect)
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(
                "CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS"
            )
            db.execute(
                "CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY); "
                "CREATE TABLE auth.sessions(id uuid PRIMARY KEY, "
                "user_id uuid REFERENCES auth.users(id), not_after timestamptz)"
            )
            db.execute((fixture_dir / "reference-linking-proposal.sql").read_text())

        space, other, creator, reviewer, creator_session, reviewer_session = (
            uuid4() for _ in range(6)
        )
        base = Endpoint(
            work_id="essay", edition_id="e", package_id="p", revision="1", document_id="d"
        )
        selected = Endpoint.model_validate(
            {
                **base.model_dump(),
                "locators": [
                    TextLocator(stream_id="body", start=0, end=5, exact="Quote", suffix=" tail")
                ],
            }
        )
        reference = Reference(
            source=selected,
            target=Endpoint(work_id="book"),
            provenance=Provenance(origin="manual", agent_id="forged", method="fixture"),
        )
        html = b"<html><head></head><body><p>A <em>word</em>.</p></body></html>"
        html_endpoint = Endpoint(
            work_id="essay",
            edition_id="e",
            package_id="html",
            revision=sha256_revision(html),
            document_id="html",
        )
        inventory = LinkingInventory(
            catalog=(
                CatalogEntry(work_id="essay", names=("Essay",)),
                CatalogEntry(work_id="book", names=("Book",)),
            ),
            snapshots=(TextSnapshot(endpoint=base, stream_id="body", text="Quote tail"),),
            assets=(AssetRecord(endpoint=html_endpoint, format="html", data=html),),
        )
        scopes = {
            endpoint_scope(e)[0]: endpoint_scope(e)[1]
            for e in evidence_endpoints((reference, html_endpoint))
        }
        with psycopg.connect(dsn) as db:
            db.execute("INSERT INTO auth.users VALUES (%s),(%s)", (creator, reviewer))
            db.execute(
                "INSERT INTO reference_working.spaces(id,authority_id) "
                "VALUES (%s,'test'),(%s,'other')",
                (space, other),
            )
            db.execute(
                "INSERT INTO reference_working.memberships "
                "VALUES (%s,%s,'contribute'),(%s,%s,'review'),(%s,%s,'publish')",
                (space, creator, space, reviewer, space, reviewer),
            )
            db.execute(
                "INSERT INTO auth.sessions VALUES (%s,%s,NULL),(%s,%s,NULL)",
                (creator_session, creator, reviewer_session, reviewer),
            )
            for actor in (creator, reviewer):
                for key, scope in scopes.items():
                    db.execute(
                        "INSERT INTO reference_working.resource_access VALUES (%s,%s,%s,%s)",
                        (space, actor, key, Jsonb(scope)),
                    )

        claims = {
            "creator": {"sub": str(creator), "session_id": str(creator_session)},
            "reviewer": {"sub": str(reviewer), "session_id": str(reviewer_session)},
        }

        def verify(token):
            if token not in claims:
                raise UserAuthError("invalid")
            return claims[token]

        monkeypatch.setattr(app_module, "verify_user_token", verify)
        monkeypatch.setattr(linking_postgres, "verify_user_token", verify)
        server_dsn = psycopg.conninfo.make_conninfo(dsn, options="-c role=service_role")
        monkeypatch.setattr(
            app_module.app.state,
            "linking_runtime",
            LinkingRuntime(dsn=server_dsn, inventory=inventory),
            raising=False,
        )
        a, b = {"Authorization": "Bearer creator"}, {"Authorization": "Bearer reviewer"}
        path = f"/v1/linking/{space}/references"
        body = {"reference": reference.model_dump(mode="json"), "reason": "manual"}
        with TestClient(app_module.app) as client:
            selection_path = f"/v1/linking/{space}/retrieve"
            assert client.post(
                selection_path, json=selected.model_dump(mode="json"), headers=a
            ).json() == {"text": "Quote"}
            _, spans = _stream(html)
            browser_body = {
                "original": html_endpoint.model_dump(mode="json"),
                "asset_id": "html",
                "captured_nodes": [(node_path, text) for node_path, _start, _end, text in spans],
                "start_path": spans[0][0],
                "start_utf16": 0,
                "end_path": spans[1][0],
                "end_utf16": 4,
            }
            browser = client.post(
                f"/v1/linking/{space}/browser-selection", json=browser_body, headers=a
            )
            assert browser.status_code == 200 and browser.json()["text"] == "A word", browser.text
            broken = client.post(
                f"/v1/linking/{space}/browser-selection",
                json={**browser_body, "captured_nodes": []},
                headers=a,
            )
            assert broken.status_code == 422
            created = client.post(path, json=body, headers=a)
            assert created.status_code == 200, created.text
            assert created.json()["reference"]["provenance"]["agent_id"] == str(creator)
            reference_id = created.json()["reference"]["id"]
            export_path = f"/v1/linking/{space}/export"
            assert (
                client.post(
                    export_path, json={"reference_ids": [reference_id]}, headers=a
                ).status_code
                == 422
            )
            assert (
                client.get(f"/v1/linking/{other}/references/{reference_id}", headers=a).status_code
                == 403
            )
            resolved = client.post(
                f"{path}/{reference_id}/resolve",
                json={"expected_version": 1, "reason": "resolve"},
                headers=a,
            )
            assert resolved.status_code == 200 and resolved.json()["version"] == 2, resolved.text
            approved = client.post(
                f"{path}/{reference_id}/approve",
                json={"expected_version": 2, "reason": "reviewed"},
                headers=b,
            )
            assert approved.status_code == 200 and approved.json()["version"] == 3, approved.text
            invalid = client.post(
                f"{path}/{reference_id}/reopen",
                json={"expected_version": 3, "reason": "invalid transition"},
                headers=b,
            )
            assert invalid.status_code == 422
            assert (
                client.post(path, json={**body, "expected_version": 1}, headers=a).status_code
                == 409
            )
            history = client.get(f"{path}/{reference_id}", headers=a)
            assert [row["version"] for row in history.json()["history"]] == [1, 2, 3]
            snapshot = client.post(export_path, json={"reference_ids": [reference_id]}, headers=a)
            assert snapshot.status_code == 200, snapshot.text
            publication_path = f"/v1/linking/{space}/publication/{reference_id}"
            publish_body = {
                "event_id": str(uuid4()),
                "expected_version": 3,
                "reason": "publish",
                "snapshot": snapshot.text,
            }
            assert (
                client.post(
                    publication_path + "/acknowledge", json=publish_body, headers=a
                ).status_code
                == 403
            )
            published = client.post(publication_path + "/acknowledge", json=publish_body, headers=b)
            assert published.status_code == 200 and published.json()["working_version"] == 4, (
                published.text
            )
            assert (
                client.post(publication_path + "/acknowledge", json=publish_body, headers=b).json()
                == published.json()
            )
            withdraw_body = {"event_id": str(uuid4()), "expected_version": 4, "reason": "withdraw"}
            withdrawn = client.post(
                publication_path + "/withdraw",
                json=withdraw_body,
                headers=b,
            )
            assert withdrawn.status_code == 200 and withdrawn.json()["working_version"] == 5, (
                withdrawn.text
            )
            assert (
                client.post(publication_path + "/withdraw", json=withdraw_body, headers=b).json()
                == withdrawn.json()
            )
            after_withdrawal = client.post(
                export_path, json={"reference_ids": [reference_id]}, headers=a
            )
            assert after_withdrawal.status_code == 200 and after_withdrawal.json()["entries"] == []
            reopened = client.post(
                f"{path}/{reference_id}/reopen",
                json={"expected_version": 5, "reason": "reopen"},
                headers=a,
            )
            assert reopened.status_code == 200 and reopened.json()["version"] == 6
            with psycopg.connect(dsn) as db:
                db.execute(
                    "DELETE FROM reference_working.resource_access "
                    "WHERE space_id=%s AND user_id=%s",
                    (space, reviewer),
                )
            assert client.get(f"{path}/{reference_id}", headers=b).status_code == 403
            with psycopg.connect(dsn) as db:
                db.execute("DELETE FROM auth.sessions WHERE id=%s", (creator_session,))
            assert client.get(f"{path}/{reference_id}", headers=a).status_code == 401
    finally:
        process.terminate()
        process.wait(timeout=10)
