"""Contract checks for the HTTP and MCP adapters."""

import asyncio
import hashlib
import os
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.asgi_transport import StreamingASGITransport, run_asgi_lifespan

from corpora_api.app import app, bridge, mcp
from corpora_api.core import CoreBridge, CoreProtocolError, CoreUnavailable


def test_structure_uses_core_and_documents_response(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "<root/>"
    expected = {
        "schema_version": 1,
        "source_id": "book.xml",
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "diagnostics": [],
    }
    requests: list[dict[str, object]] = []

    def fake_run(request: dict[str, object], *, needs_dictionaries: bool = False) -> dict:
        requests.append(request)
        assert not needs_dictionaries
        return expected

    monkeypatch.setattr(bridge, "run", fake_run)
    with TestClient(app) as client:
        response = client.post("/v1/checks/structure", json={"source_id": "book.xml", "text": text})
        schema = client.get("/openapi.json").json()

    assert response.status_code == 200
    assert response.json() == expected
    assert requests == [{"operation": "structure", "source_id": "book.xml", "text": text}]
    assert "/v1/checks/structure" in schema["paths"]
    assert "DiagnosticReport" in schema["components"]["schemas"]


def test_missing_dictionary_is_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "dictionaries", None)
    with TestClient(app) as client:
        response = client.post("/v1/checks/spelling", json={"text": "hello", "locale": "en-US"})
    assert response.status_code == 503
    assert "CORPORA_DICTIONARIES_DIR" in response.json()["detail"]


def test_mcp_has_curated_core_tool_names() -> None:
    async def list_names() -> set[str]:
        async with Client(mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    assert asyncio.run(list_names()) == {
        "check_xml_structure",
        "check_tei_purpose",
        "detect_text_language",
        "check_text_spelling",
    }


def test_mounted_mcp_serves_core_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(request: dict[str, object], *, needs_dictionaries: bool = False) -> dict:
        assert request["operation"] == "structure"
        return {"source_id": request["source_id"], "diagnostics": []}

    monkeypatch.setattr(bridge, "run", fake_run)

    def http_client_factory(**kwargs: object) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=StreamingASGITransport(app), **kwargs)

    async def call_tool() -> dict:
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/", httpx_client_factory=http_client_factory
        )
        async with run_asgi_lifespan(app):
            async with Client(transport) as client:
                result = await client.call_tool(
                    "check_xml_structure", {"text": "<root/>", "source_id": "book.xml"}
                )
                return result.data

    assert asyncio.run(call_tool()) == {"source_id": "book.xml", "diagnostics": []}


def test_real_core_structure_and_language() -> None:
    executable = os.getenv("CORPORA_TEXT_CHECK_BIN")
    if not executable or not Path(executable).is_file():
        pytest.skip("set CORPORA_TEXT_CHECK_BIN to run the real core integration")
    runner = CoreBridge(executable=executable)
    text = "<root/>"
    report = runner.run({"operation": "structure", "source_id": "book.xml", "text": text})
    assert report["diagnostics"] == []
    assert report["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    detection = runner.run(
        {"text": "A short English sentence.", "locale": "en-US", "detect_only": True}
    )
    assert detection["locale"] == "en-US"
    assert detection["dictionary"] == "en_US"
    bridge.executable = Path(executable)
    with TestClient(app) as client:
        assert client.post("/v1/checks/structure", json={"text": text}).status_code == 200
        assert (
            client.post(
                "/v1/checks/tei-metadata",
                json={"text": '<TEI xmlns="http://www.tei-c.org/ns/1.0"/>'},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/language/detect",
                json={"text": "A short English sentence.", "locale": "en-US"},
            ).status_code
            == 200
        )


def test_core_bridge_rejects_missing_binary_and_dictionary() -> None:
    runner = CoreBridge(executable="/missing/corpora-text-check")
    with pytest.raises(CoreUnavailable, match="executable"):
        runner.run({"operation": "structure", "text": "<root/>"})
    with pytest.raises(CoreUnavailable, match="CORPORA_DICTIONARIES_DIR"):
        runner.run({"text": "hello"}, needs_dictionaries=True)


def test_core_bridge_rejects_stale_source_hash(tmp_path: Path) -> None:
    executable = tmp_path / "fake-core"
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' \'{"schema_version":1,"source_sha256":"wrong","diagnostics":[]}\'\n'
    )
    executable.chmod(0o755)
    runner = CoreBridge(executable=str(executable))
    with pytest.raises(CoreProtocolError, match="stale"):
        runner.run({"operation": "structure", "text": "<root/>"})
