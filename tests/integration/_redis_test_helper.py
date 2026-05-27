from __future__ import annotations

import os

import pytest


def redis_config() -> dict[str, object]:
    return {
        "host": os.getenv("REDIS_HOST", "127.0.0.1"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "db": int(os.getenv("REDIS_DB", "0")),
        "prod_db": int(os.getenv("REDIS_PROD_DB", "0")),
        "news_raw_stream": os.getenv("REDIS_STREAM_NEWS_RAW", "news_raw_queue"),
        "signal_stream": os.getenv("REDIS_STREAM_SIGNAL", "signal_queue"),
        "dlq_stream": os.getenv("REDIS_STREAM_DLQ", "failed_messages_dlq"),
    }


def ensure_safe_test_redis(config: dict[str, object]) -> None:
    test_db = int(config["db"])
    prod_db = int(config["prod_db"])

    if test_db == prod_db:
        pytest.fail(
            "Integration tests must use a Redis DB index different from production. "
            f"Current REDIS_DB={test_db} and REDIS_PROD_DB={prod_db}."
        )

    stream_names = [
        str(config["news_raw_stream"]),
        str(config["signal_stream"]),
        str(config["dlq_stream"]),
    ]
    if any(name.endswith("_test") for name in stream_names):
        return

    # Separate DB is already sufficient isolation; warn only through strict failure when DB is not separated.
    # No additional action needed when stream names are shared but DB is isolated.
