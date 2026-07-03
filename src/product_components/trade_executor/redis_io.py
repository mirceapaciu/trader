from __future__ import annotations

import json

import redis

from .models import SignalMessage


class RedisTradeExecutorIo:
    """Consumer-group reader for the signal_queue Redis stream.

    Mirrors ThesisBuilder's stream reader (pending -> XAUTOCLAIM -> new), but the
    input stream is ``signal_queue`` and there is no downstream publish path —
    TradeExecutor is a terminal sink, so only ``publish_dlq`` writes anything.
    """

    def __init__(
        self,
        *,
        queue_url: str,
        signal_queue: str,
        failed_messages_dlq: str,
        consumer_group: str,
        consumer_name: str,
        claim_min_idle_ms: int = 60_000,
    ) -> None:
        self._client = redis.from_url(queue_url, decode_responses=True)
        self._signal_queue = signal_queue
        self._failed_messages_dlq = failed_messages_dlq
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name
        self._claim_min_idle_ms = claim_min_idle_ms
        self._claim_cursor = "0-0"

    def ping(self) -> bool:
        return bool(self._client.ping())

    def ensure_streams_and_group(self) -> None:
        for stream_name in (self._signal_queue, self._failed_messages_dlq):
            self._ensure_stream(stream_name)
        self._ensure_group(self._signal_queue, self._consumer_group)

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

    def read(self, *, count: int, block_ms: int) -> list[SignalMessage]:
        pending_response = self._client.xreadgroup(
            groupname=self._consumer_group,
            consumername=self._consumer_name,
            streams={self._signal_queue: "0"},
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
            streams={self._signal_queue: ">"},
            count=count,
            block=_block_arg(block_ms),
        )
        return _messages(response)

    def _claim_stale(self, *, count: int) -> list[SignalMessage]:
        if self._claim_min_idle_ms <= 0:
            return []
        try:
            result = self._client.xautoclaim(
                name=self._signal_queue,
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
        next_cursor = result[0] if result else "0-0"
        entries = result[1] if len(result) > 1 else []
        self._claim_cursor = next_cursor or "0-0"
        return _messages([(self._signal_queue, entries)])

    def ack(self, message_id: str) -> None:
        self._client.xack(self._signal_queue, self._consumer_group, message_id)

    def delivery_count(self, message_id: str) -> int:
        try:
            entries = self._client.xpending_range(
                self._signal_queue,
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

    def publish_dlq(self, *, message: SignalMessage, error_code: str) -> None:
        self._client.xadd(
            self._failed_messages_dlq,
            {
                "source_stream": self._signal_queue,
                "source_message_id": message.message_id,
                "event_id": message.event_id,
                "event_type": message.event_type,
                "dedupe_key": message.dedupe_key,
                "error_code": error_code,
                "payload_json": json.dumps(message.payload, separators=(",", ":"), sort_keys=True),
            },
        )

    def stream_length(self) -> int | None:
        try:
            return int(self._client.xlen(self._signal_queue))
        except redis.exceptions.RedisError:
            return None

    def pending_count(self) -> int | None:
        try:
            pending = self._client.xpending(self._signal_queue, self._consumer_group)
        except redis.exceptions.ResponseError as exc:
            if "nogroup" in str(exc).lower():
                return 0
            raise
        if isinstance(pending, dict):
            return int(pending.get("pending", 0))
        if isinstance(pending, (tuple, list)) and pending:
            return int(pending[0])
        return 0

    def _ensure_stream(self, stream_name: str) -> None:
        try:
            self._client.xinfo_stream(stream_name)
        except redis.exceptions.ResponseError as exc:
            if "no such key" not in str(exc).lower():
                raise
            marker_id = self._client.xadd(stream_name, {"bootstrap": "trade_executor"})
            self._client.xdel(stream_name, marker_id)


def _block_arg(block_ms: int) -> int | None:
    return block_ms if block_ms > 0 else None


def _message(message_id: str, fields: dict[str, str]) -> SignalMessage:
    payload_json = fields.get("payload_json") or "{}"
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return SignalMessage(
        message_id=message_id,
        event_id=str(fields.get("event_id", "")),
        event_type=str(fields.get("event_type", "")),
        dedupe_key=str(fields.get("dedupe_key", "")),
        payload=payload,
        raw_fields=dict(fields),
    )


def _messages(response) -> list[SignalMessage]:
    messages: list[SignalMessage] = []
    for _stream_name, entries in response:
        for message_id, fields in entries:
            if not fields:
                continue
            messages.append(_message(message_id, fields))
    return messages
