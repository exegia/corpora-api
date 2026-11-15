"""Opt-in owner-authorized working references; configuration stays server-owned."""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

import psycopg
from corpora_linking import Provenance, Reference
from corpora_linking.models import NonEmpty, Value
from fastapi import APIRouter, HTTPException, Request
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
