"""Prepare an offline import of an owner-scoped flat Supabase archive.

No route calls this module. All legacy readers and writers must be coordinated
at cutover: the source lock covers import, not future legacy writes or deletes.
"""

from __future__ import annotations

import hashlib
import re
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.storage import SupabaseCorpusStorage
from corpora_api.storage_detail import download_stored_archive
from corpora_api.stored_revisions import Revision, StoredRevisions
from corpora_api.validation import ValidationBridge


class SourceStamp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    version: str = Field(min_length=1, max_length=256)


class StoredArchiveImporter:
    def __init__(
        self, source: SupabaseCorpusStorage, revisions: StoredRevisions, validator: ValidationBridge
    ) -> None:
        self.source, self.revisions, self.validator = source, revisions, validator

    def import_archive(self, owner: str, filename: str) -> Revision:
        owner, filename, _ = self.revisions._identity(owner, filename)
        if not filename.endswith(".corpus"):
            raise HTTPException(422, "Only corpus archives can be versioned")
        source_url, source_key, bucket = self.source._config()
        target_url, target_headers = self.revisions._config()
        if source_url != target_url or target_headers["apikey"] != source_key:
            raise HTTPException(503, "Source and revision storage do not match")
        stamp_value = self.revisions._rpc(
            "corpora_legacy_source_stamp",
            {"p_owner": owner, "p_bucket": bucket, "p_filename": filename},
        )
        try:
            stamp = SourceStamp.model_validate(stamp_value)
        except ValidationError as error:
            raise HTTPException(503, "Source storage returned invalid metadata") from error
        archive = download_stored_archive(self.source, owner, filename)
        if self.validator.validate(filename, archive).get("valid") is not True:
            raise HTTPException(422, "Source archive failed validation")
        try:
            manifest = run_corpus(archive, "view", view="manifest")
        except CorpusProcessError as error:
            raise HTTPException(503, "Source archive manifest is unavailable") from error
        version = manifest.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"v?1\.\d+", version):
            raise HTTPException(422, "Source archive version cannot be edited")
        fact = Revision(
            owner=owner,
            filename=filename,
            revision=uuid4(),
            digest="sha256:" + hashlib.sha256(archive).hexdigest(),
            version=version,
        )
        self.revisions.stage(fact, archive)
        self.revisions._rpc(
            "corpora_stored_import",
            {
                "p_owner": owner,
                "p_bucket": bucket,
                "p_filename": filename,
                "p_source_id": str(stamp.id),
                "p_source_version": stamp.version,
                "p_revision": str(fact.revision),
                "p_digest": fact.digest,
                "p_version": version,
            },
        )
        return fact
