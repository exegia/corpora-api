#!/usr/bin/env python3
"""Initialize private buckets and an optional superadmin on a local Supabase stack."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx
import psycopg

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
BUCKETS = ("conversion-jobs", "project-corpora")


def initialize(workdir: Path, superadmin: UUID | None = None) -> None:
    """Create the two private buckets and optionally grant one confirmed local user."""
    result = subprocess.run(
        ["supabase", "status", "--workdir", str(workdir), "--output-format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    env = json.loads(result.stdout)["env"]
    api_url, db_url, key = env["API_URL"].rstrip("/"), env["DB_URL"], env["SERVICE_ROLE_KEY"]
    if (
        urlparse(api_url).hostname not in LOCAL_HOSTS
        or urlparse(db_url).hostname not in LOCAL_HOSTS
    ):
        raise ValueError("Fresh bootstrap initialization is limited to a local Supabase stack")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    with psycopg.connect(db_url) as db, httpx.Client(timeout=20, trust_env=False) as client:
        if (
            db.execute("SELECT to_regclass('corpora_session_guard.superadmins')").fetchone()[0]
            is None
        ):
            raise ValueError("Apply the fresh baseline before initializing the stack")
        if (
            superadmin is not None
            and not db.execute(
                "SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=%s AND deleted_at IS NULL "
                "AND (email_confirmed_at IS NOT NULL OR phone_confirmed_at IS NOT NULL))",
                (superadmin,),
            ).fetchone()[0]
        ):
            raise ValueError("The requested superadmin must be a confirmed local Auth user")
        for bucket in BUCKETS:
            url = f"{api_url}/storage/v1/bucket/{bucket}"
            response = client.get(url, headers=headers)
            missing = response.status_code == 404
            if response.status_code == 400:
                try:
                    detail = response.json()
                    missing = (
                        isinstance(detail, dict)
                        and str(detail.get("statusCode")) == "404"
                        and detail.get("error") == "Bucket not found"
                    )
                except ValueError:
                    missing = False
            if missing:
                response = client.post(
                    f"{api_url}/storage/v1/bucket",
                    headers=headers,
                    json={"id": bucket, "name": bucket, "public": False},
                )
                response.raise_for_status()
                response = client.get(url, headers=headers)
            response.raise_for_status()
            value = response.json()
            if (
                not isinstance(value, dict)
                or value.get("id") != bucket
                or value.get("public") is not False
            ):
                raise ValueError(f"Bucket {bucket} exists but is not private")
        if db.execute(
            "SELECT count(*) FROM storage.buckets WHERE id = ANY(%s) AND public IS FALSE",
            (list(BUCKETS),),
        ).fetchone()[0] != len(BUCKETS):
            raise ValueError(
                "The local API and database do not refer to the same initialized stack"
            )
        if superadmin is not None:
            db.execute(
                "INSERT INTO corpora_session_guard.superadmins(user_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (superadmin,),
            )
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workdir", type=Path, required=True, help="The fresh local Supabase workdir"
    )
    parser.add_argument(
        "--superadmin-user", type=UUID, help="An existing, confirmed local Auth user UUID"
    )
    args = parser.parse_args()
    try:
        initialize(args.workdir, args.superadmin_user)
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        psycopg.Error,
    ):
        parser.exit(
            1, "Could not initialize the fresh local Supabase stack; check its status and inputs.\n"
        )
    print(
        "Private buckets are ready."
        + (
            " The requested superadmin is configured."
            if args.superadmin_user
            else " No superadmin was assigned."
        )
    )


if __name__ == "__main__":
    main()
