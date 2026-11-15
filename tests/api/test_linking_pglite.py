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
from corpora_linking import (
    BibleBook,
    CatalogEntry,
    Endpoint,
    Provenance,
    Reference,
    ScholarlyCitationMention,
    TextLocator,
    TextSnapshot,
)
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from corpora_api import app as app_module
from corpora_api import linking_postgres
from corpora_api.auth import UserAuthError
from corpora_api.linking_access import endpoint_scope, evidence_endpoints
from corpora_api.linking_html_ranges import _stream
from corpora_api.linking_pdf import sha256_revision
from corpora_api.linking_publication import SnapshotPublicationAdapter
from corpora_api.linking_runtime import (
    AssetRecord,
    BibleDetectionContext,
    LinkingInventory,
    LinkingRuntime,
)

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
        conversion_data = (
            b"<html><head></head><body><p>Compare John <em>3:16</em>.</p></body></html>"
        )
        original = Endpoint(
            work_id="essay",
            edition_id="e",
            package_id="convert-html",
            revision=sha256_revision(conversion_data),
            document_id="asset",
        )
        converted = Endpoint(
            work_id="essay",
            edition_id="e",
            package_id="convert-text",
            revision=sha256_revision(b"Compare John 3:16.\n"),
            document_id="body",
        )
        inventory = LinkingInventory(
            bible=BibleDetectionContext(
                books=(BibleBook(work_id="john", aliases=("John",)),),
                profile="fixture",
                scheme_id="fixture",
                scheme_version="1",
                detector_revision="1",
            ),
            catalog=(
                CatalogEntry(work_id="essay", names=("Essay",)),
                CatalogEntry(work_id="book", names=("Book",)),
            ),
            snapshots=(TextSnapshot(endpoint=base, stream_id="body", text="Quote tail"),),
            assets=(
                AssetRecord(endpoint=html_endpoint, format="html", data=html),
                AssetRecord(endpoint=original, format="html", data=conversion_data),
            ),
        )
        scopes = {
            endpoint_scope(e)[0]: endpoint_scope(e)[1]
            for e in evidence_endpoints(
                (
                    reference,
                    html_endpoint,
                    original,
                    converted,
                    Endpoint(work_id="john"),
                    Endpoint(work_id="other"),
                )
            )
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

        class Recognizer:
            def recognize(self, snapshot):
                start = snapshot.text.index("John 3:16")
                return (
                    ScholarlyCitationMention(
                        selection=TextLocator(
                            stream_id="body", start=start, end=start + 9, exact="John 3:16"
                        ),
                        cited_name="John",
                    ),
                )

        monkeypatch.setattr(
            app_module.app.state,
            "linking_runtime",
            LinkingRuntime(
                dsn=server_dsn,
                inventory=inventory,
                scholarly_recognizer=Recognizer(),
                scholarly_revision="fixture-v1",
            ),
            raising=False,
        )
        a, b = {"Authorization": "Bearer creator"}, {"Authorization": "Bearer reviewer"}
        path = f"/v1/linking/{space}/references"
        body = {"reference": reference.model_dump(mode="json"), "reason": "manual"}
        with TestClient(app_module.app) as client:
            conversion_url = f"/v1/linking/{space}/conversion-events"
            conversion_body = {
                "original": original.model_dump(mode="json"),
                "converted": converted.model_dump(mode="json"),
                "asset_id": "convert-html",
                "event_id": str(uuid4()),
                "reason": "converted",
            }
            first_conversion = client.post(conversion_url, json=conversion_body, headers=a)
            assert first_conversion.status_code == 200, first_conversion.text
            assert len(first_conversion.json()["report"]["references"]) == 1
            assert (
                client.post(conversion_url, json=conversion_body, headers=a).json()
                == first_conversion.json()
            )
            assert (
                client.post(
                    conversion_url, json={**conversion_body, "asset_id": "forged"}, headers=a
                ).status_code
                == 409
            )
            assert (
                client.post(
                    f"/v1/linking/{other}/conversion-events", json=conversion_body, headers=a
                ).status_code
                == 403
            )
            scholarly_url = f"/v1/linking/{space}/scholarly-conversion-events"
            scholarly_body = {
                key: value for key, value in conversion_body.items() if key != "event_id"
            }
            scholarly_body["event_ids"] = [str(uuid4())]
            scholarly = client.post(scholarly_url, json=scholarly_body, headers=a)
            assert scholarly.status_code == 200, scholarly.text
            assert len(scholarly.json()["discoveries"]) == 1
            assert (
                client.post(scholarly_url, json=scholarly_body, headers=a).json()
                == scholarly.json()
            )
            assert (
                client.post(
                    scholarly_url, json={**scholarly_body, "event_ids": []}, headers=a
                ).status_code
                == 422
            )
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
            import_plan = client.post(
                f"/v1/linking/{space}/import-plan", json={"snapshot": snapshot.text}, headers=a
            )
            assert import_plan.status_code == 200, import_plan.text
            assert len(import_plan.json()["decisions"]) == 1
            stale_import = client.post(
                f"/v1/linking/{space}/import",
                json={
                    "snapshot": snapshot.text,
                    "reference_id": reference_id,
                    "expected_version": 1,
                    "reason": "import",
                },
                headers=a,
            )
            assert stale_import.status_code == 409, stale_import.text
            imported_ref = reference.model_copy(
                update={"id": uuid4(), "review": "approved", "resolution": "resolved"}
            )
            external_snapshot = (
                SnapshotPublicationAdapter("upstream").export((imported_ref,)).decode()
            )
            import_body = {
                "snapshot": external_snapshot,
                "reference_id": str(imported_ref.id),
                "reason": "import",
            }
            imported = client.post(f"/v1/linking/{space}/import", json=import_body, headers=a)
            assert imported.status_code == 200, imported.text
            assert imported.json()["version"] == 1
            imported_state = client.get(f"{path}/{imported_ref.id}", headers=a)
            assert (
                imported_state.status_code == 200
                and imported_state.json()["history"][-1]["reference"]["review"] == "pending"
            ), imported_state.text
            assert (
                client.post(f"/v1/linking/{space}/import", json=import_body, headers=a).status_code
                == 409
            )
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
            discovery_path = f"/v1/linking/{space}/discoveries"
            mention = ScholarlyCitationMention(
                selection=TextLocator(
                    stream_id="body", start=0, end=5, exact="Quote", suffix=" tail"
                ),
                cited_name="Quote",
            )
            discovery_body = {
                "source": base.model_dump(mode="json"),
                "mention": mention.model_dump(mode="json"),
                "event_id": str(uuid4()),
                "reason": "discovered",
            }
            registered = client.post(discovery_path, json=discovery_body, headers=a)
            assert registered.status_code == 200, registered.text
            assert registered.json()["discovery"]["work_identification"]["status"] == "unresolved"
            assert (
                client.post(discovery_path, json=discovery_body, headers=a).json()
                == registered.json()
            )
            changed = client.post(
                discovery_path,
                json={
                    **discovery_body,
                    "mention": {**discovery_body["mention"], "cited_name": "Other"},
                },
                headers=a,
            )
            assert changed.status_code == 409
            discovery_id = registered.json()["discovery"]["id"]
            updated_inventory = inventory.model_copy(
                update={
                    "catalog": (
                        CatalogEntry(work_id="essay", names=("Essay",)),
                        CatalogEntry(work_id="book", names=("Quote",)),
                        CatalogEntry(work_id="other", names=("Quote",)),
                    )
                }
            )
            app_module.app.state.linking_runtime = LinkingRuntime(
                dsn=server_dsn, inventory=updated_inventory
            )
            identified = client.post(
                f"{discovery_path}/{discovery_id}/identify",
                json={"expected_version": 1, "reason": "catalog"},
                headers=a,
            )
            assert identified.status_code == 200 and identified.json()["version"] == 2, (
                identified.text
            )
            choose = {"expected_version": 2, "reason": "review", "work_id": "book"}
            assert (
                client.post(
                    f"{discovery_path}/{discovery_id}/select", json=choose, headers=a
                ).status_code
                == 403
            )
            chosen = client.post(f"{discovery_path}/{discovery_id}/select", json=choose, headers=b)
            assert chosen.status_code == 200 and chosen.json()["version"] == 3, chosen.text
            assert (
                client.post(
                    f"{discovery_path}/{discovery_id}/select", json=choose, headers=b
                ).status_code
                == 409
            )
            discovery_history = client.get(f"{discovery_path}/{discovery_id}", headers=a)
            assert [row["action"] for row in discovery_history.json()["history"]] == [
                "register",
                "identify",
                "select",
            ]
            selected_id = chosen.json()["selected_reference_id"]
            assert client.get(f"{path}/{selected_id}", headers=a).status_code == 200
            second = client.post(
                discovery_path, json={**discovery_body, "event_id": str(uuid4())}, headers=a
            )
            assert second.status_code == 200, second.text
            second_id = second.json()["discovery"]["id"]
            rejected = client.post(
                f"{discovery_path}/{second_id}/reject",
                json={"expected_version": 1, "reason": "reject"},
                headers=b,
            )
            assert rejected.status_code == 200 and rejected.json()["version"] == 2
            assert (
                client.post(
                    f"{discovery_path}/{second_id}/select",
                    json={"expected_version": 2, "reason": "invalid", "work_id": "book"},
                    headers=b,
                ).status_code
                == 422
            )
            with psycopg.connect(dsn) as db:
                db.execute(
                    "DELETE FROM reference_working.resource_access "
                    "WHERE space_id=%s AND user_id=%s",
                    (space, reviewer),
                )
            assert client.get(f"{path}/{reference_id}", headers=b).status_code == 403
            assert client.get(f"{discovery_path}/{discovery_id}", headers=b).status_code == 403
            with psycopg.connect(dsn) as db:
                db.execute("DELETE FROM auth.sessions WHERE id=%s", (creator_session,))
            assert client.get(f"{path}/{reference_id}", headers=a).status_code == 401
            assert client.get(f"{discovery_path}/{discovery_id}", headers=a).status_code == 401
    finally:
        process.terminate()
        process.wait(timeout=10)
