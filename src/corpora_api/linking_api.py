"""Opt-in owner-authorized working references; configuration stays server-owned."""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

import psycopg
from corpora_linking import Endpoint, Provenance, Reference, ScholarlyCitationMention
from corpora_linking.models import NonEmpty, Value
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field, ValidationError

from .auth import UserAuthError
from .linking_runtime import LinkingInventory, LinkingRuntime
from .linking_store import VersionConflictError

router = APIRouter(prefix="/v1/linking", tags=["linking"])


class SaveLink(Value):
    reference: Reference
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    reason: NonEmpty


class Decision(Value):
    expected_version: int = Field(ge=1, strict=True)
    reason: NonEmpty
    allow_unresolved_target: bool = False


class ExportRequest(Value):
    reference_ids: tuple[UUID, ...]


class PublicationRequest(Decision):
    event_id: UUID
    snapshot: str | None = None


class BrowserSelectionRequest(Value):
    original: Endpoint
    asset_id: NonEmpty
    captured_nodes: tuple[tuple[tuple[int, ...], str], ...]
    start_path: tuple[int, ...]
    start_utf16: int = Field(ge=0, strict=True)
    end_path: tuple[int, ...]
    end_utf16: int = Field(ge=0, strict=True)


class DiscoveryRequest(Value):
    source: Endpoint
    mention: ScholarlyCitationMention
    event_id: UUID
    reason: NonEmpty


class WorkChoice(Decision):
    work_id: NonEmpty | None = None


@lru_cache(maxsize=1)
def _environment_runtime(dsn: str, inventory_path: str) -> LinkingRuntime:
    inventory = LinkingInventory.model_validate_json(Path(inventory_path).read_bytes())
    return LinkingRuntime(dsn=dsn, inventory=inventory)


def _configured(request: Request) -> LinkingRuntime:
    runtime = getattr(request.app.state, "linking_runtime", None)
    if runtime is not None:
        return runtime
    if os.getenv("LINKING_ENABLED", "").lower() not in ("1", "true"):
        raise HTTPException(503, "Reference linking is unavailable")
    dsn, inventory = os.getenv("LINKING_DATABASE_URL"), os.getenv("LINKING_INVENTORY")
    if not dsn or not inventory:
        raise HTTPException(503, "Reference linking is unavailable")
    try:
        return _environment_runtime(dsn, inventory)
    except (OSError, ValueError, ValidationError) as error:
        raise HTTPException(503, "Reference linking is unavailable") from error


@contextmanager
def _operation(request: Request, space_id: UUID):
    runtime = _configured(request)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "Sign in to manage references")
    try:
        yield runtime, runtime.store(space_id, token)
    except UserAuthError as error:
        raise HTTPException(401, "Session is invalid or expired") from error
    except PermissionError as error:
        raise HTTPException(403, "Reference access denied") from error
    except VersionConflictError as error:
        raise HTTPException(409, "Reference changed; reload before retrying") from error
    except (ValueError, IndexError) as error:
        raise HTTPException(422, "Reference selection or transition is invalid") from error
    except (psycopg.Error, ImportError) as error:
        raise HTTPException(503, "Reference linking is unavailable") from error


@router.post("/{space_id}/references")
def save_link(space_id: UUID, body: SaveLink, request: Request):
    with _operation(request, space_id) as (_, store):
        previous = store.get(body.reference.id) if body.expected_version is not None else None
        provenance = (
            previous.provenance
            if previous is not None
            else Provenance(origin="manual", agent_id=store.actor_id, method="reader-selection")
        )
        reference = Reference.model_validate(
            {
                **body.reference.model_dump(),
                "provenance": provenance,
                "resolution": "unresolved",
                "review": "pending",
                "publication": "draft",
            }
        )
        version = store.save(reference, expected_version=body.expected_version, reason=body.reason)
        return {"version": version, "reference": store.get(reference.id)}


@router.get("/{space_id}/references/{reference_id}")
def reference_history(space_id: UUID, reference_id: UUID, request: Request):
    with _operation(request, space_id) as (_, store):
        history = store.history(reference_id)
        if not history:
            raise HTTPException(404, "Reference not found")
        return {"history": history}


@router.post("/{space_id}/references/{reference_id}/{action}")
def reference_decision(
    space_id: UUID,
    reference_id: UUID,
    action: Literal["resolve", "approve", "reject", "reopen"],
    body: Decision,
    request: Request,
):
    with _operation(request, space_id) as (runtime, store):
        parameters = dict(expected_version=body.expected_version, reason=body.reason)
        if action == "resolve":
            version = store.resolve_target(
                reference_id, resolver=runtime.resolver(store), **parameters
            )
        elif action == "approve":
            version = store.approve(
                reference_id,
                resolver=runtime.resolver(store),
                allow_unresolved_target=body.allow_unresolved_target,
                **parameters,
            )
        elif action == "reject":
            version = store.reject(reference_id, **parameters)
        else:
            version = store.reopen(reference_id, **parameters)
        return {"version": version}


