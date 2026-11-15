"""Unwired direct PostgreSQL connector for the session-bound reader.

The caller must provision and review a suitable login. The numeric host address
avoids DNS resolution during connection; the host name is retained for TLS
certificate verification. Credentials must come from trusted server config.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

import psycopg


@dataclass(frozen=True, repr=False)
class SessionDatabase:
    """Create a fresh, time-limited connection using verified TLS by default."""

    host: str
    hostaddr: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    allow_insecure_local: bool = False

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.hostaddr)
        except ValueError as error:
            raise ValueError("A single numeric database address is required") from error
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Invalid database address")
        try:
            host_is_local = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", self.host):
                raise ValueError("A single database host is required") from None
            host_is_local = self.host.lower() == "localhost"
        if self.allow_insecure_local and not (address.is_loopback and host_is_local):
            raise ValueError("Insecure connections are restricted to loopback")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Invalid database port")
        if not self.database or "=" in self.database or "://" in self.database:
            raise ValueError("Invalid database name")
        if not self.user or not self.password:
            raise ValueError("Database credentials are required")

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(
            host=self.host,
            hostaddr=self.hostaddr,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
            connect_timeout=5,
            sslmode="disable" if self.allow_insecure_local else "verify-full",
            gssencmode="disable",
            autocommit=True,
        )
