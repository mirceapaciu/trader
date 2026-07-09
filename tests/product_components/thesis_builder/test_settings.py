from src.product_components.thesis_builder.settings import ThesisBuilderSettings


def test_thesis_builder_settings_defaults(monkeypatch) -> None:
    monkeypatch.delenv("THESIS_BUILDER_DB_SCHEMA", raising=False)
    monkeypatch.delenv("THESIS_BUILDER_CONSUMER_GROUP", raising=False)
    monkeypatch.delenv("THESIS_CARD_REQUIRED_EVIDENCE_COUNT", raising=False)
    monkeypatch.delenv("THESIS_BUILDER_TRIAGE_ENABLED", raising=False)
    monkeypatch.delenv("THESIS_BUILDER_LISTICLE_PREFILTER_ENABLED", raising=False)

    settings = ThesisBuilderSettings.from_env()

    assert settings.thesis_builder_db_schema == "thesis_builder"
    assert settings.consumer_group == "thesis_builder_group"
    assert settings.news_raw_queue == "news_raw_queue"
    assert settings.signal_queue == "signal_queue"
    assert settings.required_evidence_count == 3
    assert settings.min_relevance == 0.5
    assert settings.batch_size == 10
    assert settings.block_ms == 5000
    assert settings.claim_min_idle_seconds == 300
    assert settings.max_delivery_attempts == 3
    assert settings.llm_max_output_tokens == 1200
    assert settings.triage_enabled is False
    assert settings.triage_model == "gpt-4o-mini"
    assert settings.triage_max_output_tokens == 200
    assert settings.listicle_prefilter_enabled is False
    assert settings.listicle_prefilter_tag_threshold == 6
    assert settings.llm_request_timeout_seconds == 60.0
    assert settings.llm_max_retries == 2


def test_thesis_builder_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("THESIS_BUILDER_DB_SCHEMA", "tb_test")
    monkeypatch.setenv("THESIS_BUILDER_CONSUMER_GROUP", "tb_group_test")
    monkeypatch.setenv("NEWS_RAW_QUEUE", "news_test")
    monkeypatch.setenv("SIGNAL_QUEUE", "signal_test")
    monkeypatch.setenv("THESIS_CARD_REQUIRED_EVIDENCE_COUNT", "4")
    monkeypatch.setenv("THESIS_BUILDER_MIN_CONFIDENCE", "0.7")
    monkeypatch.setenv("THESIS_BUILDER_BATCH_SIZE", "5")
    monkeypatch.setenv("THESIS_BUILDER_BLOCK_MS", "250")
    monkeypatch.setenv("THESIS_BUILDER_CLAIM_MIN_IDLE_SECONDS", "45")
    monkeypatch.setenv("THESIS_BUILDER_MAX_DELIVERY_ATTEMPTS", "7")
    monkeypatch.setenv("THESIS_BUILDER_LLM_MAX_OUTPUT_TOKENS", "900")
    monkeypatch.setenv("THESIS_BUILDER_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("THESIS_BUILDER_TRIAGE_MODEL", "gpt-4o-mini-triage")
    monkeypatch.setenv("THESIS_BUILDER_TRIAGE_MAX_OUTPUT_TOKENS", "80")
    monkeypatch.setenv("THESIS_BUILDER_LISTICLE_PREFILTER_ENABLED", "1")
    monkeypatch.setenv("THESIS_BUILDER_LISTICLE_PREFILTER_TAG_THRESHOLD", "8")
    monkeypatch.setenv("THESIS_BUILDER_LLM_REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("THESIS_BUILDER_LLM_MAX_RETRIES", "1")

    settings = ThesisBuilderSettings.from_env()

    assert settings.thesis_builder_db_schema == "tb_test"
    assert settings.consumer_group == "tb_group_test"
    assert settings.news_raw_queue == "news_test"
    assert settings.signal_queue == "signal_test"
    assert settings.required_evidence_count == 4
    assert settings.min_confidence == 0.7
    assert settings.batch_size == 5
    assert settings.block_ms == 250
    assert settings.claim_min_idle_seconds == 45
    assert settings.max_delivery_attempts == 7
    assert settings.llm_max_output_tokens == 900
    assert settings.triage_enabled is True
    assert settings.triage_model == "gpt-4o-mini-triage"
    assert settings.triage_max_output_tokens == 80
    assert settings.listicle_prefilter_enabled is True
    assert settings.listicle_prefilter_tag_threshold == 8
    assert settings.llm_request_timeout_seconds == 30.0
    assert settings.llm_max_retries == 1
