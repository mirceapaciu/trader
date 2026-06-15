import redis

from src.product_components.thesis_builder.service import ThesisBuilderRunner
from src.product_components.thesis_builder.settings import ThesisBuilderSettings


class _FakeRedis:
    def __init__(self) -> None:
        self.created_groups: list[tuple[str, str, str, bool]] = []
        self.streams: set[str] = set()
        self.deleted: list[tuple[str, str]] = []

    def ping(self) -> bool:
        return True

    def xinfo_stream(self, stream_name: str):
        if stream_name not in self.streams:
            raise redis.exceptions.ResponseError("no such key")
        return {"length": 0}

    def xadd(self, stream_name: str, _payload: dict[str, str]) -> str:
        self.streams.add(stream_name)
        return "1-0"

    def xdel(self, stream_name: str, message_id: str) -> None:
        self.deleted.append((stream_name, message_id))

    def xgroup_create(self, *, name: str, groupname: str, id: str, mkstream: bool) -> None:
        self.created_groups.append((name, groupname, id, mkstream))

    def xlen(self, stream_name: str) -> int:
        return 0 if stream_name in self.streams else 0

    def xpending(self, _stream_name: str, _group_name: str) -> dict[str, int]:
        return {"pending": 0}


def test_thesis_builder_runner_bootstraps_streams_and_group(monkeypatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(
        "src.product_components.thesis_builder.service.redis.from_url",
        lambda *_args, **_kwargs: fake,
    )
    settings = _settings()

    runner = ThesisBuilderRunner(settings=settings)
    runner.bootstrap()

    assert fake.streams == {"news_raw_queue", "signal_queue", "failed_messages_dlq"}
    assert fake.created_groups == [("news_raw_queue", "thesis_builder_group", "0", True)]


def test_thesis_builder_runner_reports_safe_idle_status(monkeypatch) -> None:
    fake = _FakeRedis()
    fake.streams.update({"news_raw_queue", "signal_queue", "failed_messages_dlq"})
    monkeypatch.setattr(
        "src.product_components.thesis_builder.service.redis.from_url",
        lambda *_args, **_kwargs: fake,
    )

    status = ThesisBuilderRunner(settings=_settings()).status()

    assert status.news_raw_length == 0
    assert status.pending_count == 0
    assert status.signal_length == 0
    assert status.processing_enabled is False


def _settings() -> ThesisBuilderSettings:
    return ThesisBuilderSettings(
        thesis_builder_db_schema="thesis_builder",
        shared_db_schema="shared",
        news_fetcher_db_schema="news_fetcher",
        queue_url="redis://localhost:6379/0",
        news_raw_queue="news_raw_queue",
        signal_queue="signal_queue",
        failed_messages_dlq="failed_messages_dlq",
        consumer_group="thesis_builder_group",
        consumer_name="consumer",
        poll_interval_seconds=120,
        heartbeat_interval_seconds=60,
        evidence_collection_max_minutes=120,
        required_evidence_count=3,
        min_confidence=0.6,
        contrarian_min_confidence=0.72,
        trend_follow_min_confidence=0.68,
        risk_max_loss_usd=120.0,
    )
