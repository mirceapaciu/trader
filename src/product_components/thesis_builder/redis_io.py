from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import redis

from src.core_components.event_ingestion_engine.errors import NonTransientPublishError, TransientPublishError
from .models import NewsArticle


@dataclass(frozen=True)
class ReprocessCommandMessage:
    message_id: str
    run_id: str
    days_back: int


@dataclass(frozen=True)
class NewsStreamMessage:
    message_id: str
    event_id: str
    event_type: str
    dedupe_key: str
    payload: dict[str, Any]
    raw_fields: dict[str, str]

    @property
    def article_id(self) -> str:
        payload_id = self.payload.get("id") or self.payload.get("article_id")
        if payload_id:
            return str(payload_id)
        if self.dedupe_key:
            return self.dedupe_key
        return self.event_id

    def as_article(self) -> NewsArticle | None:
        payload_id = self.payload.get("id") or self.payload.get("article_id")
        source = self.payload.get("source")
        headline = self.payload.get("headline") or self.payload.get("title")
        url = self.payload.get("url") or self.payload.get("canonical_locator")
        published_at = _parse_datetime(
            self.payload.get("published_at") or self.payload.get("occurred_at")
        )
        attributes = self.payload.get("attributes") or {}
        fetched_at = _parse_datetime(
            self.payload.get("fetched_at")
            or self.payload.get("ingested_at")
            or attributes.get("fetched_at")
        )
        tickers = (
            self.payload.get("tickers")
            or self.payload.get("entities")
            or attributes.get("tickers")
            or []
        )
        if not (
            isinstance(payload_id, str)
            and isinstance(source, str)
            and isinstance(headline, str)
            and isinstance(url, str)
            and isinstance(tickers, list)
            and published_at is not None
            and fetched_at is not None
        ):
            return None
        sentiment_source = self.payload.get("sentiment_source") or attributes.get("sentiment_source")
        return NewsArticle(
            id=payload_id,
            source=source,
            headline=headline,
            summary=str(self.payload.get("summary")) if self.payload.get("summary") is not None else None,
            url=url,
            tickers=[str(item).strip().upper() for item in tickers if str(item).strip()],
            published_at=published_at,
            fetched_at=fetched_at,
            sentiment_source=_parse_float(sentiment_source),
        )


