import hashlib
import struct
import time

import httpx
import pytest

from corpora_api import corpus_git_acquire as acquire

SHA = "a" * 40
REF = "refs/heads/corpora-op-fixture"


def pkt(value):
    return f"{len(value) + 4:04x}".encode() + value


def advertisement(*, sha=SHA, capabilities=b"shallow side-band-64k no-progress"):
    return (
        pkt(b"# service=git-upload-pack\n")
        + b"0000"
        + pkt((sha + " " + REF).encode() + b"\0" + capabilities + b"\n")
        + b"0000"
    )


def pack(count=3):
    # The network layer checks framing/checksum, not compressed object validity.
    # Offline Git indexing and the corpus verifier must validate real objects.
    value = struct.pack(">4sII", b"PACK", 2, count) + b"test-pack-body"
    return value + hashlib.sha1(value, usedforsecurity=False).digest()


def response(value=None):
    return (
        pkt(b"shallow " + SHA.encode() + b"\n")
        + b"0000"
        + pkt(b"NAK\n")
        + pkt(b"\1" + (pack() if value is None else value))
        + b"0000"
    )


class Fragments(httpx.SyncByteStream):
    def __init__(self, value):
        self.value = value

    def __iter__(self):
        for byte in self.value:
            yield bytes([byte])


def transfer(tmp_path, *, refs=None, result=None, mutate=None, deadline=None):
    requests = []

    def handle(request):
        requests.append(request)
        get = request.method == "GET"
        body = (
            (advertisement() if refs is None else refs)
            if get
            else (response() if result is None else result)
        )
        headers = {
            "Content-Type": "application/x-git-upload-pack-"
            + ("advertisement" if get else "result")
        }
        status = 200
        if mutate:
            status, headers = mutate(get, status, headers)
        return httpx.Response(status, headers=headers, stream=Fragments(body))

    path = tmp_path / "candidate.pack"
    receipt = acquire.download_commit_pack(
        "fixture/corpus",
        "dataset",
        REF,
        SHA,
        "fixture-placeholder",
        path,
        deadline=time.monotonic() + 60 if deadline is None else deadline,
        transport=httpx.MockTransport(handle),
    )
    return receipt, requests, path


def test_fragmented_smart_http_pack_is_exact_and_request_is_depth_one(tmp_path):
    receipt, requests, path = transfer(tmp_path)
    assert path.read_bytes() == pack()
    assert receipt.size == len(pack()) and receipt.object_count == 3
    assert len(requests) == 2
    assert requests[0].url.host == requests[1].url.host == "huggingface.co"
    assert requests[1].content == (
        pkt(b"want " + SHA.encode() + b" side-band-64k no-progress\n")
        + pkt(b"deepen 1\n")
        + b"0000"
        + pkt(b"done\n")
    )
    assert all(r.headers["accept-encoding"] == "identity" for r in requests)


def test_packet_parser_handles_boundaries_inside_headers_and_pack_checksum(tmp_path):
    deadline = time.monotonic() + 60
    refs = acquire._Packets(iter(Fragments(advertisement())), 262144, deadline)
    assert b"shallow" in acquire._advertisement(refs, REF, SHA)
    packets = acquire._Packets(iter(Fragments(response())), 262144, deadline)
    path = tmp_path / "fragmented.pack"
    assert acquire._pack(packets, SHA, path).size == len(pack())
    assert path.read_bytes() == pack()


@pytest.mark.parametrize(
    "refs",
    [
        advertisement(sha="b" * 40),
        advertisement(capabilities=b"shallow"),
        advertisement(capabilities=b"shallow side-band-64k object-format=sha256"),
        advertisement()[:-1],
        advertisement() + b"trailing",
        b"0002",
        b"zzzz",
        pkt(b"version 2\n") + b"0000",
    ],
)
def test_invalid_or_moved_advertisement_never_creates_pack(tmp_path, refs):
    with pytest.raises(acquire.CorpusGitAcquisitionError):
        transfer(tmp_path, refs=refs)
    assert not (tmp_path / "candidate.pack").exists()


@pytest.mark.parametrize(
    "value",
    [
        response()[:-1],
        response() + b"trailing",
        response(pack()[:-1] + b"X"),
        response(pack(count=0)),
        response(pack(count=acquire.MAX_PACK_OBJECTS + 1)),
        response().replace(b"shallow " + SHA.encode(), b"shallow " + b"b" * 40),
        b"0000" + pkt(b"ACK " + SHA.encode() + b"\n") + b"0000",
        b"0000" + pkt(b"NAK\n") + pkt(b"\3server error") + b"0000",
        b"0000" + pkt(b"NAK\n") + b"0000",
    ],
)
def test_invalid_transfer_removes_partial_output(tmp_path, value):
    with pytest.raises(acquire.CorpusGitAcquisitionError):
        transfer(tmp_path, result=value)
    assert not (tmp_path / "candidate.pack").exists()


@pytest.mark.parametrize(
    "bound", ["MAX_PACK_BYTES", "MAX_ADVERTISEMENT_BYTES", "MAX_RESPONSE_BYTES"]
)
def test_transfer_stops_at_each_byte_bound(tmp_path, monkeypatch, bound):
    monkeypatch.setattr(acquire, bound, 16)
    with pytest.raises(acquire.CorpusGitAcquisitionError):
        transfer(tmp_path)
    assert not (tmp_path / "candidate.pack").exists()


def test_progress_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "MAX_PROGRESS_BYTES", 8)
    result = b"0000" + pkt(b"NAK\n") + pkt(b"\2" + b"x" * 9) + b"0000"
    with pytest.raises(acquire.CorpusGitAcquisitionError):
        transfer(tmp_path, result=result)
    assert not (tmp_path / "candidate.pack").exists()


@pytest.mark.parametrize(
    "status,extra",
    [
        (302, {"location": "https://foreign.test"}),
        (401, {}),
        (200, {"content-encoding": "gzip"}),
        (200, {"content-type": "text/plain"}),
    ],
)
def test_transport_does_not_follow_redirects_or_decode_compressed_bodies(tmp_path, status, extra):
    with pytest.raises(acquire.CorpusGitAcquisitionError):
        transfer(tmp_path, mutate=lambda get, code, headers: (status, {**headers, **extra}))


def test_expired_deadline_does_not_start_transfer(tmp_path):
    with pytest.raises(acquire.CorpusGitAcquisitionError):
        transfer(tmp_path, deadline=time.monotonic() - 1)


def test_existing_output_is_not_overwritten_or_removed(tmp_path):
    path = tmp_path / "candidate.pack"
    path.write_bytes(b"existing")
    with pytest.raises(acquire.CorpusGitAcquisitionError):
        transfer(tmp_path)
    assert path.read_bytes() == b"existing"
