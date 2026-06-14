from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from psycopg import sql


_ENV_LOADED = False


def _ensure_integration_env_loaded() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    repo_root = Path(__file__).resolve().parents[2]
    _load_env_file(repo_root / ".env.shared")
    _load_env_file(repo_root / ".env.test")
    _ENV_LOADED = True


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def db_config() -> dict[str, object]:
    _ensure_integration_env_loaded()
    return {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.getenv("POSTGRES_DATABASE", os.getenv("POSTGRES_DB", "trader")),
        "user": os.getenv("POSTGRES_USER", "trader"),
        "password": os.getenv("POSTGRES_PASSWORD", ""),
        "sslmode": os.getenv("POSTGRES_SSLMODE", "disable"),
        "connect_timeout": int(os.getenv("POSTGRES_CONNECT_TIMEOUT", "5")),
    }


def ensure_safe_test_database(config: dict[str, object]) -> None:
    db_name = str(config["dbname"]).strip().lower()
    prod_db_name = os.getenv("POSTGRES_PROD_DATABASE", "trader").strip().lower()

    if db_name == prod_db_name:
        pytest.fail(
            "Integration tests must run against a dedicated test DB. "
            f"Current dbname '{db_name}' matches production dbname '{prod_db_name}'. "
            "Set POSTGRES_DATABASE to a separate test database such as 'trader_test'."
        )

    if "test" not in db_name:
        pytest.fail(
            "Integration tests require an explicit test database name. "
            f"Current dbname is '{db_name}'. Use a database containing 'test' in its name."
        )


def ensure_postgres_access(config: dict[str, object]) -> None:
    admin_config = dict(config)
    admin_config["dbname"] = os.getenv("POSTGRES_ADMIN_DATABASE", "postgres")
    try:
        with psycopg.connect(**admin_config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except psycopg.OperationalError as exc:
        pytest.skip(
            "PostgreSQL is not reachable with current integration-test credentials. "
            f"Details: {exc}"
        )


def ensure_test_database_exists(config: dict[str, object]) -> None:
    db_name = str(config["dbname"]).strip()

    admin_config = dict(config)
    admin_config["dbname"] = os.getenv("POSTGRES_ADMIN_DATABASE", "postgres")

    try:
        with psycopg.connect(**admin_config, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                exists = cur.fetchone() is not None
                if not exists:
                    cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    except psycopg.OperationalError as exc:
        pytest.skip(
            "Unable to verify or create integration test database due to connectivity/auth issue. "
            f"Target database '{db_name}'. Details: {exc}"
        )
    except psycopg.Error as exc:
        pytest.fail(
            "Unable to verify or create integration test database. "
            f"Target database '{db_name}'. Error: {exc}"
        )


def bootstrap_newsfetcher_schema(config: dict[str, object]) -> None:
    schema_files = [
        (
            Path(__file__).resolve().parents[2]
            / "src"
            / "product_components"
            / "shared"
            / "db"
            / "schema.sql"
        ),
        (
            Path(__file__).resolve().parents[2]
            / "src"
            / "product_components"
            / "news_fetcher"
            / "db"
            / "schema.sql"
        ),
        (
            Path(__file__).resolve().parents[2]
            / "src"
            / "product_components"
            / "thesis_builder"
            / "db"
            / "schema.sql"
        ),
        (
            Path(__file__).resolve().parents[2]
            / "src"
            / "product_components"
            / "trade_executor"
            / "db"
            / "schema.sql"
        ),
    ]

    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            for schema_file in schema_files:
                sql_text = schema_file.read_text(encoding="utf-8")
                cur.execute(sql_text)
        conn.commit()


def bootstrap_newsfetcher_only_schema(config: dict[str, object]) -> None:
    schema_file = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "product_components"
        / "news_fetcher"
        / "db"
        / "schema.sql"
    )
    sql_text = schema_file.read_text(encoding="utf-8")
    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
        conn.commit()