class RedisThesisBuilderIo:
    def __init__(
        self,
        *,
        queue_url: str,
        news_raw_queue: str,
        signal_queue: str,
        failed_messages_dlq: str,
        consumer_group: str,
        consumer_name: str,
        reprocess_command_queue: str | None = None,
        claim_min_idle_ms: int = 300_000,
    ) -> None:
        self._client = redis.from_url(queue_url, decode_responses=True)
        self._news_raw_queue = news_raw_queue
        self._signal_queue = signal_queue
        self._failed_messages_dlq = failed_messages_dlq
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name
        self._reprocess_command_queue = reprocess_command_queue
        self._reprocess_group = f"{consumer_group}_reprocess"
        self._claim_min_idle_ms = claim_min_idle_ms
        self._claim_cursor = "0-0"

    def ping(self) -> bool:
        return bool(self._client.ping())

    def ensure_streams_and_group(self) -> None:
        for stream_name in (self._news_raw_queue, self._signal_queue, self._failed_messages_dlq):
            self._ensure_stream(stream_name)
        self._ensure_group(self._news_raw_queue, self._consumer_group)
        if self._reprocess_command_queue:
            self._ensure_stream(self._reprocess_command_queue)
            self._ensure_group(self._reprocess_command_queue, self._reprocess_group)

    def _ensure_group(self, stream_name: str, group_name: str) -> None:
        try:
            self._client.xgroup_create(
                name=stream_name,
                groupname=group_name,
                id="0",
                mkstream=True,
            )
        except redis.exceptions.ResponseError as exc:
            if "busygroup" not in str(exc).lower():
                raise

    def read_reprocess_commands(self, *, count: int, block_ms: int) -> list[ReprocessCommandMessage]:
        if not self._reprocess_command_queue:
            return []
        pending = self._client.xreadgroup(
            groupname=self._reprocess_group,
            consumername=self._consumer_name,
            streams={self._reprocess_command_queue: "0"},
            count=count,
            block=0,
        )
        commands = _reprocess_commands(pending)
        if commands:
            return commands
        response = self._client.xreadgroup(
            groupname=self._reprocess_group,
            consumername=self._consumer_name,
            streams={self._reprocess_command_queue: ">"},
            count=count,
            block=_block_arg(block_ms),
        )
        return _reprocess_commands(response)

    def ack_reprocess(self, message_id: str) -> None:
        if not self._reprocess_command_queue:
            return
        self._client.xack(self._reprocess_command_queue, self._reprocess_group, message_id)

    def read(self, *, count: int, block_ms: int) -> list[NewsStreamMessage]:
        pending_response = self._client.xreadgroup(
            groupname=self._consumer_group,
            consumername=self._consumer_name,
            streams={self._news_raw_queue: "0"},
            count=count,
            block=0,
        )
        pending_messages = _messages(pending_response)
        if pending_messages:
            return pending_messages
        claimed = self._claim_stale(count=count)
        if claimed:
            return claimed
        response = self._client.xreadgroup(
            groupname=self._consumer_group,
            consumername=self._consumer_name,
            streams={self._news_raw_queue: ">"},
            count=count,
            block=_block_arg(block_ms),
        )
        return _messages(response)

    def _claim_stale(self, *, count: int) -> list[NewsStreamMessage]:
        """Reclaim pending entries left behind by dead consumers.

        ``read``'s id="0" read only sees *this* consumer's pending entries, so a
        message delivered to a consumer that then crashed (or, before the stable
        consumer name, a previous PID-based name) would stay pending forever and
        keep ``pending_count`` pinned, tripping the stall alarm. XAUTOCLAIM
        transfers entries idle longer than ``claim_min_idle_ms`` from any consumer
        to us so the normal processing/ack path can drain them. Entries already
        trimmed out of the stream are dropped from the PEL by XAUTOCLAIM itself.
        """
        if self._claim_min_idle_ms <= 0:
            return []
        try:
            result = self._client.xautoclaim(
                name=self._news_raw_queue,
                groupname=self._consumer_group,
                consumername=self._consumer_name,
                min_idle_time=self._claim_min_idle_ms,
                start_id=self._claim_cursor,
                count=count,
            )
        except redis.exceptions.ResponseError as exc:
            if "nogroup" in str(exc).lower():
                return []
            raise
        # redis-py 7 returns (next_cursor, claimed_entries, deleted_ids); older
        # builds omit the deleted list. A "0-0" cursor means the scan wrapped.
        next_cursor = result[0] if result else "0-0"
        entries = result[1] if len(result) > 1 else []
        self._claim_cursor = next_cursor or "0-0"
        return _messages([(self._news_raw_queue, entries)])

    def ack(self, message_id: str) -> None:
        self._client.xack(self._news_raw_queue, self._consumer_group, message_id)

    def delivery_count(self, message_id: str) -> int:
        try:
            entries = self._client.xpending_range(
                self._news_raw_queue,
                self._consumer_group,
                min=message_id,
                max=message_id,
                count=1,
            )
        except redis.exceptions.ResponseError:
            return 1
        if not entries:
            return 1
        entry = entries[0]
        if isinstance(entry, dict):
            return int(entry.get("times_delivered") or entry.get("delivery_count") or 1)
        if isinstance(entry, (tuple, list)) and len(entry) >= 4:
            return int(entry[3])
        return 1

    def publish_signal(self, envelope: dict[str, Any]) -> None:
        self._publish(self._signal_queue, envelope)

    def publish_dlq(self, *, message: NewsStreamMessage, error_code: str) -> None:
        self._client.xadd(
            self._failed_messages_dlq,
            {
                "source_stream": self._news_raw_queue,
                "source_message_id": message.message_id,
                "event_id": message.event_id,
                "event_type": message.event_type,
                "dedupe_key": message.dedupe_key,
                "error_code": error_code,
                "payload_json": json.dumps(message.payload, separators=(",", ":"), sort_keys=True),
            },
        )

    def stream_lengths(self) -> tuple[int | None, int | None]:
        return _stream_length(self._client, self._news_raw_queue), _stream_length(self._client, self._signal_queue)

    def pending_count(self) -> int | None:
        try:
            pending = self._client.xpending(self._news_raw_queue, self._consumer_group)
        except redis.exceptions.ResponseError as exc:
            if "nogroup" in str(exc).lower():
                return 0
            raise
        if isinstance(pending, dict):
            return int(pending.get("pending", 0))
        if isinstance(pending, (tuple, list)) and pending:
            return int(pending[0])
        return 0

    def _publish(self, stream_name: str, envelope: dict[str, Any]) -> None:
        payload = {
            "event_id": str(envelope.get("event_id", "")),
            "event_type": str(envelope.get("event_type", "")),
            "event_version": str(envelope.get("event_version", "1.0")),
            "occurred_at": str(envelope.get("occurred_at", "")),
            "producer": str(envelope.get("producer", "thesis_builder")),
            "dedupe_key": str(envelope.get("dedupe_key", "")),
            "payload_json": json.dumps(envelope.get("payload") or {}, separators=(",", ":"), sort_keys=True),
        }
        try:
            self._client.xadd(stream_name, payload)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise TransientPublishError(str(exc)) from exc
        except redis.exceptions.RedisError as exc:
            raise NonTransientPublishError(str(exc)) from exc

    def _ensure_stream(self, stream_name: str) -> None:
        try:
            self._client.xinfo_stream(stream_name)
        except redis.exceptions.ResponseError as exc:
            if "no such key" not in str(exc).lower():
                raise
            marker_id = self._client.xadd(stream_name, {"bootstrap": "thesis_builder"})
            self._client.xdel(stream_name, marker_id)


