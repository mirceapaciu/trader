from __future__ import annotations

import json
from typing import Any

import redis

from src.core_components.event_ingestion_engine.errors import (
    NonTransientPublishError,
    TransientPublishError,
)
from src.core_components.event_ingestion_engine.interfaces import EventPublisher


class RedisStreamPublisher(EventPublisher):
    """Redis Streams publisher implementation for news_raw_queue events."""

    def __init__(self, *, queue_url: str, stream_name: str) -> None:
        self._stream_name = stream_name
        self._client = redis.from_url(queue_url, decode_responses=True)

    def publish(self, envelope: dict[str, Any]) -> None:
        payload = {
            "event_id": str(envelope.get("event_id", "")),
            "event_type": str(envelope.get("event_type", "")),
            "event_version": str(envelope.get("event_version", "1")),
            "occurred_at": str(envelope.get("occurred_at", "")),
            "producer": str(envelope.get("producer", "news_fetcher")),
            "dedupe_key": str(envelope.get("dedupe_key", "")),
            "payload_json": _serialize_payload(envelope.get("payload")),
        }
        try:
            self._client.xadd(self._stream_name, payload)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise TransientPublishError(str(exc)) from exc
        except redis.exceptions.RedisError as exc:
            raise NonTransientPublishError(str(exc)) from exc


def _serialize_payload(payload: Any) -> str:
    if payload is None:
        return "{}"
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
