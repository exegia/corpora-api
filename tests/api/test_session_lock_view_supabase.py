"""Opt-in security experiment; this is not a deployable migration."""

from __future__ import annotations

import base64
import json
import os
import threading
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_restricted_view_locks_real_session_without_service_update():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    url = os.environ["API_URL"].rstrip("/")
    dsn = os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    suffix = uuid4().hex[:12]
    owner_role, schema = "lock_owner_" + suffix, "lock_test_" + suffix
    view = sql.Identifier(schema, "session_lock")
    owner_ident = sql.Identifier(owner_role)
    schema_ident = sql.Identifier(schema)
    user = None
    role_created = schema_created = False
    with (
        httpx.Client(timeout=15) as api,
        psycopg.connect(dsn, user="supabase_admin", autocommit=True) as admin,
    ):
        assert admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[
            0
        ]
        try:
            signup = api.post(
                url + "/auth/v1/signup",
                headers={"apikey": anon, "Authorization": "Bearer " + anon},
                json={"email": str(uuid4()) + "@example.org", "password": str(uuid4())},
            )
            signup.raise_for_status()
            token = signup.json()["access_token"]
            claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
            user, session = claims["sub"], claims["session_id"]
            assert not admin.execute(
                "SELECT has_column_privilege('service_role','auth.sessions','not_after','UPDATE')"
            ).fetchone()[0]
            admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT BYPASSRLS").format(owner_ident))
            role_created = True
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(schema_ident))
            schema_created = True
            admin.execute(
                sql.SQL("GRANT CREATE, USAGE ON SCHEMA {} TO {}").format(schema_ident, owner_ident)
            )
            admin.execute("SET ROLE supabase_auth_admin")
            try:
                admin.execute(sql.SQL("GRANT USAGE ON SCHEMA auth TO {}").format(owner_ident))
                admin.execute(
                    sql.SQL(
                        "GRANT SELECT(id,user_id,not_after), UPDATE(not_after) "
                        "ON auth.sessions TO {}"
                    ).format(owner_ident)
                )
            finally:
                admin.execute("RESET ROLE")
            admin.execute(
                sql.SQL(
                    "CREATE VIEW {} AS SELECT id,user_id,not_after,"
                    "NULL::integer AS lock_permission FROM auth.sessions"
                ).format(view)
            )
            admin.execute(sql.SQL("ALTER VIEW {} OWNER TO {}").format(view, owner_ident))
            admin.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(schema_ident, owner_ident)
            )
            admin.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO service_role").format(schema_ident))
            admin.execute(
                sql.SQL(
                    "GRANT SELECT(id,user_id,not_after), UPDATE(lock_permission) "
                    "ON {} TO service_role"
                ).format(view)
            )
            assert not admin.execute(
                "SELECT pg_has_role('service_role', %s, 'MEMBER')", (owner_role,)
            ).fetchone()[0]
            for browser in ("anon", "authenticated"):
                assert not admin.execute(
                    "SELECT has_schema_privilege(%s,%s,'USAGE')", (browser, schema)
                ).fetchone()[0]
                assert not admin.execute(
                    "SELECT has_table_privilege(%s,%s,'SELECT')",
                    (browser, schema + ".session_lock"),
                ).fetchone()[0]

            finished, result = threading.Event(), []

            def logout():
                try:
                    with httpx.Client(timeout=15) as client:
                        response = client.post(
                            url + "/auth/v1/logout",
                            headers={"apikey": anon, "Authorization": "Bearer " + token},
                        )
                        result.append(response.status_code)
                finally:
                    finished.set()

            thread = threading.Thread(target=logout, daemon=True)
            with psycopg.connect(dsn) as lock:
                lock.execute("SET ROLE service_role")
                found = lock.execute(
                    sql.SQL("SELECT id FROM {} WHERE id=%s AND user_id=%s FOR SHARE").format(view),
                    (session, user),
                ).fetchall()
                assert len(found) == 1
                thread.start()
                try:
                    assert not finished.wait(0.5), "logout completed while session was locked"
                finally:
                    lock.rollback()
                    thread.join(timeout=10)
            assert finished.is_set() and result == [204]
            with psycopg.connect(dsn, autocommit=True) as limited:
                limited.execute("SET ROLE service_role")
                for target, column, value, expected_state in (
                    (sql.SQL("auth.sessions"), "not_after", "now()", "42501"),
                    (view, "not_after", "now()", "42501"),
                    (view, "lock_permission", "1", "0A000"),
                ):
                    with pytest.raises(psycopg.Error) as error:
                        limited.execute(
                            sql.SQL("UPDATE {} SET {}={} WHERE id=%s").format(
                                target, sql.Identifier(column), sql.SQL(value)
                            ),
                            (session,),
                        )
                    assert error.value.sqlstate == expected_state
                assert not limited.execute(
                    sql.SQL("SELECT id FROM {} WHERE id=%s").format(view), (session,)
                ).fetchall()
        finally:
            if user:
                api.delete(
                    url + "/auth/v1/admin/users/" + user,
                    headers={"apikey": service, "Authorization": "Bearer " + service},
                ).raise_for_status()
            if schema_created:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema_ident))
            if role_created:
                admin.execute(sql.SQL("REVOKE ALL ON auth.sessions FROM {}").format(owner_ident))
                admin.execute(sql.SQL("REVOKE USAGE ON SCHEMA auth FROM {}").format(owner_ident))
                admin.execute(sql.SQL("DROP ROLE {}").format(owner_ident))
