"""Exercise TLS verification against a disposable PostgreSQL server."""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from corpora_api.session_connection import SessionDatabase

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN") or not shutil.which("openssl"),
    reason="requires isolated PostgreSQL binaries and openssl",
)


def test_verified_tls_rejects_wrong_hostname_and_untrusted_certificate(monkeypatch):
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    with tempfile.TemporaryDirectory(prefix="session-tls-", dir="/tmp") as temporary:
        root = Path(temporary)
        cert, key = root / "server.crt", root / "server.key"
        other_cert, other_key = root / "other.crt", root / "other.key"
        for certificate, private_key in ((cert, key), (other_cert, other_key)):
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(private_key),
                    "-out",
                    str(certificate),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=db.example.test",
                    "-addext",
                    "subjectAltName=DNS:db.example.test",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        key.chmod(0o600)
        cluster = root / "data"
        init = [str(binaries / "initdb"), "-D", str(cluster), "-U", "postgres", "-A", "trust"]
        if share := os.getenv("CORPORA_TEST_POSTGRES_SHARE"):
            init.extend(["-L", share])
        subprocess.run(init, check=True, capture_output=True, timeout=60)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ctl = str(binaries / "pg_ctl")
        options = shlex.join(
            [
                "-k",
                temporary,
                "-p",
                str(port),
                "-c",
                "listen_addresses=127.0.0.1",
                "-c",
                "ssl=on",
                "-c",
                f"ssl_cert_file={cert}",
                "-c",
                f"ssl_key_file={key}",
            ]
        )
        subprocess.run(
            [ctl, "-D", str(cluster), "-l", str(root / "pg.log"), "-o", options, "start"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            database = SessionDatabase(
                host="db.example.test",
                hostaddr="127.0.0.1",
                port=port,
                database="postgres",
                user="postgres",
                password="test-only-password",
            )
            monkeypatch.setenv("PGSSLROOTCERT", str(cert))
            with database.connect() as db:
                assert db.execute(
                    "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
                ).fetchone()[0]
            with pytest.raises(psycopg.OperationalError, match="certificate"):
                replace(database, host="wrong.example.test").connect()
            monkeypatch.setenv("PGSSLROOTCERT", str(other_cert))
            with pytest.raises(psycopg.OperationalError, match="certificate"):
                database.connect()
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "immediate", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )
