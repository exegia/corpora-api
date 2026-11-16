"""Tests for the unwired validation and original-byte binding."""

import hashlib
import io
import os
import zipfile
from pathlib import Path

import pytest

from corpora_api.corpus_archive_inventory import CorpusInventoryError
from corpora_api.corpus_archive_prepare import CorpusPreparationError, prepare_corpus_archive
from corpora_api.validation import ValidationBridge, ValidationTimedOut

CHECKS = {
    "text_fabric_load": True,
    "context_fabric_load": True,
    "cfm_mmap_reload": True,
    "stats_match": True,
    "samples_match": True,
}


def zipped():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("tf/otype.tf", b"sample")
        archive.writestr("tf/.cfr/cache.cfr", b"original cache")
    return buffer.getvalue()


class Validator:
    def __init__(self, report):
        self.report = report
        self.seen = []

    def validate(self, filename, archive):
        self.seen.append((filename, archive))
        if isinstance(self.report, Exception):
            raise self.report
        return self.report


def test_preparation_binds_inventory_and_validator_to_same_immutable_bytes():
    data = zipped()
    validator = Validator({"path": "sample.zip", "valid": True, "checks": CHECKS})
    prepared = prepare_corpus_archive(
        "sample.zip", data, hashlib.sha256(data).hexdigest(), validator
    )
    assert prepared.archive is data and validator.seen == [("sample.zip", data)]
    assert validator.seen[0][1] is data
    assert prepared.inventory.archive_sha256 == hashlib.sha256(data).hexdigest()
    assert [file.path for file in prepared.inventory.files] == ["tf/.cfr/cache.cfr", "tf/otype.tf"]


@pytest.mark.parametrize("valid", [False, None, 1, "true"])
def test_invalid_or_nonboolean_verdict_never_prepares(valid):
    data = zipped()
    validator = Validator({"path": "sample.zip", "valid": valid, "checks": CHECKS})
    with pytest.raises(CorpusPreparationError):
        prepare_corpus_archive("sample.zip", data, hashlib.sha256(data).hexdigest(), validator)


@pytest.mark.parametrize(
    "report", [None, [], {"path": "wrong.zip", "valid": True, "checks": CHECKS}]
)
def test_malformed_or_mismatched_report_is_rejected(report):
    data = zipped()
    with pytest.raises(CorpusPreparationError):
        prepare_corpus_archive(
            "sample.zip", data, hashlib.sha256(data).hexdigest(), Validator(report)
        )


@pytest.mark.parametrize(
    "change", [{}, {"text_fabric_load": False}, {"stats_match": 1}, {"future_check": False}]
)
def test_incomplete_real_loader_checks_never_prepare(change):
    data = zipped()
    checks = {**CHECKS, **change} if change else {}
    with pytest.raises(CorpusPreparationError):
        prepare_corpus_archive(
            "sample.zip",
            data,
            hashlib.sha256(data).hexdigest(),
            Validator({"path": "sample.zip", "valid": True, "checks": checks}),
        )


def test_corpus_extension_requires_shipped_cache_checks():
    data = zipped()
    validator = Validator({"path": "sample.corpus", "valid": True, "checks": CHECKS})
    with pytest.raises(CorpusPreparationError):
        prepare_corpus_archive("sample.corpus", data, hashlib.sha256(data).hexdigest(), validator)
    validator.report["checks"] = {**CHECKS, "shipped_cfm_load": True, "shipped_cfm_match": True}
    assert prepare_corpus_archive(
        "sample.corpus", data, hashlib.sha256(data).hexdigest(), validator
    )


def test_bad_identity_format_digest_and_invalid_archive_fail_before_validator():
    data = zipped()
    validator = Validator({"path": "sample.zip", "valid": True, "checks": CHECKS})
    for name, raw, digest in [
        ("../sample.zip", data, hashlib.sha256(data).hexdigest()),
        ("sample.cusx", data, hashlib.sha256(data).hexdigest()),
        ("bad\ud800.zip", data, hashlib.sha256(data).hexdigest()),
        ("sample.zip", data, "a" * 64),
        ("sample.zip", bytearray(data), hashlib.sha256(data).hexdigest()),
    ]:
        with pytest.raises(CorpusPreparationError):
            prepare_corpus_archive(name, raw, digest, validator)
    assert validator.seen == []
    invalid_zip = b"not a zip"
    with pytest.raises(CorpusInventoryError):
        prepare_corpus_archive(
            "sample.zip", invalid_zip, hashlib.sha256(invalid_zip).hexdigest(), validator
        )
    assert validator.seen == []


def test_validator_failures_are_not_treated_as_success():
    data = zipped()
    validator = Validator(ValidationTimedOut("timed out"))
    with pytest.raises(ValidationTimedOut):
        prepare_corpus_archive("sample.zip", data, hashlib.sha256(data).hexdigest(), validator)


def test_real_loader_fixture_when_configured():
    executable = os.getenv("CORPORA_VALIDATE_BIN")
    fixture = os.getenv("CORPORA_VALIDATE_TEST_ARCHIVE")
    if not executable or not fixture:
        pytest.skip("requires real core executable and archive")
    path = Path(fixture)
    data = path.read_bytes()
    prepared = prepare_corpus_archive(
        path.name, data, hashlib.sha256(data).hexdigest(), ValidationBridge(executable)
    )
    assert prepared.archive == data and len(prepared.inventory.files) > 0
