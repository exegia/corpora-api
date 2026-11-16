"""Unwired atomic recording of a trusted Hub staging readback.

A StagedCorpusArchive is an internal assertion, not independently verified by
this module. Never construct one from client data. Recording it proves neither
future retention/privacy nor that a remote worker has stopped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from corpora_api.corpus_hub_stage import StagedCorpusArchive
from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataStore,
)


class CorpusStageReceiptRecorder:
    """Record one exact internal stage result without making Hub requests."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def record_trusted(self, stage: StagedCorpusArchive) -> StagedCorpusArchive:
        if (
            not isinstance(stage, StagedCorpusArchive)
            or not all(
                isinstance(v, UUID) for v in (stage.corpus_id, stage.operation_id, stage.claim_id)
            )
            or not isinstance(stage.repo_id, str)
            or stage.repo_type not in ("dataset", "model")
            or stage.input_format not in ("zip", "corpus")
            or type(stage.archive_size) is not int
            or not 0 < stage.archive_size <= 3_000_000
            or not isinstance(stage.archive_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", stage.archive_sha256)
            or not isinstance(stage.requested_base_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", stage.requested_base_sha)
            or not isinstance(stage.stage_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", stage.stage_sha)
            or stage.stage_sha == stage.requested_base_sha
            or stage.stage_ref != "corpora-stage-" + str(stage.operation_id)
            or stage.stage_path
            != (f".corpora-staging/{stage.operation_id}/{stage.claim_id}.{stage.input_format}")
        ):
            raise CorpusMetadataError("Invalid trusted staging receipt")
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    raw = db.execute(
                        "SELECT row_to_json(corpora_corpus_metadata) "
                        "FROM public.corpora_corpus_metadata WHERE id=%s::uuid FOR UPDATE",
                        (str(stage.corpus_id),),
                    ).fetchone()
                    if raw is None:
                        raise CorpusMetadataConflict("Corpus staging receipt conflicts")
                    metadata = CorpusMetadata.model_validate(raw[0])
                    CorpusMetadataStore._check_link(metadata)
                    if (
                        metadata.id != stage.corpus_id
                        or metadata.hf_repo_id != stage.repo_id
                        or metadata.hf_repo_type != stage.repo_type
                    ):
                        raise CorpusMetadataConflict("Corpus staging receipt conflicts")
                    raw = db.execute(
                        "SELECT row_to_json(corpora_corpus_write_ops) "
                        "FROM public.corpora_corpus_write_ops WHERE id=%s::uuid FOR UPDATE",
                        (str(stage.operation_id),),
                    ).fetchone()
                    if raw is None or not isinstance(raw[0], dict):
                        raise CorpusMetadataConflict("Corpus staging receipt conflicts")
                    op = raw[0]
                    try:
                        sequence = op["expected_sequence"]
                        if (
                            UUID(op["id"]) != stage.operation_id
                            or UUID(op["corpus_id"]) != stage.corpus_id
                            or UUID(op["worker_claim"]) != stage.claim_id
                            or type(sequence) is not int
                            or sequence < 0
                            or op["input_format"] != stage.input_format
                            or op["input_sha256"] != stage.archive_sha256
                        ):
                            raise ValueError("operation mismatch")
                    except (KeyError, ValueError, TypeError, AttributeError) as error:
                        raise CorpusMetadataConflict("Corpus staging receipt conflicts") from error
                    if sequence == 0:
                        base = db.execute(
                            "SELECT commit_sha FROM public.corpora_corpus_bootstrap "
                            "WHERE corpus_id=%s::uuid",
                            (str(stage.corpus_id),),
                        ).fetchone()
                        actual_base = base[0] if base else None
                    else:
                        actual_base = op["expected_parent_sha"]
                    if actual_base != stage.requested_base_sha:
                        raise CorpusMetadataConflict("Corpus staging receipt conflicts")
                    values = (
                        stage.corpus_id,
                        stage.claim_id,
                        stage.repo_id,
                        stage.repo_type,
                        stage.input_format,
                        stage.archive_sha256,
                        stage.archive_size,
                        stage.requested_base_sha,
                        stage.stage_sha,
                        stage.stage_ref,
                        stage.stage_path,
                    )
                    existing = db.execute(
                        "SELECT corpus_id,worker_claim,repo_id,repo_type,input_format,input_sha256,"
                        "archive_size,requested_base_sha,stage_sha,stage_ref,stage_path "
                        "FROM public.corpora_corpus_staged_inputs WHERE operation_id=%s::uuid",
                        (str(stage.operation_id),),
                    ).fetchone()
                    if existing is not None:
                        if existing != values or op["state"] not in ("executing", "completed"):
                            raise CorpusMetadataConflict("Corpus staging receipt conflicts")
                    else:
                        if op["state"] != "executing":
                            raise CorpusMetadataConflict("Corpus staging requires reconciliation")
                        head = db.execute(
                            "SELECT sequence,commit_sha FROM public.corpora_corpus_heads "
                            "WHERE corpus_id=%s::uuid",
                            (str(stage.corpus_id),),
                        ).fetchone()
                        actual_head = (head[0], head[1]) if head else (0, None)
                        if (
                            actual_head != (sequence, op["expected_parent_sha"])
                            or (sequence == 0 and metadata.state != "provisioning")
                            or (sequence > 0 and metadata.state != "ready")
                        ):
                            raise CorpusMetadataConflict("Corpus staging receipt conflicts")
                        db.execute(
                            "INSERT INTO public.corpora_corpus_staged_inputs "
                            "(operation_id,corpus_id,worker_claim,repo_id,repo_type,input_format,"
                            "input_sha256,archive_size,requested_base_sha,stage_sha,"
                            "stage_ref,stage_path) "
                            "VALUES (%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                str(stage.operation_id),
                                str(stage.corpus_id),
                                str(stage.claim_id),
                                stage.repo_id,
                                stage.repo_type,
                                stage.input_format,
                                stage.archive_sha256,
                                stage.archive_size,
                                stage.requested_base_sha,
                                stage.stage_sha,
                                stage.stage_ref,
                                stage.stage_path,
                            ),
                        )
            return stage
        except CorpusMetadataConflict:
            raise
        except Exception:
            raise CorpusMetadataError(
                "Corpus staging receipt failed; reconcile before retrying"
            ) from None
