"""TF upload preflight and MCP contract checks."""

import asyncio
import base64
import io
import zipfile

from fastapi.testclient import TestClient
from fastmcp import Client

from corpora_api.app import app, mcp


def bundle(*, missing_containment: bool = False, implicit_otype: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        otype = "word\nword\nsentence\n" if implicit_otype else "1-2\tword\n3\tsentence\n"
        archive.writestr("tf/otype.tf", "@node\n@valueType=str\n\n" + otype)
        archive.writestr(
            "tf/oslots.tf",
            "@edge\n@valueType=int\n\n"
            if missing_containment
            else "@edge\n@valueType=int\n\n3\t1-2\n",
        )
        archive.writestr("tf/otext.tf", "@config\n@fmt:text-orig-full={word}\n")
    return output.getvalue()


def test_rest_tf_archive_check_and_auth(monkeypatch) -> None:
    monkeypatch.setenv("CORPORA_API_TOKEN", "test-token")
    with TestClient(app) as client:
        denied = client.post("/v1/checks/text-fabric", files={"file": ("tf.zip", bundle())})
        response = client.post(
            "/v1/checks/text-fabric",
            files={"file": ("tf.zip", bundle())},
            headers={"Authorization": "Bearer test-token"},
        )
        schema = client.get("/v1/openapi.json").json()
    assert denied.status_code == 401
    assert response.status_code == 200
    assert response.json()["status"] == "passed"
    assert response.json()["max_slot"] == 2
    assert response.json()["max_node"] == 3
    assert response.json()["source_sha256"]
    assert "TFCheckReport" in schema["components"]["schemas"]


def test_missing_containment_is_reported() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/checks/text-fabric", files={"file": ("tf.zip", bundle(missing_containment=True))}
        )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["issues"][0]["code"] == "OSLOTS_MISSING"


def test_implicit_otype_numbering_is_supported() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/checks/text-fabric", files={"file": ("tf.zip", bundle(implicit_otype=True))}
        )
    assert response.status_code == 200
    assert response.json()["status"] == "passed"


def test_unsafe_archive_is_rejected() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../otype.tf", "@node\n\n1\tword\n")
    with TestClient(app) as client:
        response = client.post(
            "/v1/checks/text-fabric", files={"file": ("tf.zip", output.getvalue())}
        )
    assert response.status_code == 422


def test_mcp_exposes_tf_checker_and_prompt() -> None:
    async def check() -> tuple[set[str], set[str], dict]:
        async with Client(mcp) as client:
            tools = {tool.name for tool in await client.list_tools()}
            prompts = {prompt.name for prompt in await client.list_prompts()}
            result = await client.call_tool(
                "check_text_fabric_archive", {"archive_base64": base64.b64encode(bundle()).decode()}
            )
            return tools, prompts, result.data

    tools, prompts, report = asyncio.run(check())
    assert "check_text_fabric_archive" in tools
    assert "text_fabric_checker" in prompts
    assert report["status"] == "passed"
