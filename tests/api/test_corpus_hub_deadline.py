"""Deadlines include metadata, Hub transfers, sink backpressure, and cleanup."""

import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest

from corpora_api.corpus_hub_deadline import DeadlineCorpusHubReader
from corpora_api.corpus_hub_read import CorpusHubReadError

REVISION = "a" * 40


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0, close_delay=0):
        self.chunks = chunks
        self.delay = delay
        self.close_delay = close_delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        self.closed = True


def setup(monkeypatch, *, slow=None, foreign=False):
    owner, corpus = str(uuid4()), str(uuid4())
    repo = f"test-org/corpus-{corpus}"
    for key, value in {
        "SUPABASE_URL": "https://db.example.test",
        "SUPABASE_SERVICE_ROLE_KEY": "placeholder",
        "HF_CORPUS_READ_TOKEN": "read-token-placeholder",
        "HF_CORPUS_ORGANIZATION": "test-org",
    }.items():
        monkeypatch.setenv(key, value)
    row = {
        "id": corpus,
        "owner_id": owner,
        "title": "Example",
        "description": None,
        "language": None,
        "properties": {},
        "hf_repo_id": repo,
        "hf_repo_type": "dataset",
        "state": "ready",
        "revision": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    bodies, requests = [], []

    async def handler(request):
        requests.append(request)
        assert "cookie" not in request.headers
        if request.url.host == "db.example.test":
            payload = json.dumps([] if foreign else [row]).encode()
            kind = "registry"
        elif request.url.path.startswith("/api/"):
            payload = json.dumps({"id": repo, "sha": REVISION, "private": True}).encode()
            kind = "hub"
        elif request.url.host == "huggingface.co":
            return httpx.Response(
                302,
                headers={
                    "location": "https://cdn.hf.co/file?signed=secret",
                    "set-cookie": "session=untrusted",
                },
            )
        else:
            assert request.url.host == "cdn.hf.co"
            assert "authorization" not in request.headers
            payload = b"abcdef"
            kind = "file"
        chunks = [payload] if slow != kind else [payload[i : i + 1] for i in range(len(payload))]
        body = Body(
            chunks,
            delay=0.02 if slow == kind else 0,
            close_delay=1 if slow == "close" and kind == "file" else 0,
        )
        bodies.append(body)
        return httpx.Response(200, stream=body, headers={"set-cookie": "session=untrusted"})

    return owner, corpus, handler, bodies, requests


def test_transfer_authorizes_and_strips_cdn_token(monkeypatch):
    owner, corpus, handler, bodies, requests = setup(monkeypatch)

    async def run():
        output = bytearray()

        async def sink(chunk):
            output.extend(chunk)

        await DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
            owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=1, sink=sink
        )
        assert output == b"abcdef"

    asyncio.run(run())
    assert len(requests) == 4
    assert all(body.closed for body in bodies)


@pytest.mark.parametrize("phase", ["registry", "hub", "file", "sink"])
def test_deadline_cancels_slow_phase_and_closes_response(monkeypatch, phase):
    owner, corpus, handler, bodies, _ = setup(monkeypatch, slow=phase)

    async def run():
        async def sink(_):
            if phase == "sink":
                await asyncio.sleep(1)

        started = time.monotonic()
        with pytest.raises(CorpusHubReadError, match="timed out"):
            await DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
                owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=0.08, sink=sink
            )
        assert time.monotonic() - started < 0.5

    asyncio.run(run())
    assert all(body.closed for body in bodies)


def test_foreign_metadata_never_contacts_hub(monkeypatch):
    owner, corpus, handler, bodies, requests = setup(monkeypatch, foreign=True)

    async def run():
        async def sink(_):
            pytest.fail("foreign data was delivered")

        with pytest.raises(CorpusHubReadError):
            await DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
                owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=1, sink=sink
            )

    asyncio.run(run())
    assert len(requests) == 1
    assert all(body.closed for body in bodies)


def test_file_limit_closes_response_without_delivering_excess(monkeypatch):
    owner, corpus, handler, bodies, _ = setup(monkeypatch)

    async def run():
        output = bytearray()

        async def sink(chunk):
            output.extend(chunk)

        with pytest.raises(CorpusHubReadError, match="size limit"):
            await DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
                owner, corpus, REVISION, "tf/x.tf", max_bytes=3, timeout=1, sink=sink
            )
        assert len(output) <= 3

    asyncio.run(run())
    assert all(body.closed for body in bodies)


