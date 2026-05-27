from __future__ import annotations

import os
import time

import pytest
import redis

from tests.integration._redis_test_helper import ensure_safe_test_redis, redis_config


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _enforce_redis_test_isolation() -> None:
    ensure_safe_test_redis(redis_config())


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=(os.getenv("REDIS_PASSWORD") or None),
        socket_connect_timeout=5,
        socket_timeout=5,
        decode_responses=True,
    )


def _required_stream_names() -> list[str]:
    return [
        os.getenv("REDIS_STREAM_NEWS_RAW", "news_raw_queue"),
        os.getenv("REDIS_STREAM_SIGNAL", "signal_queue"),
        os.getenv("REDIS_STREAM_DLQ", "failed_messages_dlq"),
    ]


def _wait_until_ready(client: redis.Redis, timeout_seconds: float = 15.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None

    while time.time() < deadline:
        try:
            if client.ping() is True:
                return
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            last_error = exc
        time.sleep(0.5)

    if isinstance(last_error, redis.exceptions.ConnectionError):
        pytest.skip(
            "Redis is running but not reachable from test process. "
            "Set REDIS_HOST/REDIS_PORT/REDIS_PASSWORD for host access."
        )

    pytest.fail(f"Redis not ready within {timeout_seconds:.1f}s: {last_error}")


def test_redis_ping() -> None:
    client = _redis_client()
    _wait_until_ready(client)

    assert client.ping() is True


def test_required_streams_accept_events() -> None:
    client = _redis_client()
    _wait_until_ready(client)
    payload = {
        "source": "integration_test",
        "created_at": str(time.time()),
    }

    for stream_name in _required_stream_names():
        entry_id = client.xadd(stream_name, payload)
        assert isinstance(entry_id, str)

        stream_info = client.xinfo_stream(stream_name)
        assert stream_info["length"] >= 1
