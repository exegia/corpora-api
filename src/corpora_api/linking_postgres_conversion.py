"""Authenticated conversion event registry; no schema deployment."""

from datetime import UTC, datetime
from uuid import UUID

import psycopg
from corpora_linking import Detector
from psycopg.types.json import Jsonb

from .linking_conversion import ConversionInput
from .linking_events import ConversionEvent, prepare_conversion
from .linking_postgres import PostgreSQLReferenceStore
from .linking_postgres_publication import _authority, _lock_event
from .linking_store import VersionConflictError, WorkingRevision


class PostgreSQLConversionEventRegistry:
    store: PostgreSQLReferenceStore

    def __init__(self, store: PostgreSQLReferenceStore):
        self.store = store

    def get(self, event_id: UUID) -> ConversionEvent | None:
        with psycopg.connect(self.store.dsn) as db:
            actor = self.store._authorize(db, None)
            row = db.execute(
                "SELECT record FROM reference_working.conversion_events "
                "WHERE space_id = %s AND authority_id = %s AND event_id = %s",
                (self.store.space_id, _authority(db, self.store), event_id),
            ).fetchone()
            event = ConversionEvent.model_validate(row[0]) if row else None
            self.store._check_access(db, actor, event)
            return event

    def register(
        self,
        conversion: ConversionInput,
        detector: Detector,
        *,
        event_id: UUID,
        detector_revision: str,
        reason: str,
    ) -> ConversionEvent:
        report, digest = prepare_conversion(
            conversion, detector, detector_revision=detector_revision, reason=reason
        )
        try:
            with psycopg.connect(self.store.dsn) as db:
                actor = self.store._authorize(db, "contribute")
                self.store._check_access(db, actor, report)
                authority = _authority(db, self.store)
                _lock_event(db, self.store, "conversion", event_id)
                row = db.execute(
                    "SELECT record FROM reference_working.conversion_events "
                    "WHERE space_id = %s AND authority_id = %s AND event_id = %s",
                    (self.store.space_id, authority, event_id),
                ).fetchone()
                if row:
                    prior = ConversionEvent.model_validate(row[0])
                    self.store._check_access(db, actor, prior)
                    if (
                        prior.input_digest != digest
                        or prior.actor_id != str(actor)
                        or prior.reason != reason
                    ):
                        raise VersionConflictError(
                            "conversion event identity reused "
                            "with changed input, actor, reason, detector or output"
                        )
                    return prior
                event = ConversionEvent(
                    authority_id=authority,
                    event_id=event_id,
                    detector_revision=detector_revision,
                    input_digest=digest,
                    actor_id=str(actor),
                    reason=reason,
                    recorded_at=datetime.now(UTC),
                    report=report,
                )
                for item in sorted(report.references, key=lambda item: str(item.reference.id)):
                    db.execute(
                        "INSERT INTO reference_working.heads VALUES (%s, %s, 1)",
                        (self.store.space_id, item.reference.id),
                    )
                    self.store._insert_revision(
                        db,
                        WorkingRevision(
                            version=1,
                            actor_id=str(actor),
                            reason=reason,
                            recorded_at=event.recorded_at,
                            action="edit",
                            reference=item.reference,
                            conversion=item,
                        ),
                    )
                db.execute(
                    "INSERT INTO reference_working.conversion_events "
                    "(space_id, authority_id, event_id, fingerprint, "
                    "detector_revision, report, actor_id, recorded_at, "
                    "record) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        self.store.space_id,
                        authority,
                        event_id,
                        digest.removeprefix("sha256:"),
                        detector_revision,
                        Jsonb(report.model_dump(mode="json")),
                        actor,
                        event.recorded_at,
                        Jsonb(event.model_dump(mode="json")),
                    ),
                )
                return event
        except psycopg.errors.UniqueViolation as exc:
            raise VersionConflictError(
                "detected reference ID already belongs to working history"
            ) from exc