@router.post("/{space_id}/retrieve")
def retrieve_selection(space_id: UUID, endpoint: Endpoint, request: Request):
    with _operation(request, space_id) as (runtime, store):
        return {"text": runtime.resolver(store).retrieve(endpoint)}


@router.post("/{space_id}/export")
def export_references(space_id: UUID, body: ExportRequest, request: Request):
    with _operation(request, space_id) as (_, store):
        from .linking_postgres_publication import PostgreSQLPublicationLedger

        return Response(
            PostgreSQLPublicationLedger(store).export_current(body.reference_ids),
            media_type="application/json",
        )


@router.post("/{space_id}/publication/{reference_id}/{action}")
def publication(
    space_id: UUID,
    reference_id: UUID,
    action: Literal["acknowledge", "withdraw"],
    body: PublicationRequest,
    request: Request,
):
    with _operation(request, space_id) as (_, store):
        from .linking_postgres_publication import PostgreSQLPublicationLedger

        ledger = PostgreSQLPublicationLedger(store)
        if action == "acknowledge":
            if body.snapshot is None:
                raise HTTPException(422, "Approved snapshot is required")
            return ledger.acknowledge_snapshot(
                body.snapshot.encode(),
                reference_id,
                event_id=body.event_id,
                expected_version=body.expected_version,
                reason=body.reason,
            )
        return ledger.withdraw(
            reference_id,
            event_id=body.event_id,
            expected_version=body.expected_version,
            reason=body.reason,
        )


@router.post("/{space_id}/browser-selection")
def browser_selection(space_id: UUID, body: BrowserSelectionRequest, request: Request):
    with _operation(request, space_id) as (runtime, store):
        from .linking_access import endpoint_scope
        from .linking_html_ranges import make_browser_html_range

        with psycopg.connect(store.dsn) as db:
            actor = store._authorize(db, None)
            store._check_access(db, actor, body.original)
            assets = [
                asset
                for asset in runtime.inventory.assets
                if asset.format == "html"
                and endpoint_scope(asset.endpoint)[0] == endpoint_scope(body.original)[0]
            ]
            if len(assets) != 1:
                raise ValueError("HTML asset unavailable or ambiguous")
            endpoint = make_browser_html_range(
                assets[0].data,
                body.original,
                asset_id=body.asset_id,
                captured_nodes=body.captured_nodes,
                start_path=body.start_path,
                start_utf16=body.start_utf16,
                end_path=body.end_path,
                end_utf16=body.end_utf16,
            )
        return {"endpoint": endpoint, "text": runtime.resolver(store).retrieve(endpoint)}


@router.post("/{space_id}/discoveries")
def register_discovery(space_id: UUID, body: DiscoveryRequest, request: Request):
    with _operation(request, space_id) as (runtime, store):
        from corpora_linking import SnapshotCatalog, identify_scholarly_citation

        from .linking_postgres_discoveries import PostgreSQLDiscoveryStore

        snapshot = runtime.snapshot(store, body.source)
        discovery = identify_scholarly_citation(
            snapshot,
            body.mention,
            SnapshotCatalog(entries=runtime.inventory.catalog),
            provenance=Provenance(
                origin="automatic", agent_id=store.actor_id, method="submitted-scholarly-span"
            ),
        )
        return PostgreSQLDiscoveryStore(store).register(
            discovery, snapshot, event_id=body.event_id, reason=body.reason
        )


@router.get("/{space_id}/discoveries/{discovery_id}")
def discovery_history(space_id: UUID, discovery_id: UUID, request: Request):
    with _operation(request, space_id) as (_, store):
        from .linking_postgres_discoveries import PostgreSQLDiscoveryStore

        return {"history": PostgreSQLDiscoveryStore(store).history(discovery_id)}


@router.post("/{space_id}/discoveries/{discovery_id}/{action}")
def discovery_decision(
    space_id: UUID,
    discovery_id: UUID,
    action: Literal["identify", "reject", "select"],
    body: WorkChoice,
    request: Request,
):
    with _operation(request, space_id) as (runtime, store):
        from corpora_linking import SnapshotCatalog

        from .linking_postgres_discoveries import PostgreSQLDiscoveryStore

        discoveries = PostgreSQLDiscoveryStore(store)
        if action == "select":
            if body.work_id is None:
                raise HTTPException(422, "Choose an identified work")
            return discoveries.select_work(
                discovery_id,
                body.work_id,
                expected_version=body.expected_version,
                reason=body.reason,
            )
        if action == "reject":
            return discoveries.reject(
                discovery_id, expected_version=body.expected_version, reason=body.reason
            )
        discovery = discoveries.get(discovery_id)
        if discovery is None:
            raise HTTPException(404, "Citation discovery not found")
        snapshot = runtime.snapshot(store, discovery.source.model_copy(update={"locators": ()}))
        return discoveries.refresh_identification(
            discovery_id,
            snapshot,
            SnapshotCatalog(entries=runtime.inventory.catalog),
            expected_version=body.expected_version,
            reason=body.reason,
        )