def _block_arg(block_ms: int) -> int | None:
    """Translate a poll interval into a redis XREADGROUP ``block`` argument.

    Redis treats ``BLOCK 0`` as "block forever", which is the opposite of the
    non-blocking poll the callers intend when they pass ``block_ms=0``. Returning
    ``None`` omits the BLOCK clause entirely so the read returns immediately.
    """
    return block_ms if block_ms > 0 else None


def _message(message_id: str, fields: dict[str, str]) -> NewsStreamMessage:
    payload_json = fields.get("payload_json") or "{}"
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return NewsStreamMessage(
        message_id=message_id,
        event_id=str(fields.get("event_id", "")),
        event_type=str(fields.get("event_type", "")),
        dedupe_key=str(fields.get("dedupe_key", "")),
        payload=payload,
        raw_fields=dict(fields),
    )


def _messages(response) -> list[NewsStreamMessage]:
    messages: list[NewsStreamMessage] = []
    for _stream_name, entries in response:
        for message_id, fields in entries:
            # XAUTOCLAIM may surface entries whose stream data was trimmed away;
            # redis-py reports these with no fields. They carry no article, so
            # skip them here and let the PEL cleanup handled by XAUTOCLAIM stand.
            if not fields:
                continue
            messages.append(_message(message_id, fields))
    return messages


def _reprocess_commands(response) -> list[ReprocessCommandMessage]:
    commands: list[ReprocessCommandMessage] = []
    for _stream_name, entries in response:
        for message_id, fields in entries:
            run_id = str(fields.get("run_id", "")).strip()
            if not run_id:
                continue
            try:
                days_back = int(fields.get("days_back", "0"))
            except (TypeError, ValueError):
                days_back = 0
            commands.append(
                ReprocessCommandMessage(
                    message_id=message_id,
                    run_id=run_id,
                    days_back=days_back,
                )
            )
    return commands


def _stream_length(client: redis.Redis, stream_name: str) -> int | None:
    try:
        return int(client.xlen(stream_name))
    except redis.exceptions.RedisError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
