from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


@dataclass(frozen=True)
class ThesisBuilderSettings:
    """Environment-backed ThesisBuilder runtime settings."""

    thesis_builder_db_schema: str
    shared_db_schema: str
    news_fetcher_db_schema: str

    queue_url: str
    news_raw_queue: str
    signal_queue: str
    failed_messages_dlq: str
    reprocess_command_queue: str
    consumer_group: str
    consumer_name: str
    reprocess_max_articles: int

    poll_interval_seconds: int
    heartbeat_interval_seconds: int
    batch_size: int
    block_ms: int
    max_delivery_attempts: int
    evidence_collection_max_minutes: int
    max_evidence_age_minutes: int
    required_evidence_count: int
    min_confidence: float
    min_relevance: float
    contrarian_min_confidence: float
    trend_follow_min_confidence: float
    risk_max_loss_usd: float
    default_time_horizon: str
    llm_model: str
    llm_daily_token_budget: int
    llm_max_output_tokens: int
    llm_request_timeout_seconds: float
    llm_max_retries: int
    openai_api_key: str

    log_level: str = "INFO"
    log_file: str = "logs/thesis-builder.log"

    @property
    def postgres_dsn(self) -> str:
        host = os.getenv("POSTGRES_HOST", "127.0.0.1")
        port = os.getenv("POSTGRES_PORT", "5432")
        database = os.getenv("POSTGRES_DATABASE", "trader")
        user = os.getenv("POSTGRES_USER", "trader")
        password = os.getenv("POSTGRES_PASSWORD", "")
        sslmode = os.getenv("POSTGRES_SSLMODE", "disable")
        return (
            f"host={host} port={port} dbname={database} user={user} "
            f"password={password} sslmode={sslmode}"
        )

    @classmethod
    def from_env(cls) -> "ThesisBuilderSettings":
        return cls(
            thesis_builder_db_schema=os.getenv("THESIS_BUILDER_DB_SCHEMA", "thesis_builder"),
            shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
            news_fetcher_db_schema=os.getenv("NEWSFETCHER_DB_SCHEMA", "news_fetcher"),
            queue_url=_queue_url_from_env(),
            news_raw_queue=os.getenv("NEWS_RAW_QUEUE", "news_raw_queue"),
            signal_queue=os.getenv("SIGNAL_QUEUE", "signal_queue"),
            failed_messages_dlq=os.getenv("FAILED_MESSAGES_DLQ", "failed_messages_dlq"),
            reprocess_command_queue=os.getenv("REPROCESS_COMMAND_QUEUE", "reprocess_command_queue"),
            consumer_group=os.getenv("THESIS_BUILDER_CONSUMER_GROUP", "thesis_builder_group"),
            consumer_name=os.getenv("THESIS_BUILDER_CONSUMER_NAME", _default_consumer_name()),
            reprocess_max_articles=_int_env("THESIS_BUILDER_REPROCESS_MAX_ARTICLES", 200),
            poll_interval_seconds=_int_env("THESIS_BUILDER_POLL_INTERVAL_SECONDS", _int_env("PIPELINE_INTERVAL", 120)),
            heartbeat_interval_seconds=_int_env("THESIS_BUILDER_HEARTBEAT_INTERVAL_SECONDS", 60),
            batch_size=_int_env("THESIS_BUILDER_BATCH_SIZE", 10),
            block_ms=_int_env("THESIS_BUILDER_BLOCK_MS", 5000),
            max_delivery_attempts=_int_env("THESIS_BUILDER_MAX_DELIVERY_ATTEMPTS", 3),
            evidence_collection_max_minutes=_int_env("THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES", 120),
            max_evidence_age_minutes=_int_env("THESIS_CARD_MAX_EVIDENCE_AGE_MINUTES", 180),
            required_evidence_count=_int_env("THESIS_CARD_REQUIRED_EVIDENCE_COUNT", 3),
            min_confidence=_float_env("THESIS_BUILDER_MIN_CONFIDENCE", 0.6),
            min_relevance=_float_env("THESIS_BUILDER_MIN_RELEVANCE", 0.5),
            contrarian_min_confidence=_float_env("THESIS_BUILDER_CONTRARIAN_MIN_CONFIDENCE", 0.72),
            trend_follow_min_confidence=_float_env("THESIS_BUILDER_TREND_FOLLOW_MIN_CONFIDENCE", 0.68),
            risk_max_loss_usd=_float_env("THESIS_BUILDER_RISK_MAX_LOSS_USD", 120.0),
            default_time_horizon=os.getenv("THESIS_BUILDER_DEFAULT_TIME_HORIZON", "swing_1d_5d"),
            llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            llm_daily_token_budget=_int_env("LLM_DAILY_TOKEN_BUDGET", 500000),
            llm_max_output_tokens=_int_env("THESIS_BUILDER_LLM_MAX_OUTPUT_TOKENS", 1200),
            llm_request_timeout_seconds=_float_env("THESIS_BUILDER_LLM_REQUEST_TIMEOUT_SECONDS", 60.0),
            llm_max_retries=_int_env("THESIS_BUILDER_LLM_MAX_RETRIES", 2),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            log_level=os.getenv("THESIS_BUILDER_LOG_LEVEL") or os.getenv("LOG_LEVEL", "INFO"),
            log_file=os.getenv("THESIS_BUILDER_LOG_FILE", "logs/thesis-builder.log"),
        )

    def log_file_path(self, repo_root: Path) -> Path | None:
        return _optional_path(self.log_file, repo_root)


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return int(value)


def _float_env(key: str, default: float) -> float:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return float(value)


def _optional_path(value: str, repo_root: Path) -> Path | None:
    if not value.strip():
        return None
    path = Path(value.strip())
    if not path.is_absolute():
        path = repo_root / path
    return path


def _default_consumer_name() -> str:
    hostname = os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "local"
    return f"thesis_builder_{hostname}_{os.getpid()}"


def _queue_url_from_env() -> str:
    queue_url = os.getenv("QUEUE_URL", "redis://127.0.0.1:6379/0").strip()
    redis_password = (os.getenv("REDIS_PASSWORD") or "").strip()
    if not redis_password:
        return queue_url

    parts = urlsplit(queue_url)
    if parts.scheme not in {"redis", "rediss"}:
        return queue_url
    if "@" in parts.netloc:
        return queue_url

    host_port = parts.netloc
    return urlunsplit(
        (
            parts.scheme,
            f":{quote(redis_password, safe='')}@{host_port}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )
