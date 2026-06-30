from src.product_components.thesis_builder.redis_io import (
    RedisThesisBuilderIo,
    _block_arg,
)


class _RecordingClient:
    """Minimal redis stub that records the ``block`` arg of XREADGROUP reads."""

    def __init__(self, *, claim_result=None) -> None:
        self.block_args: list[object] = []
        self.autoclaim_calls: list[dict] = []
        self._claim_result = claim_result or ("0-0", [], [])

    def xreadgroup(self, *, groupname, consumername, streams, count, block):
        # Only the new-message read (id ">") carries the caller's poll interval;
        # the pending read (id "0") always uses block=0 and is ignored by Redis.
        if ">" in streams.values():
            self.block_args.append(block)
        return []

    def xautoclaim(self, *, name, groupname, consumername, min_idle_time, start_id, count):
        self.autoclaim_calls.append(
            {
                "name": name,
                "min_idle_time": min_idle_time,
                "start_id": start_id,
                "count": count,
            }
        )
        return self._claim_result


def _build_io(client: _RecordingClient) -> RedisThesisBuilderIo:
    io = RedisThesisBuilderIo.__new__(RedisThesisBuilderIo)
    io._client = client
    io._news_raw_queue = "news_raw_queue"
    io._signal_queue = "signal_queue"
    io._failed_messages_dlq = "failed_messages_dlq"
    io._consumer_group = "thesis_builder_group"
    io._consumer_name = "consumer"
    io._reprocess_command_queue = "reprocess_command_queue"
    io._reprocess_group = "thesis_builder_group_reprocess"
    io._claim_min_idle_ms = 300_000
    io._claim_cursor = "0-0"
    return io


def test_block_arg_treats_zero_as_non_blocking() -> None:
    # Redis BLOCK 0 means "block forever"; callers passing 0 want a no-wait poll.
    assert _block_arg(0) is None
    assert _block_arg(-1) is None
    assert _block_arg(5000) == 5000


def test_read_reprocess_commands_does_not_block_forever() -> None:
    client = _RecordingClient()
    io = _build_io(client)

    io.read_reprocess_commands(count=1, block_ms=0)

    assert client.block_args == [None]


def test_read_passes_through_positive_block_interval() -> None:
    client = _RecordingClient()
    io = _build_io(client)

    io.read(count=10, block_ms=5000)

    assert client.block_args == [5000]


def test_read_reclaims_orphaned_pending_before_new_messages() -> None:
    # An entry left pending by a dead consumer is surfaced via XAUTOCLAIM and
    # returned so the normal processing/ack path can drain it. Because the claim
    # produced work, the blocking new-message read must be skipped this cycle.
    fields = {"event_id": "evt-1", "event_type": "news", "dedupe_key": "dk-1", "payload_json": "{}"}
    client = _RecordingClient(claim_result=("0-0", [("1700000000000-0", fields)], []))
    io = _build_io(client)

    messages = io.read(count=10, block_ms=5000)

    assert [m.message_id for m in messages] == ["1700000000000-0"]
    assert client.autoclaim_calls and client.autoclaim_calls[0]["min_idle_time"] == 300_000
    assert client.block_args == []  # new-message read skipped because a claim hit


def test_read_skips_claim_when_disabled() -> None:
    client = _RecordingClient()
    io = _build_io(client)
    io._claim_min_idle_ms = 0

    io.read(count=10, block_ms=5000)

    assert client.autoclaim_calls == []
    assert client.block_args == [5000]


def test_claim_advances_cursor_for_next_scan() -> None:
    client = _RecordingClient(claim_result=("1700000000005-0", [], []))
    io = _build_io(client)

    io.read(count=10, block_ms=0)

    assert io._claim_cursor == "1700000000005-0"


def test_claim_drops_trimmed_entries_without_fields() -> None:
    # XAUTOCLAIM can surface an entry whose stream data was trimmed; redis-py
    # reports it with empty fields. It carries no article, so it is filtered out.
    client = _RecordingClient(claim_result=("0-0", [("1700000000000-0", None)], []))
    io = _build_io(client)

    messages = io.read(count=10, block_ms=0)

    assert messages == []
