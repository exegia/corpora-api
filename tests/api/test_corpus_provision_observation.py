import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from corpora_api.corpus_metadata import CorpusMetadataConflict
from corpora_api.corpus_provision_observation import (
    CorpusProvisionHubObserver,
    CorpusProvisionReconciler,
    ProvisionObservationTarget,
)


@pytest.fixture
def target():
    corpus = uuid4()
    return ProvisionObservationTarget(
        uuid4(), uuid4(), corpus, f"fixture-org/corpus-{corpus}", "dataset", "uncertain"
    )


def test_observes_only_exact_private_repository_without_mutation(target):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": target.repo_id, "private": True, "sha": "a" * 40})

    observer = CorpusProvisionHubObserver(
        "fixture-token", transport_factory=lambda: httpx.MockTransport(respond)
    )
    result = observer.observe_trusted(target)
    assert result.status == "private_repository_observed"
    assert result.observed_head_sha == "a" * 40
    assert len(requests) == 1 and requests[0].method == "GET"
    assert str(requests[0].url) == f"https://huggingface.co/api/datasets/{target.repo_id}"


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
def test_http_failures_are_inconclusive_and_never_follow_redirects(target, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://elsewhere.example/"})

    observer = CorpusProvisionHubObserver(
        "fixture-token", transport_factory=lambda: httpx.MockTransport(respond)
    )
    result = observer.observe_trusted(target)
    assert result.status == "inconclusive"
    assert result.observed_head_sha is None
    assert len(requests) == 1


@pytest.mark.parametrize(
    "info",
    [
        {"id": "wrong/name", "private": True, "sha": "a" * 40},
        {"id": "correct", "private": False, "sha": "a" * 40},
        {"id": "correct", "private": "true", "sha": "a" * 40},
    ],
)
def test_wrong_identity_or_privacy_is_a_mismatch(target, info):
    if info["id"] == "correct":
        info["id"] = target.repo_id
    observer = CorpusProvisionHubObserver(
        "fixture-token",
        transport_factory=lambda: httpx.MockTransport(lambda _: httpx.Response(200, json=info)),
    )
    result = observer.observe_trusted(target)
    assert result.status == "identity_or_privacy_mismatch"
    assert result.observed_head_sha is None


def test_missing_head_and_network_error_are_inconclusive(target):
    observer = CorpusProvisionHubObserver(
        "fixture-token",
        transport_factory=lambda: httpx.MockTransport(
            lambda _: httpx.Response(200, json={"id": target.repo_id, "private": True})
        ),
    )
    assert observer.observe_trusted(target).status == "inconclusive"

    def fail(request):
        raise httpx.ReadTimeout("failed", request=request)

    observer = CorpusProvisionHubObserver(
        "fixture-token", transport_factory=lambda: httpx.MockTransport(fail)
    )
    assert observer.observe_trusted(target).status == "inconclusive"


def test_reconciler_rejects_snapshot_changed_during_hub_read(target):
    snapshots = iter([target, replace(target, state="executing")])
    lookup = SimpleNamespace(load_trusted=lambda _: next(snapshots))
    observer = CorpusProvisionHubObserver(
        "fixture-token",
        transport_factory=lambda: httpx.MockTransport(lambda _: httpx.Response(404)),
    )
    reconciler = CorpusProvisionReconciler(lookup, observer)
    with pytest.raises(CorpusMetadataConflict):
        reconciler.inspect_trusted(target.operation_id)


def test_oversized_response_is_inconclusive(target):
    observer = CorpusProvisionHubObserver(
        "fixture-token",
        transport_factory=lambda: httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * 65537)
        ),
    )
    assert observer.observe_trusted(target).status == "inconclusive"


class SlowBody(httpx.AsyncByteStream):
    def __init__(self, payload, *, delay=0, close_delay=0):
        self.payload = payload
        self.delay = delay
        self.close_delay = close_delay
        self.closed = False
        self.cancelled = False

    async def __aiter__(self):
        try:
            for byte in self.payload:
                await asyncio.sleep(self.delay)
                yield bytes([byte])
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        await asyncio.sleep(self.close_delay)
        self.closed = True


