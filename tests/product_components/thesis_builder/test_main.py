from __future__ import annotations

import logging
from dataclasses import replace

from src.product_components.thesis_builder.main import _configure_logging
from src.product_components.thesis_builder.settings import ThesisBuilderSettings


def _settings() -> ThesisBuilderSettings:
    return ThesisBuilderSettings(
        thesis_builder_db_schema="thesis_builder",
        shared_db_schema="shared",
        news_fetcher_db_schema="news_fetcher",
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
        signal_queue="signal_queue",
        failed_messages_dlq="failed_messages_dlq",
        reprocess_command_queue="reprocess_command_queue",
        consumer_group="thesis_builder_group",
        consumer_name="thesis_builder_local",
        reprocess_max_articles=200,
        poll_interval_seconds=120,
        heartbeat_interval_seconds=60,
        batch_size=10,
        block_ms=5000,
        claim_min_idle_seconds=300,
        max_delivery_attempts=3,
        evidence_collection_max_minutes=120,
        max_evidence_age_minutes=180,
        required_evidence_count=3,
        min_confidence=0.6,
        min_relevance=0.5,
        contrarian_min_confidence=0.72,
        trend_follow_min_confidence=0.68,
        risk_max_loss_usd=120.0,
        default_time_horizon="swing_1d_5d",
        llm_model="gpt-4o-mini",
        llm_daily_token_budget=500000,
        llm_max_output_tokens=1200,
        triage_enabled=False,
        triage_model="gpt-4o-mini",
        triage_max_output_tokens=200,
        listicle_prefilter_enabled=False,
        listicle_prefilter_tag_threshold=6,
        already_priced_event_driven_atr_multiple=1.5,
        already_priced_event_driven_return_threshold=0.04,
        already_priced_sentiment_momentum_atr_multiple=2.0,
        already_priced_sentiment_momentum_return_threshold=0.06,
        llm_request_timeout_seconds=60.0,
        llm_max_retries=2,
        openai_api_key="test-key",
    )


def test_configure_logging_writes_to_configured_log_file(tmp_path) -> None:
    log_file = tmp_path / "logs" / "thesis-builder.log"
    settings = replace(_settings(), log_file=str(log_file), log_level="INFO")

    try:
        _configure_logging(settings, tmp_path)
        logging.getLogger("thesis_builder").info("thesis builder log file smoke test")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert "thesis builder log file smoke test" in log_file.read_text(encoding="utf-8")
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        logging.basicConfig(handlers=[], force=True)


def test_configure_logging_falls_back_when_log_file_is_locked(
    monkeypatch, capsys, tmp_path
) -> None:
    settings = replace(_settings(), log_file=str(tmp_path / "logs" / "thesis-builder.log"), log_level="INFO")

    def _raise_oserror(*_: object, **__: object) -> logging.Handler:
        raise OSError("locked")

    monkeypatch.setattr(logging, "FileHandler", _raise_oserror)

    try:
        _configure_logging(settings, tmp_path)
        captured = capsys.readouterr()
        assert "falling back to stderr-only logging" in captured.err
        assert len(logging.getLogger().handlers) == 1
        assert isinstance(logging.getLogger().handlers[0], logging.StreamHandler)
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        logging.basicConfig(handlers=[], force=True)
