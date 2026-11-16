from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from corpora_api import timed_corpus_hub_provision as timed
from corpora_api.corpus_hub_provision import (
    CorpusHubProvisionConflict,
    CorpusHubProvisionError,
    CorpusHubProvisionUncertain,
    VerifiedPrivateRepository,
)
from corpora_api.corpus_metadata import CorpusMetadata, CorpusMetadataNotFound


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://db.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-registry-secret-placeholder")
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    monkeypatch.setenv("HF_CORPUS_PROVISION_TOKEN", "test-hub-secret-placeholder")


def test_worker_receives_credentials_via_stdin_and_checks_receipt(config, monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    calls = []
    real_popen = subprocess.Popen
    code = (
        "import json,sys; r=json.load(sys.stdin); "
        "assert r['registry_key'].endswith('placeholder'); "
        "assert r['hub_token'].endswith('placeholder'); "
        "print(json.dumps({'corpus_id':r['corpus_id'],'metadata_revision':1,"
        "'repo_id':r['organization']+'/corpus-'+r['corpus_id'],'repo_type':'dataset',"
        "'observed_head_sha':'a'*40}))"
    )

    def worker(argv, **kwargs):
        for secret in ("test-registry-secret-placeholder", "test-hub-secret-placeholder"):
            assert secret not in repr(argv) and secret not in repr(kwargs["env"])
        calls.append(True)
        return real_popen([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(timed.subprocess, "Popen", worker)
    receipt = timed._provision_with_deadline(owner, corpus, timeout=1)
    assert str(receipt.corpus_id) == corpus and receipt.metadata_revision == 1
    assert receipt.observed_head_sha == "a" * 40
    assert len(calls) == 1


@pytest.mark.parametrize("sha", [None, "", "A" * 40, "g" * 40, "a" * 39, 123, "omitted"])
def test_worker_rejects_invalid_observed_head(config, monkeypatch, sha):
    real_popen = subprocess.Popen

    def worker(_argv, **kwargs):
        head_field = "" if sha == "omitted" else ",'observed_head_sha':" + repr(sha)
        program = (
            "import json,sys; r=json.load(sys.stdin); "
            "print(json.dumps({'corpus_id':r['corpus_id'],'metadata_revision':1,"
            "'repo_id':r['organization']+'/corpus-'+r['corpus_id'],"
            "'repo_type':'dataset'" + head_field + "}))"
        )
        return real_popen([sys.executable, "-c", program], **kwargs)

    monkeypatch.setattr(timed.subprocess, "Popen", worker)
    with pytest.raises(CorpusHubProvisionUncertain):
        timed._provision_with_deadline(str(uuid4()), str(uuid4()), timeout=1)


def test_stalled_process_is_killed_reaped_and_marked_uncertain(config, monkeypatch):
    real_popen = subprocess.Popen
    workers = []

    def stalled(_argv, **kwargs):
        worker = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(timed.subprocess, "Popen", stalled)
    with pytest.raises(CorpusHubProvisionUncertain):
        timed._provision_with_deadline(str(uuid4()), str(uuid4()), timeout=0.1)
    assert workers[0].poll() is not None


def test_interrupted_worker_is_killed_and_reaped(config, monkeypatch):
    real_popen = subprocess.Popen
    workers = []

    def stalled(_argv, **kwargs):
        worker = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        workers.append(worker)
        return worker

    def interrupt(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(timed.subprocess, "Popen", stalled)
    monkeypatch.setattr(timed.os, "set_blocking", interrupt)
    with pytest.raises(KeyboardInterrupt):
        timed._provision_with_deadline(str(uuid4()), str(uuid4()), timeout=1)
    assert workers[0].poll() is not None


def test_signal_group_failure_retains_lease_for_descendant(config, monkeypatch, tmp_path):
    real_popen = subprocess.Popen
    real_killpg = os.killpg
    workers = []
    retained, release = threading.Event(), threading.Event()
    child_file = tmp_path / "child-pid"

    def stalled(_argv, **kwargs):
        program = (
            "import pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "pathlib.Path(" + repr(str(child_file)) + ").write_text(str(p.pid)); time.sleep(30)"
        )
        worker = real_popen([sys.executable, "-c", program], **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(timed.subprocess, "Popen", stalled)
    monkeypatch.setattr(timed.os, "killpg", lambda *_: (_ for _ in ()).throw(OSError("failed")))

    def retain():
        retained.set()
        assert release.wait(5)

    monkeypatch.setattr(timed, "_retain_unconfirmed_lease", retain)

    class Lease:
        def read(self, claims, callback):
            callback(claims["sub"])
            return b""

    with pytest.raises(CorpusHubProvisionUncertain):
        timed.provision_under_session(Lease(), {"sub": str(uuid4())}, str(uuid4()), timeout=1)
    try:
        assert retained.wait(5)
        assert workers[0].wait(timeout=2) is not None
        child = int(child_file.read_text())
        os.kill(child, 0)
    finally:
        try:
            real_killpg(workers[0].pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        release.set()


def test_already_exited_worker_does_not_retain_lease(monkeypatch):
    worker = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    worker.wait(timeout=5)
    retained = []
    monkeypatch.setattr(timed, "_retain_unconfirmed_lease", lambda: retained.append(True))
    timed._stop_and_reap(worker)
    assert not retained


@pytest.mark.parametrize(
    "program",
    [
        "import sys; sys.stdin.read(); sys.stdout.buffer.write(b'\\xff')",
        "import sys; sys.stdin.read(); sys.stdout.buffer.write(b'x' * 1000000)",
    ],
)
def test_invalid_or_oversized_output_is_uncertain_and_reaped(config, monkeypatch, program):
    real_popen = subprocess.Popen
    workers = []

    def worker(_argv, **kwargs):
        process = real_popen([sys.executable, "-c", program], **kwargs)
        workers.append(process)
        return process

    monkeypatch.setattr(timed.subprocess, "Popen", worker)
    with pytest.raises(CorpusHubProvisionUncertain):
        timed._provision_with_deadline(str(uuid4()), str(uuid4()), timeout=1)
    assert workers[0].poll() is not None


@pytest.mark.parametrize(
    "url", ["http://remote.example", "http://localhost", "https://user:secret@example.test"]
)
def test_metadata_credentials_never_use_insecure_or_userinfo_url(config, monkeypatch, url):
    monkeypatch.setenv("SUPABASE_URL", url)
    with pytest.raises(CorpusHubProvisionError, match="not secure"):
        timed._provision_with_deadline(str(uuid4()), str(uuid4()), timeout=1)


@pytest.mark.parametrize(
    "code,error",
    [
        ("not_found", CorpusMetadataNotFound),
        ("conflict", CorpusHubProvisionConflict),
        ("uncertain", CorpusHubProvisionUncertain),
        ("unexpected", CorpusHubProvisionUncertain),
    ],
)
def test_worker_outcomes_are_not_automatically_retried(config, monkeypatch, code, error):
    calls = []

    real_popen = subprocess.Popen

    def worker(_argv, **kwargs):
        calls.append(True)
        program = "import sys; sys.stdin.read(); print(" + repr(json.dumps({"error": code})) + ")"
        return real_popen([sys.executable, "-c", program], **kwargs)

    monkeypatch.setattr(timed.subprocess, "Popen", worker)
    with pytest.raises(error):
        timed._provision_with_deadline(str(uuid4()), str(uuid4()), timeout=1)
    assert calls == [True]


def test_session_lease_wraps_worker_and_rechecks_before_receipt(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    receipt = VerifiedPrivateRepository(
        UUID(corpus), 1, f"fixture-org/corpus-{corpus}", "dataset", "a" * 40
    )
    events = []

    class Lease:
        def __init__(self, *, reject_before=False, reject_after=False):
            self.reject_before, self.reject_after = reject_before, reject_after

        def read(self, claims, callback):
            events.append("check-before")
            if self.reject_before:
                raise PermissionError("revoked")
            callback(claims["sub"])
            events.append("check-after")
            if self.reject_after:
                raise PermissionError("expired")
            return b""

    def worker(actual_owner, actual_corpus, *, timeout, deadline, cancelled):
        assert (actual_owner, actual_corpus) == (owner, corpus) and 0 < timeout <= 1
        events.append("hub")
        return receipt

    monkeypatch.setattr(timed, "_provision_with_deadline", worker)
    assert timed.provision_under_session(Lease(), {"sub": owner}, corpus, timeout=1) == receipt
    assert events == ["check-before", "hub", "check-after"]
    events.clear()
    with pytest.raises(PermissionError):
        timed.provision_under_session(Lease(reject_before=True), {"sub": owner}, corpus, timeout=1)
    assert events == ["check-before"]
    events.clear()
    with pytest.raises(CorpusHubProvisionUncertain):
        timed.provision_under_session(Lease(reject_after=True), {"sub": owner}, corpus, timeout=1)
    assert events == ["check-before", "hub", "check-after"]


def test_deadline_returns_while_unterminated_worker_keeps_lease(monkeypatch):
    owner, corpus = str(uuid4()), str(uuid4())
    locked, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Lease:
        def read(self, claims, callback):
            locked.set()
            try:
                callback(claims["sub"])
            finally:
                finished.set()
            return b""

    def stalled(_owner, _corpus, *, timeout, deadline, cancelled):
        assert release.wait(5)
        raise CorpusHubProvisionUncertain("uncertain")

    monkeypatch.setattr(timed, "_provision_with_deadline", stalled)
    with pytest.raises(CorpusHubProvisionUncertain):
        timed.provision_under_session(Lease(), {"sub": owner}, corpus, timeout=0.1)
    assert locked.is_set() and not finished.is_set()
    release.set()
    assert finished.wait(5)


def test_stalled_lease_uses_bounded_capacity_until_released(monkeypatch):
    release, slot_released = threading.Event(), threading.Event()

    class Slots:
        def __init__(self):
            self.semaphore = threading.BoundedSemaphore(1)

        def acquire(self, *, blocking):
            return self.semaphore.acquire(blocking=blocking)

        def release(self):
            self.semaphore.release()
            slot_released.set()

    class Lease:
        def read(self, _claims, _callback):
            assert release.wait(5)
            return b""

    monkeypatch.setattr(timed, "_PROVISION_SLOTS", Slots())
    claims, corpus = {"sub": str(uuid4())}, str(uuid4())
    with pytest.raises(CorpusHubProvisionUncertain):
        timed.provision_under_session(Lease(), claims, corpus, timeout=0.1)
    try:
        with pytest.raises(CorpusHubProvisionError, match="capacity"):
            timed.provision_under_session(Lease(), claims, corpus, timeout=0.1)
    finally:
        release.set()
    assert slot_released.wait(5)


def test_timeout_before_lease_acquisition_never_starts_worker(monkeypatch):
    release, finished, called = threading.Event(), threading.Event(), []

    class Lease:
        def read(self, claims, callback):
            try:
                assert release.wait(5)
                callback(claims["sub"])
            finally:
                finished.set()
            return b""

    monkeypatch.setattr(
        timed, "_provision_with_deadline", lambda *_args, **_kwargs: called.append(True)
    )
    with pytest.raises(CorpusHubProvisionUncertain):
        timed.provision_under_session(Lease(), {"sub": str(uuid4())}, str(uuid4()), timeout=0.5)
    release.set()
    assert finished.wait(5) and not called


def test_timeout_during_config_lookup_never_spawns_worker(config, monkeypatch):
    started, release, finished, calls = threading.Event(), threading.Event(), threading.Event(), []

    class Lease:
        def read(self, claims, callback):
            try:
                callback(claims["sub"])
            finally:
                finished.set()
            return b""

    def delayed_config():
        started.set()
        assert release.wait(5)
        return "https://db.example.test/table", {"apikey": "placeholder"}

    monkeypatch.setattr(timed.CorpusMetadataStore, "_config", delayed_config)
    monkeypatch.setattr(timed.subprocess, "Popen", lambda *_args, **_kwargs: calls.append(True))
    with pytest.raises(CorpusHubProvisionUncertain):
        timed.provision_under_session(Lease(), {"sub": str(uuid4())}, str(uuid4()), timeout=0.5)
    assert started.is_set()
    release.set()
    assert finished.wait(5) and not calls


def test_timeout_during_process_launch_never_supplies_worker_input(config, monkeypatch, tmp_path):
    real_popen = subprocess.Popen
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    marker = tmp_path / "received-input"

    class Lease:
        def read(self, claims, callback):
            try:
                callback(claims["sub"])
            finally:
                finished.set()
            return b""

    def delayed_launch(_argv, **kwargs):
        started.set()
        assert release.wait(5)
        program = (
            "import sys,pathlib; sys.stdin.read(); pathlib.Path(" + repr(str(marker)) + ").touch()"
        )
        return real_popen([sys.executable, "-c", program], **kwargs)

    monkeypatch.setattr(timed.subprocess, "Popen", delayed_launch)
    with pytest.raises(CorpusHubProvisionUncertain):
        timed.provision_under_session(Lease(), {"sub": str(uuid4())}, str(uuid4()), timeout=0.5)
    assert started.is_set()
    release.set()
    assert finished.wait(5) and not marker.exists()


def test_worker_uses_stdin_configuration_without_exporting_secrets(monkeypatch, capsys):
    owner, corpus, admin = str(uuid4()), str(uuid4()), str(uuid4())
    request = {
        "actor": owner,
        "corpus_id": corpus,
        "registry_url": "https://db.example.test/table",
        "registry_key": "registry-placeholder",
        "organization": "fixture-org",
        "hub_token": "hub-placeholder",
        "superadmin": admin,
        "deadline": time.monotonic() + 10,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("HF_CORPUS_PROVISION_TOKEN", raising=False)

    class Provisioner:
        def __init__(self, metadata, *, token):
            assert token == "hub-placeholder" and metadata._is_superadmin(admin)
            assert metadata._config()[1]["apikey"] == "registry-placeholder"
            assert not metadata._is_superadmin(owner)
            self.metadata = metadata

        def _create_private(self, actor, identifier, *, deadline):
            assert (actor, identifier) == (owner, corpus)
            assert deadline > time.monotonic()
            row = CorpusMetadata(
                id=UUID(corpus),
                owner_id=UUID(owner),
                title="Mini",
                description=None,
                language=None,
                properties={},
                hf_repo_id=f"fixture-org/corpus-{corpus}",
                hf_repo_type="dataset",
                state="provisioning",
                revision=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            self.metadata._check_link(row)
            return VerifiedPrivateRepository(
                UUID(corpus), 1, row.hf_repo_id, row.hf_repo_type, "a" * 40
            )

    monkeypatch.setattr(timed, "CorpusHubProvisioner", Provisioner)
    timed._main()
    output = capsys.readouterr().out
    assert json.loads(output)["corpus_id"] == corpus
    assert "placeholder" not in output


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), True])
def test_deadline_is_bounded(config, timeout):
    with pytest.raises(ValueError):
        timed._provision_with_deadline(str(uuid4()), str(uuid4()), timeout=timeout)
