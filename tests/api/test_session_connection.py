from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace

import psycopg
import pytest

from corpora_api.session_connection import SessionDatabase


def config(**overrides):
    values = {
        "host": "db.example.test",
        "hostaddr": "192.0.2.1",
        "port": 5432,
        "database": "postgres",
        "user": "reader",
        "password": "example-password",
    }
    return SessionDatabase(**(values | overrides))


def test_connector_forces_verified_tls_and_a_single_bounded_address(monkeypatch):
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    database = config()
    database.connect()
    assert calls[0]["host"] == "db.example.test"
    assert calls[0]["hostaddr"] == "192.0.2.1"
    assert calls[0]["connect_timeout"] == 5
    assert calls[0]["sslmode"] == "verify-full"
    assert calls[0]["gssencmode"] == "disable"
    assert calls[0]["autocommit"] is True
    assert "example-password" not in repr(database)


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "db.example.test,other.test"},
        {"hostaddr": "192.0.2.1,192.0.2.2"},
        {"hostaddr": "0.0.0.0"},
        {"allow_insecure_local": True},
        {"host": "db.example.test", "hostaddr": "127.0.0.1", "allow_insecure_local": True},
        {"port": 0},
        {"database": "host=other"},
        {"database": "postgresql://other/db"},
        {"password": ""},
    ],
)
def test_invalid_connection_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        config(**changes)


def test_stalled_local_handshake_times_out():
    accepted = threading.Event()
    release = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def stall():
            connection, _ = listener.accept()
            with connection:
                accepted.set()
                release.wait(12)

        worker = threading.Thread(target=stall, daemon=True)
        worker.start()
        database = replace(
            config(),
            host="127.0.0.1",
            hostaddr="127.0.0.1",
            port=listener.getsockname()[1],
            allow_insecure_local=True,
        )
        start = time.monotonic()
        try:
            with pytest.raises(psycopg.OperationalError):
                database.connect()
            elapsed = time.monotonic() - start
            assert accepted.is_set() and 3 <= elapsed < 10
        finally:
            release.set()
            worker.join(2)
