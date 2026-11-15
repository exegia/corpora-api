"""Core-backed regression for compressed archives above the expansion limit."""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from corpora_api.version_process import VersionProcessError, run_version


@pytest.mark.skipif(
    not os.getenv("CORPORA_CORE_TEST_ROOT"), reason="requires proposed core checkout"
)
def test_core_expansion_limit_is_reported_as_too_large(monkeypatch):
    core_src = str(Path(os.environ["CORPORA_CORE_TEST_ROOT"]) / "packages/corpus-validate/src")
    monkeypatch.setenv("PYTHONPATH", core_src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        with archive.open("corpora/oversized.tf", "w") as member:
            for _ in range(64):
                member.write(b"x" * 1_000_000)
            member.write(b"x")
    data = stream.getvalue()
    assert len(data) < 3_000_000
    with pytest.raises(VersionProcessError) as error:
        run_version(
            data,
            None,
            "edit",
            name="mini.corpus",
            expected_digest="sha256:" + hashlib.sha256(data).hexdigest(),
            expected_version="1.0",
            owner=str(uuid4()),
            current_revision=str(uuid4()),
            new_revision=str(uuid4()),
            operation="manifest",
            updates={"name": "test"},
            node=None,
        )
    assert error.value.kind == "too_large"
