"""Async PostgreSQL access for the SQL governor.

Connects to Neon as the read-only `sql_governor_ro` role, so the connection is
physically incapable of writing — the database-level lock beneath the firewall.
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

import asyncpg
from dotenv import load_dotenv

load_dotenv()  # pull DATABASE_URL out of .env


class DatabaseError(RuntimeError):
    """Raised when Postgres rejects a query (syntax or relational error)."""


@runtime_checkable
class DatabaseConn(Protocol):
    """The slice of database behaviour the agent depends on."""

    read_only: bool

    async def dry_run(self, sql: str) -> str: ...
    async def fetch(self, sql: str) -> list[dict[str, Any]]: ...


class NeonDatabase:
    """A read-only asyncpg connection pool against Neon Postgres."""

    read_only = True

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str | None = None) -> "NeonDatabase":
        dsn = dsn or os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set — add it to your .env file.")
        # statement_cache_size=0 keeps us compatible with Neon's pooled endpoint.
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, statement_cache_size=0)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def dry_run(self, sql: str) -> str:
        """Plan the query via EXPLAIN without running it; raise on a bad query."""
        try:
            rows = await self._pool.fetch(f"EXPLAIN {sql}")
        except asyncpg.PostgresError as exc:
            raise DatabaseError(str(exc)) from exc
        return "\n".join(r["QUERY PLAN"] for r in rows)

    async def fetch(self, sql: str) -> list[dict[str, Any]]:
        """Run a SELECT and return rows as a list of dicts."""
        try:
            rows = await self._pool.fetch(sql)
        except asyncpg.PostgresError as exc:
            raise DatabaseError(str(exc)) from exc
        return [dict(r) for r in rows]