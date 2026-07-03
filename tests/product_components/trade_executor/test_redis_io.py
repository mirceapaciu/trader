from src.product_components.trade_executor.redis_io import RedisTradeExecutorIo, _block_arg


class _RecordingClient:
    def __init__(self, *, claim_result=None) -> None:
        self.block_args: list[object] = []
        self.autoclaim_calls: list[dict] = []
        self._claim_result = claim_result or ("0-0", [], [])

    def xreadgroup(self, *, groupname, consumername, streams, count, block):
        if ">" in streams.values():
            self.block_args.append(block)
        return []

    def xautoclaim(self, *, name, groupname, consumername, min_idle_time, start_id, count):
        self.autoclaim_calls.append({"name": name, "min_idle_time": min_idle_time})
        return self._claim_result


def _build_io(client: _RecordingClient) -> RedisTradeExecutorIo:
    io = RedisTradeExecutorIo.__new__(RedisTradeExecutorIo)
    io._client = client
    io._signal_queue = "signal_queue"
    io._failed_messages_dlq = "failed_messages_dlq"
    io._consumer_group = "trade_executor_group"
    io._consumer_name = "consumer"
    io._claim_min_idle_ms = 60_000
    io._claim_cursor = "0-0"
    return io


def test_block_arg_treats_zero_as_non_blocking() -> None:
    assert _block_arg(0) is None
    assert _block_arg(5000) == 5000


def test_read_passes_positive_block_interval() -> None:
    client = _RecordingClient()
    _build_io(client).read(count=10, block_ms=5000)
    assert client.block_args == [5000]


def test_read_reads_from_signal_queue() -> None:
    client = _RecordingClient()
    _build_io(client).read(count=10, block_ms=5000)
    assert client.autoclaim_calls and client.autoclaim_calls[0]["name"] == "signal_queue"


def test_read_decodes_thesis_card_signal() -> None:
    fields = {
        "event_id": "evt_thesis_card_c1",
        "event_type": "thesis_card.created",
        "dedupe_key": "c1",
        "payload_json": '{"thesis_card_id":"c1","ticker":"AAPL"}',
    }
    client = _RecordingClient(claim_result=("0-0", [("1-0", fields)], []))
    io = _build_io(client)

    messages = io.read(count=10, block_ms=5000)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.event_type == "thesis_card.created"
    assert msg.is_thesis_card is True
    assert msg.payload["ticker"] == "AAPL"
    assert client.block_args == []  # claim hit -> new-read skipped


def test_read_drops_trimmed_entries() -> None:
    client = _RecordingClient(claim_result=("0-0", [("1-0", None)], []))
    assert _build_io(client).read(count=10, block_ms=0) == []