@pytest.mark.parametrize("error", [ValueError("sink error"), TimeoutError("sink timeout")])
def test_sink_exceptions_are_not_rewritten(monkeypatch, error):
    owner, corpus, handler, bodies, _ = setup(monkeypatch)

    async def run():
        async def sink(_):
            raise error

        with pytest.raises(type(error), match=str(error)):
            await DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
                owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=1, sink=sink
            )

    asyncio.run(run())
    assert all(body.closed for body in bodies)


def test_stalled_close_is_bounded(monkeypatch):
    owner, corpus, handler, _, _ = setup(monkeypatch, slow="close")

    async def run():
        async def sink(_):
            pass

        started = time.monotonic()
        with pytest.raises(CorpusHubReadError, match="timed out"):
            await DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
                owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=0.08, sink=sink
            )
        assert time.monotonic() - started < 0.5

    asyncio.run(run())


def test_stalled_cleanup_does_not_mask_sink_failure(monkeypatch):
    owner, corpus, handler, _, _ = setup(monkeypatch, slow="close")

    async def run():
        async def sink(_):
            raise ValueError("sink failure")

        with pytest.raises(ValueError, match="sink failure"):
            await DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
                owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=1, sink=sink
            )

    asyncio.run(run())


def test_caller_handled_exception_does_not_hide_cleanup_failure(monkeypatch):
    owner, corpus, handler, _, _ = setup(monkeypatch)

    class SlowClose(httpx.MockTransport):
        async def aclose(self):
            await asyncio.sleep(1)

    async def run():
        async def sink(_):
            pass

        try:
            raise ValueError("already handled")
        except ValueError:
            with pytest.raises(CorpusHubReadError, match="cleanup failed"):
                await DeadlineCorpusHubReader(lambda: SlowClose(handler)).transfer(
                    owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=1, sink=sink
                )

    asyncio.run(run())


def test_slow_transport_setup_cannot_start_expired_transfer(monkeypatch):
    owner, corpus, handler, _, requests = setup(monkeypatch)

    async def run():
        def factory():
            time.sleep(0.05)
            return httpx.MockTransport(handler)

        async def sink(_):
            pytest.fail("expired transfer delivered bytes")

        with pytest.raises(CorpusHubReadError, match="timed out"):
            await DeadlineCorpusHubReader(factory).transfer(
                owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=0.01, sink=sink
            )

    asyncio.run(run())
    assert not requests


def test_external_cancellation_is_preserved_and_closes_response(monkeypatch):
    owner, corpus, handler, bodies, _ = setup(monkeypatch, slow="file")

    async def run():
        async def sink(_):
            pass

        task = asyncio.create_task(
            DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
                owner, corpus, REVISION, "tf/x.tf", max_bytes=20, timeout=1, sink=sink
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert all(body.closed for body in bodies)


def test_deadline_closes_real_trickling_http_stream(monkeypatch):
    owner, corpus, mock_handler, _, _ = setup(monkeypatch)

    async def run():
        disconnected = asyncio.Event()

        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
                await writer.drain()
                for _ in range(1000):
                    writer.write(b"x")
                    await writer.drain()
                    await asyncio.sleep(0.02)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                disconnected.set()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = httpx.AsyncHTTPTransport()

        async def handler(request):
            if request.url.host == "cdn.hf.co":
                return await transport.handle_async_request(
                    httpx.Request("GET", f"http://127.0.0.1:{port}/")
                )
            return await mock_handler(request)

        async def sink(_):
            pass

        try:
            started = time.monotonic()
            with pytest.raises(CorpusHubReadError, match="timed out"):
                await DeadlineCorpusHubReader(lambda: httpx.MockTransport(handler)).transfer(
                    owner, corpus, REVISION, "tf/x.tf", max_bytes=2000, timeout=0.08, sink=sink
                )
            assert time.monotonic() - started < 0.5
            await asyncio.wait_for(disconnected.wait(), timeout=1)
        finally:
            await transport.aclose()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
