from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import redis

from .settings import ThesisBuilderSettings

LOGGER = logging.getLogger("thesis_builder.service")


@dataclass(frozen=True)
class ThesisBuilderRuntimeStatus:
    news_raw_length: int | None
    pending_count: int | None
    signal_length: int | None
    processing_enabled: bool


class ThesisBuilderRunner:
    """Runnable ThesisBuilder process shell.

    The analysis pipeline is intentionally not implemented here yet. This runner
    validates infrastructure, creates the Redis consumer group, and keeps the
    process alive without reading or acknowledging queued news.
    """

    def __init__(self, *, settings: ThesisBuilderSettings) -> None:
        self._settings = settings
        self._redis = redis.from_url(settings.queue_url, decode_responses=True)

    def run_forever(self) -> None:
        self.bootstrap()
        LOGGER.info(
            "ThesisBuilder runtime started in safe idle mode; analysis pipeline is not implemented yet"
        )
        while True:
            try:
                status = self.status()
                LOGGER.info(
                    "ThesisBuilder heartbeat news_raw_length=%s pending_count=%s signal_length=%s processing_enabled=%s",
                    status.news_raw_length,
                    status.pending_count,
                    status.signal_length,
                    status.processing_enabled,
                )
            except Exception:
                LOGGER.exception("ThesisBuilder heartbeat failed")
            time.sleep(max(1, self._settings.heartbeat_interval_seconds))

    def bootstrap(self) -> None:
        self._redis.ping()
        self._ensure_stream(self._settings.news_raw_queue)
        self._ensure_stream(self._settings.signal_queue)
        self._ensure_stream(self._settings.failed_messages_dlq)
        self._ensure_consumer_group()

    def status(self) -> ThesisBuilderRuntimeStatus:
        return ThesisBuilderRuntimeStatus(
            news_raw_length=_stream_length(self._redis, self._settings.news_raw_queue),
            pending_count=_pending_count(
                self._redis,
                stream_name=self._settings.news_raw_queue,
                group_name=self._settings.consumer_group,
            ),
            signal_length=_stream_length(self._redis, self._settings.signal_queue),
            processing_enabled=False,
        )

    def _ensure_stream(self, stream_name: str) -> None:
        try:
            self._redis.xinfo_stream(stream_name)
        except redis.exceptions.ResponseError as exc:
            if "no such key" not in str(exc).lower():
                raise
            marker_id = self._redis.xadd(stream_name, {"bootstrap": "thesis_builder"})
            self._redis.xdel(stream_name, marker_id)

    def _ensure_consumer_group(self) -> None:
        try:
            self._redis.xgroup_create(
                name=self._settings.news_raw_queue,
                groupname=self._settings.consumer_group,
                id="0",
                mkstream=True,
            )
            LOGGER.info(
                "Created Redis consumer group stream=%s group=%s",
                self._settings.news_raw_queue,
                self._settings.consumer_group,
            )
        except redis.exceptions.ResponseError as exc:
            if "busygroup" not in str(exc).lower():
                raise


def _stream_length(client: redis.Redis, stream_name: str) -> int | None:
    try:
        return int(client.xlen(stream_name))
    except redis.exceptions.RedisError:
        LOGGER.exception("Failed to read Redis stream length stream=%s", stream_name)
        return None


def _pending_count(client: redis.Redis, *, stream_name: str, group_name: str) -> int | None:
    try:
        pending: Any = client.xpending(stream_name, group_name)
    except redis.exceptions.ResponseError as exc:
        if "nogroup" in str(exc).lower():
            return 0
        raise
    except redis.exceptions.RedisError:
        LOGGER.exception(
            "Failed to read Redis pending count stream=%s group=%s",
            stream_name,
            group_name,
        )
        return None

    if isinstance(pending, dict):
        return int(pending.get("pending", 0))
    if isinstance(pending, (list, tuple)) and pending:
        return int(pending[0])
    return 0