@pytest.mark.parametrize("phase", ["headers", "body", "response_cleanup", "client_cleanup"])
def test_overall_deadline_cancels_slow_io_and_bounds_cleanup(target, phase):
    body = SlowBody(
        json.dumps({"id": target.repo_id, "private": True, "sha": "a" * 40}).encode(),
        delay=0.01 if phase == "body" else 0,
        close_delay=10 if phase == "response_cleanup" else 0,
    )
    events = []

    async def respond(request):
        events.append("request")
        assert request.headers["accept-encoding"] == "identity"
        if phase == "headers":
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                events.append("headers_cancelled")
                raise
        return httpx.Response(200, stream=body)

    class Transport(httpx.MockTransport):
        async def aclose(self):
            events.append("client_cleanup")
            if phase == "client_cleanup":
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    events.append("client_cleanup_cancelled")
                    raise
            await super().aclose()

    observer = CorpusProvisionHubObserver(
        "fixture-token", timeout=0.08, transport_factory=lambda: Transport(respond)
    )
    started = time.monotonic()
    result = observer.observe_trusted(target)
    assert result.status == "inconclusive"
    assert result.observed_head_sha is None
    assert time.monotonic() - started < 0.7
    assert events.count("request") == 1
    assert events.count("client_cleanup") == 1
    if phase == "headers":
        assert "headers_cancelled" in events
    elif phase == "client_cleanup":
        assert body.closed and "client_cleanup_cancelled" in events
    elif phase == "body":
        assert body.cancelled and body.closed


def test_encoded_response_is_rejected_before_decoding(target):
    body = SlowBody(b"untrusted compressed content")
    observer = CorpusProvisionHubObserver(
        "fixture-token",
        transport_factory=lambda: httpx.MockTransport(
            lambda _: httpx.Response(200, stream=body, headers={"content-encoding": "gzip"})
        ),
    )
    assert observer.observe_trusted(target).status == "inconclusive"
    assert body.closed


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 16, "1"])
def test_invalid_deadline_is_rejected(timeout):
    with pytest.raises(ValueError, match="timeout"):
        CorpusProvisionHubObserver("fixture-token", timeout=timeout)


def test_observer_reuse_creates_and_closes_a_client_per_call(target):
    transports = []

    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True
            await super().aclose()

    def transport():
        result = Transport(
            lambda _: httpx.Response(
                200, json={"id": target.repo_id, "private": True, "sha": "a" * 40}
            )
        )
        transports.append(result)
        return result

    observer = CorpusProvisionHubObserver("fixture-token", transport_factory=transport)
    for _ in range(2):
        assert observer.observe_trusted(target).status == "private_repository_observed"
    assert len(transports) == 2
    assert all(transport.closed for transport in transports)


def test_expired_setup_does_not_start_request(target):
    requests = []

    def transport():
        time.sleep(0.02)
        return httpx.MockTransport(lambda request: requests.append(request))

    observer = CorpusProvisionHubObserver(
        "fixture-token", timeout=0.01, transport_factory=transport
    )
    assert observer.observe_trusted(target).status == "inconclusive"
    assert not requests


@pytest.mark.parametrize("phase", ["response", "client"])
@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_cleanup_errors_are_inconclusive_and_still_close_client(target, phase, error_type):
    events = []

    class Body(SlowBody):
        async def aclose(self):
            events.append("response")
            if phase == "response":
                raise error_type("test cleanup failure")
            await super().aclose()

    class Transport(httpx.MockTransport):
        async def aclose(self):
            events.append("client")
            if phase == "client":
                raise error_type("test cleanup failure")
            await super().aclose()

    body = Body(json.dumps({"id": target.repo_id, "private": True, "sha": "a" * 40}).encode())
    observer = CorpusProvisionHubObserver(
        "fixture-token",
        transport_factory=lambda: Transport(lambda _: httpx.Response(200, stream=body)),
    )
    assert observer.observe_trusted(target).status == "inconclusive"
    assert events == ["response", "client"]


def test_deadline_closes_real_trickling_http_connection(target):
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

        class LocalTransport(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request):
                # The production request remains fixed to huggingface.co;
                # only this test transport redirects to the local fixture.
                assert request.url.host == "huggingface.co"
                return await super().handle_async_request(
                    httpx.Request("GET", f"http://127.0.0.1:{port}/")
                )

        observer = CorpusProvisionHubObserver(
            "fixture-token", timeout=0.15, transport_factory=LocalTransport
        )
        try:
            started = time.monotonic()
            result = await asyncio.to_thread(observer.observe_trusted, target)
            assert result.status == "inconclusive"
            assert time.monotonic() - started < 0.7
            await asyncio.wait_for(disconnected.wait(), timeout=1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
