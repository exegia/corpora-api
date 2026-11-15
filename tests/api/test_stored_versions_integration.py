"""Cross-repository version cycle against an isolated local Supabase stack."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
import yaml
from cfabric import Fabric as ContextFabric
from fastapi import HTTPException
from tf.fabric import Fabric

from corpora_api.storage import SupabaseCorpusStorage
from corpora_api.stored_import import StoredArchiveImporter
from corpora_api.stored_revisions import StoredRevisions
from corpora_api.stored_versions import StoredVersions
from corpora_api.validation import ValidationBridge

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE") or not os.getenv("CORPORA_CORE_TEST_ROOT"),
    reason="requires disposable local Supabase and proposed core checkout",
)


def test_edit_and_restore_real_stored_archive(tmp_path, monkeypatch):
    url = os.environ["API_URL"].rstrip("/")
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    core_src = str(Path(os.environ["CORPORA_CORE_TEST_ROOT"]) / "packages/corpus-validate/src")
    monkeypatch.setenv("PYTHONPATH", core_src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    root = tmp_path / "tf"
    root.mkdir()
    assert Fabric(locations=str(root), silent="deep").save(
        nodeFeatures={
            "otype": {1: "word", 2: "word", 3: "book", 4: "chapter", 5: "verse"},
            "word": {1: "Alpha ", 2: "beta"},
            "book": {3: "Mini"},
            "chapter": {4: 1},
            "verse": {5: 1},
        },
        edgeFeatures={"oslots": {3: {1, 2}, 4: {1, 2}, 5: {1, 2}}},
        metaData={
            "otype": {"valueType": "str"},
            "oslots": {"valueType": "str"},
            "word": {"valueType": "str"},
            "book": {"valueType": "str"},
            "chapter": {"valueType": "int"},
            "verse": {"valueType": "int"},
            "otext": {
                "fmt:text-orig-full": "{word}",
                "sectionTypes": "book,chapter,verse",
                "sectionFeatures": "book,chapter,verse",
            },
        },
    )
    assert ContextFabric(locations=str(root), silent="deep").loadAll(silent="deep")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, "corpora/" + path.relative_to(root).as_posix())
        archive.writestr(
            "manifest.yml", "name: Original\nversion: 1.0\nwritten_date: 2024-01-02\n"
        )
        archive.writestr("toc.yml", "version: v1.0\ndocuments: []\n")
    data = output.getvalue()
    with httpx.Client(timeout=30) as client:

        def signup():
            response = client.post(
                url + "/auth/v1/signup",
                headers={"apikey": anon, "Authorization": "Bearer " + anon},
                json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
            )
            assert response.status_code == 200
            body = response.json()
            return body["user"]["id"], body["access_token"]

        owner, _ = signup()
        other, other_token = signup()
        bucket = "legacy-test-" + uuid4().hex
        headers = {"apikey": service, "Authorization": "Bearer " + service}
        created = client.post(
            url + "/storage/v1/bucket",
            headers=headers,
            json={"id": bucket, "name": bucket, "public": False},
        )
        assert created.status_code in (200, 201)
        monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", bucket)
        object_url = f"{url}/storage/v1/object/{bucket}/{owner}/mini.corpus"
        assert client.post(object_url, headers=headers, content=data).status_code in (200, 201)
        direct = client.post(
            url + "/rest/v1/rpc/corpora_legacy_source_stamp",
            headers={"apikey": anon, "Authorization": "Bearer " + other_token},
            json={"p_owner": owner, "p_bucket": bucket, "p_filename": "mini.corpus"},
        )
        assert direct.status_code in (401, 403, 404)
        store = StoredRevisions(client)
        importer = StoredArchiveImporter(SupabaseCorpusStorage(client), store, ValidationBridge())
        with pytest.raises(HTTPException) as foreign_import:
            importer.import_archive(other, "mini.corpus")
        assert foreign_import.value.status_code == 404

        rpc = store._rpc

        def lose_import_response(name, payload):
            value = rpc(name, payload)
            if name == "corpora_stored_import":
                raise HTTPException(503, "Simulated lost response")
            return value

        monkeypatch.setattr(store, "_rpc", lose_import_response)
        with pytest.raises(HTTPException) as lost:
            importer.import_archive(owner, "mini.corpus")
        assert lost.value.status_code == 503
        monkeypatch.setattr(store, "_rpc", rpc)
        initial = importer.import_archive(owner, "mini.corpus")
        assert store.get(owner, "mini.corpus").data == data
        assert importer.import_archive(owner, "mini.corpus") == initial
        # An overwrite changes the source version even when its bytes match;
        # it must not be mistaken for a retry of the original import.
        assert client.put(object_url, headers=headers, content=data).status_code == 200
        with pytest.raises(HTTPException) as replacement:
            importer.import_archive(owner, "mini.corpus")
        assert replacement.value.status_code == 409
        race_url = f"{url}/storage/v1/object/{bucket}/{owner}/race.corpus"
        assert client.post(race_url, headers=headers, content=data).status_code in (200, 201)
        stage = store.stage

        def race_stage(fact, content):
            stage(fact, content)
            assert client.put(race_url, headers=headers, content=b"changed").status_code == 200

        monkeypatch.setattr(store, "stage", race_stage)
        with pytest.raises(HTTPException) as import_race:
            importer.import_archive(owner, "race.corpus")
        assert import_race.value.status_code == 409
        with pytest.raises(HTTPException) as not_imported:
            store.get(owner, "race.corpus")
        assert not_imported.value.status_code == 404
        monkeypatch.setattr(store, "stage", stage)
        versions = StoredVersions(store)
        edit = versions.edit(
            owner, "mini.corpus", str(initial.revision), "manifest", {"name": "Edited"}
        )
        assert edit["version"] == "v1.1"
        assert edit["result"]["written_date"] == "2024-01-02"
        with pytest.raises(HTTPException) as stale:
            versions.edit(owner, "mini.corpus", str(initial.revision), "manifest", {"name": "Lost"})
        assert stale.value.status_code == 409
        with pytest.raises(HTTPException) as foreign:
            versions.restore(other, "mini.corpus", edit["revision"], str(initial.revision))
        assert foreign.value.status_code == 404
        prepare = versions._prepare
        winner = {}

        def race_prepare(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            monkeypatch.setattr(versions, "_prepare", prepare)
            winner.update(
                versions.edit(
                    owner, "mini.corpus", edit["revision"], "manifest", {"name": "Racing"}
                )
            )
            return prepared

        monkeypatch.setattr(versions, "_prepare", race_prepare)
        with pytest.raises(HTTPException) as raced:
            versions.edit(owner, "mini.corpus", edit["revision"], "manifest", {"name": "Losing"})
        assert raced.value.status_code == 409
        assert winner["version"] == "v1.2"
        node = versions.edit(
            owner, "mini.corpus", winner["revision"], "node", {"note": "reviewed"}, node=1
        )
        assert node["version"] == "v1.3"
        restored = versions.restore(owner, "mini.corpus", node["revision"], str(initial.revision))
        assert restored["version"] == "v1.4"
        current = store.get(owner, "mini.corpus")
        assert str(current.fact.revision) == restored["revision"]
        with zipfile.ZipFile(io.BytesIO(current.data)) as archive:
            manifest = yaml.safe_load(archive.read("manifest.yml"))
            assert manifest["name"] == "Original"
            assert manifest["written_date"].isoformat() == "2024-01-02"
            history = yaml.safe_load(archive.read("history.yml"))["versions"]
            assert [row["id"] for row in history] == ["1.0", "v1.1", "v1.2", "v1.3", "v1.4"]
            assert history[2]["snapshot_revision"] == winner["revision"]
            assert history[-1]["snapshot_revision"] == restored["revision"]
            assert history[-1]["restored_from"]["snapshot_revision"] == str(initial.revision)
            assert history[-1]["author"]["sub"] == owner
            assert (
                "annotations.json" not in archive.namelist()
                or not json.loads(archive.read("annotations.json"))["nodes"]
            )
