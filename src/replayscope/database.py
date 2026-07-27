from __future__ import annotations

from importlib.resources import files

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def create_pool(database_url: str, *, open: bool = True) -> ConnectionPool:
    return ConnectionPool(
        database_url, min_size=1, max_size=20, open=open, kwargs={"row_factory": dict_row}
    )


def migrate(pool: ConnectionPool) -> None:
    schema = files("replayscope").joinpath("schema.sql").read_text()
    with pool.connection() as conn:
        exists = conn.execute("SELECT to_regclass('public.schema_migrations') AS name").fetchone()
        if exists["name"] is None:
            conn.execute(schema)
            return
        applied = conn.execute(
            "SELECT 1 AS applied FROM schema_migrations WHERE version = '001'"
        ).fetchone()
        if not applied:
            conn.execute(schema)


def ping(pool: ConnectionPool) -> bool:
    try:
        with pool.connection() as conn:
            return conn.execute("SELECT 1 AS value").fetchone()["value"] == 1
    except Exception:  # noqa: BLE001 - dependency health becomes false
        return False
