"""Authenticated corpus uploads and the core validator process boundary."""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from corpora_api.app import app, validator
from corpora_api.auth import UserAuthError
from corpora_api.validation import ValidationBridge, ValidationUnsupported


@pytest.fixture
def user_auth(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")

    def verify(token: str) -> dict[str, str]:
        if token != "user-access-token":
            raise UserAuthError("Invalid user token")
        return {"sub": "user-1"}

    monkeypatch.setattr("corpora_api.app.verify_user_token", verify)
    return {"Authorization": "Bearer user-access-token"}


def upload(client: TestClient, headers: dict[str, str], name: str, data: bytes):
    return client.post("/v1/validate", headers=headers, files={"file": (name, data)})


def test_validation_requires_user_token_even_with_service_token(
    user_auth: dict[str, str],
) -> None:
    with TestClient(app) as client:
        assert upload(client, {}, "mini.zip", b"archive").status_code == 401
        assert (
            upload(
                client, {"Authorization": "Bearer service-token"}, "mini.zip", b"archive"
            ).status_code
            == 401
        )
        schema = client.get("/v1/openapi.json").json()
    assert schema["paths"]["/v1/validate"]["post"]["security"] == [{"SupabaseUserJWT": []}]


def test_valid_and_invalid_supported_uploads_keep_legacy_verdict_shape(
    monkeypatch: pytest.MonkeyPatch, user_auth: dict[str, str]
) -> None:
    seen: list[tuple[str, bytes]] = []

    def fake_validate(name: str, archive: bytes) -> dict:
        seen.append((name, archive))
        valid = archive == b"valid"
        return {
            "corpus": "mini",
            "path": name,
            "valid": valid,
            "stats": {"max_slot": 2} if valid else None,
            "reasons": [] if valid else ["Text-Fabric failed to load"],
            "checks": {"text_fabric_load": valid},
        }

    monkeypatch.setattr(validator, "validate", fake_validate)
    with TestClient(app) as client:
        success = upload(client, user_auth, "mini.corpus", b"valid")
        invalid = upload(client, user_auth, "bad.corpus", b"invalid")
        legacy_target = client.post("/v1/validate", headers=user_auth, json={"path": "/etc/passwd"})

    assert success.status_code == 200
    assert success.json() == {
        "corpus": "mini",
        "path": "mini.corpus",
        "valid": True,
        "stats": {"max_slot": 2},
        "reasons": [],
        "checks": {"text_fabric_load": True},
        "source_verified": None,
    }
    assert invalid.status_code == 200
    assert invalid.json()["valid"] is False
    assert legacy_target.status_code == 422
    assert seen == [("mini.corpus", b"valid"), ("bad.corpus", b"invalid")]


def test_upload_bounds_and_unsupported_formats(user_auth: dict[str, str]) -> None:
    with pytest.raises(ValidationUnsupported):
        ValidationBridge(executable="missing-corpora-validate").validate("mini.pdf", b"content")
    with pytest.raises(ValidationUnsupported):
        ValidationBridge(executable="missing-corpora-validate").validate(
            "a" * 256 + ".zip", b"content"
        )
    original = validator.executable
    validator.executable = "missing-corpora-validate"
    try:
        with TestClient(app) as client:
            unsupported = upload(client, user_auth, "mini.pdf", b"content")
            oversized = upload(client, user_auth, "mini.zip", b"x" * 3_000_001)
            missing_core = upload(client, user_auth, "mini.zip", b"archive")
    finally:
        validator.executable = original
    assert unsupported.status_code == 415
    assert oversized.status_code == 413
    assert missing_core.status_code == 503


def test_bridge_checks_core_response_and_hides_staging_path(tmp_path: Path) -> None:
    executable = tmp_path / "validator"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'corpus': 'mini', 'path': sys.argv[3], 'valid': True, "
        "'stats': None, 'reasons': [], 'checks': {'archive': True}}))\n"
    )
    executable.chmod(0o755)
    result = ValidationBridge(str(executable)).validate("mini.zip", b"archive")
    assert result["path"] == "mini.zip"
    assert "corpora-api-validate-" not in json.dumps(result)


def test_real_core_validation_upload(user_auth: dict[str, str]) -> None:
    executable = os.getenv("CORPORA_VALIDATE_BIN")
    fixture = os.getenv("CORPORA_VALIDATE_TEST_ARCHIVE")
    if not executable or not fixture:
        pytest.skip("set CORPORA_VALIDATE_BIN and CORPORA_VALIDATE_TEST_ARCHIVE")
    archive = Path(fixture)
    original = validator.executable
    validator.executable = executable
    try:
        with TestClient(app) as client:
            response = upload(client, user_auth, archive.name, archive.read_bytes())
    finally:
        validator.executable = original
    assert response.status_code == 200, response.text
    assert response.json()["valid"], response.json()["reasons"]
