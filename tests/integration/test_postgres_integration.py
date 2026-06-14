from __future__ import annotations

import psycopg
import pytest

from tests.integration._db_test_helper import (
    bootstrap_newsfetcher_schema,
    db_config,
    ensure_postgres_access,
    ensure_safe_test_database,
    ensure_test_database_exists,
)


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _enforce_test_db() -> None:
    config = db_config()
    ensure_postgres_access(config)
    ensure_safe_test_database(config)
    ensure_test_database_exists(config)
    bootstrap_newsfetcher_schema(config)


def test_postgres_connection_roundtrip() -> None:
    config = db_config()

    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()

    assert row == (1,)


def test_expected_component_schemas_exist() -> None:
    config = db_config()

    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT nspname
                FROM pg_namespace
                WHERE nspname = ANY(%s)
                ORDER BY nspname
                """,
                (["thesis_builder", "news_fetcher", "shared", "trade_executor"],),
            )
            rows = cur.fetchall()

    assert [row[0] for row in rows] == [
        "thesis_builder",
        "news_fetcher",
        "shared",
        "trade_executor",
    ]
