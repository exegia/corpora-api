"""Cross-repository version cycle against an isolated local Supabase stack."""

from __future__ import annotations

import hashlib
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

from corpora_api.stored_revisions import Revision, StoredRevisions
from corpora_api.stored_versions import StoredVersions

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
            "manifest.yml", "name: Original\nversion: '1.0'\nwritten_date: 2024-01-02\n"
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
            return response.json()["user"]["id"]

        owner, other = signup(), signup()
        store = StoredRevisions(client)
        initial = Revision(
            owner=owner,
            filename="mini.corpus",
            revision=uuid4(),
            digest="sha256:" + hashlib.sha256(data).hexdigest(),
            version="v1.0",
        )
        store.publish(initial, data, None)
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
            assert [row["id"] for row in history] == ["v1.0", "v1.1", "v1.2", "v1.3", "v1.4"]
            assert history[2]["snapshot_revision"] == winner["revision"]
            assert history[-1]["snapshot_revision"] == restored["revision"]
            assert history[-1]["restored_from"]["snapshot_revision"] == str(initial.revision)
            assert history[-1]["author"]["sub"] == owner
            assert (
                "annotations.json" not in archive.namelist()
                or not json.loads(archive.read("annotations.json"))["nodes"]
            )
