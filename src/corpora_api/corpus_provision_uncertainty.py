"""Record an ambiguous repository creation without authorizing another attempt.

This internal adapter does not inspect the Hub, stop a worker, or reconcile an
operation. Only a trusted dispatcher may call it with its original claim.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from corpora_api.corpus_metadata import CorpusMetadataConflict, CorpusMetadataError


class CorpusProvisionUncertaintyRecorder:
    """Keep the one-shot claim when the remote outcome is unknown."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def record_trusted(self, operation_id: UUID, claim_id: UUID) -> None:
        if not isinstance(operation_id, UUID) or not isinstance(claim_id, UUID):
            raise CorpusMetadataError("Invalid provisioning claim")
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    result = db.execute(
                        "SELECT corpora_session_guard.mark_provision_uncertain(%s::uuid,%s::uuid)",
                        (str(operation_id), str(claim_id)),
                    ).fetchone()
                    if result is None or result[0] is not True:
                        raise CorpusMetadataConflict("Provisioning claim is unavailable")
        except CorpusMetadataConflict:
            raise
        except Exception:
            raise CorpusMetadataError(
                "Provisioning outcome could not be recorded; reconcile before retrying"
            ) from None
