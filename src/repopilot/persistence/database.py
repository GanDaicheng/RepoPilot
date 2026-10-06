"""Short-lived configured aiosqlite connections and schema initialization."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from repopilot.persistence.migrations import SCHEMA_SQL, SCHEMA_VERSION


class SqliteDatabase:
    def __init__(self, path: Path):
        self.path = Path(path)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        async with self.connection() as connection:
            await connection.executescript(SCHEMA_SQL)
            row = await (
                await connection.execute(
                    "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
                )
            ).fetchone()
            if row is None:
                await connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif int(row["version"]) != SCHEMA_VERSION:
                raise RuntimeError("Unsupported RepoPilot database schema version.")
            await connection.commit()
