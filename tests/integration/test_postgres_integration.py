from __future__ import annotations

import os

import psycopg
import pytest


pytestmark = pytest.mark.integration


def _db_config() -> dict[str, object]:
    return {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.getenv("POSTGRES_DATABASE", os.getenv("POSTGRES_DB", "trader")),
        "user": os.getenv("POSTGRES_USER", "trader"),
        "password": os.getenv("POSTGRES_PASSWORD", "change_me"),
        "sslmode": os.getenv("POSTGRES_SSLMODE", "disable"),
        "connect_timeout": int(os.getenv("POSTGRES_CONNECT_TIMEOUT", "5")),
    }


def test_postgres_connection_roundtrip() -> None:
    config = _db_config()

    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()

    assert row == (1,)


def test_expected_component_schemas_exist() -> None:
    config = _db_config()

    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT nspname
                FROM pg_namespace
                WHERE nspname = ANY(%s)
                ORDER BY nspname
                """,
                (["analyzer_worker", "news_fetcher", "shared", "trade_executor"],),
            )
            rows = cur.fetchall()

    assert [row[0] for row in rows] == [
        "analyzer_worker",
        "news_fetcher",
        "shared",
        "trade_executor",
    ]
