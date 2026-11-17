import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from corpora_api import corpus_import_worker as worker
from corpora_api.corpus_save_claim import ClaimedCorpusSave


@pytest.fixture
def claim(monkeypatch):
    for key, value in {
        "SUPABASE_URL": "https://db.example.test",
        "SUPABASE_SERVICE_ROLE_KEY": "test-registry-placeholder",
        "HF_CORPUS_ORGANIZATION": "fixture",
        "HF_CORPUS_PROVISION_TOKEN": "test-hub-placeholder",
    }.items():
        monkeypatch.setenv(key, value)
    corpus, operation = uuid4(), uuid4()
    return ClaimedCorpusSave(
        operation,
        uuid4(),
        corpus,
        uuid4(),
        uuid4(),
        0,
        None,
        "a" * 40,
        "b" * 64,
        "corpus",
        f"fixture/corpus-{corpus}",
        "dataset",
        1,
        "corpora-op-" + str(operation),
    )


def launch(monkeypatch, program):
    popen = subprocess.Popen
    processes = []

    def start(argv, **kwargs):
        assert "placeholder" not in repr(argv)
        assert "placeholder" not in repr(kwargs["env"])
        process = popen([sys.executable, "-c", program], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", start)
    return processes


def test_worker_transfers_secrets_only_via_stdin_and_releases_capacity(claim, monkeypatch):
    processes = launch(
        monkeypatch,
        (
            "import json,sys; r=json.load(sys.stdin); "
            "assert r['config']['HF_CORPUS_PROVISION_TOKEN']=='test-hub-placeholder'; "
            "assert r['claim']['expected_sequence']==0; "
            "print(json.dumps({'completed':True}))"
        ),
    )
    with worker.ImportSlot() as slot:
        worker.run_import_worker(claim, "tiny.corpus", b"archive", slot=slot)
    assert len(processes) == 1 and processes[0].poll() == 0 and slot.released


@pytest.mark.parametrize(
    "program",
    [
        "import time; time.sleep(30)",
        "import sys; sys.stdin.read(); sys.stdout.write('x'*100000)",
        "import sys; sys.stdin.read(); sys.stdout.buffer.write(b'\\xff')",
        "import sys; sys.stdin.read(); sys.exit(1)",
    ],
)
def test_failed_worker_is_never_restarted_and_is_reaped(monkeypatch, program):
    processes = launch(monkeypatch, program)
    with pytest.raises((ValueError, TimeoutError)):
        worker._exchange(b"{}", time.monotonic() + 0.2)
    assert len(processes) == 1 and processes[0].poll() is not None


def test_capacity_is_retained_until_timed_out_worker_finishes(claim, monkeypatch):
    release, finished = threading.Event(), threading.Event()
    monkeypatch.setattr(worker, "_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(worker, "MAX_IMPORT_SECONDS", 0.03)

    def blocked(*_):
        assert release.wait(5)
        return {"completed": True}

    monkeypatch.setattr(worker, "_exchange", blocked)
    slot = worker.ImportSlot()
    original_release = slot.release

    def released():
        original_release()
        finished.set()

    monkeypatch.setattr(slot, "release", released)
    try:
        with slot, pytest.raises(TimeoutError):
            worker.run_import_worker(claim, "tiny.corpus", b"archive", slot=slot)
        assert not slot.released
        with pytest.raises(worker.HTTPException):
            with worker.ImportSlot():
                pytest.fail("Capacity released before worker stopped")
    finally:
        release.set()
        assert finished.wait(5)
    with worker.ImportSlot():
        pass


@pytest.fixture
def pipeline(monkeypatch):
    calls = []
    stage = SimpleNamespace()

    def staged(*_):
        calls.append("upload-stage")
        return SimpleNamespace(stage_sha="c" * 40)

    def publish(*_, **__):
        calls.append("publish")
        return SimpleNamespace(commit_sha="d" * 40)

    monkeypatch.setattr(worker, "CorpusHubStager", lambda *_: SimpleNamespace(stage=staged))
    monkeypatch.setattr(worker, "CorpusPublicationClaim", lambda *_: stage)
    monkeypatch.setattr(worker, "prepare_corpus_archive", lambda *_a, **_k: object())
    monkeypatch.setattr(worker, "CorpusHubPublisher", lambda *_: SimpleNamespace(publish=publish))
    return calls


@pytest.mark.parametrize("outcome", [False, "lost-ack"])
def test_unconfirmed_durable_stage_never_starts_publication(claim, pipeline, monkeypatch, outcome):
    def rpc(_self, name, params):
        pipeline.append(name)
        if outcome == "lost-ack":
            raise TimeoutError("Lost acknowledgement")
        return False

    monkeypatch.setattr(worker.CreationRPC, "call", rpc)
    with pytest.raises((ValueError, TimeoutError)):
        worker.execute_import(claim, "tiny.corpus", b"archive", time.monotonic() + 1)
    assert pipeline == ["upload-stage", "corpora_stage_import"]


@pytest.mark.parametrize("committed", [True, False])
def test_lost_completion_ack_only_observes_exact_result(claim, pipeline, monkeypatch, committed):
    completion = []

    def rpc(_self, name, params):
        pipeline.append(name)
        if name == "corpora_stage_import":
            return True
        if name == "corpora_complete_import":
            completion.append(params)
            raise TimeoutError("Lost acknowledgement")
        assert name == "corpora_find_import_result" and params == completion[0]
        assert params["p_sha"] == "d" * 40
        return committed

    monkeypatch.setattr(worker.CreationRPC, "call", rpc)
    if committed:
        worker.execute_import(claim, "tiny.corpus", b"archive", time.monotonic() + 1)
    else:
        with pytest.raises(TimeoutError):
            worker.execute_import(claim, "tiny.corpus", b"archive", time.monotonic() + 1)
    assert pipeline == [
        "upload-stage",
        "corpora_stage_import",
        "publish",
        "corpora_complete_import",
        "corpora_find_import_result",
    ]


def test_noninitial_claim_never_uploads(claim, pipeline):
    with pytest.raises(ValueError, match="Only initial"):
        worker.execute_import(
            replace(claim, expected_sequence=1), "tiny.corpus", b"archive", time.monotonic() + 1
        )
    assert not pipeline


def test_hub_control_response_per_request_and_aggregate_bounds():
    owner = SimpleNamespace(received=0, deadline=time.monotonic() + 10)
    for _ in range(4):
        assert (
            sum(map(len, worker._BoundedHubStream(httpx.ByteStream(b"x" * 2_000_000), owner)))
            == 2_000_000
        )
    with pytest.raises(ValueError, match="bounds"):
        list(worker._BoundedHubStream(httpx.ByteStream(b"x"), owner))
    owner.received = 0
    with pytest.raises(ValueError, match="bounds"):
        list(worker._BoundedHubStream(httpx.ByteStream(b"x" * 2_000_001), owner))
    owner.received, owner.deadline = 0, time.monotonic() - 1
    with pytest.raises(ValueError, match="bounds"):
        list(worker._BoundedHubStream(httpx.ByteStream(b"x"), owner))
