from src.product_components.thesis_builder.redis_io import (
    RedisThesisBuilderIo,
    _block_arg,
)


class _RecordingClient:
    """Minimal redis stub that records the ``block`` arg of XREADGROUP reads."""

    def __init__(self) -> None:
        self.block_args: list[object] = []

    def xreadgroup(self, *, groupname, consumername, streams, count, block):
        # Only the new-message read (id ">") carries the caller's poll interval;
        # the pending read (id "0") always uses block=0 and is ignored by Redis.
        if ">" in streams.values():
            self.block_args.append(block)
        return []


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
