from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HealthState = Literal["healthy", "unhealthy"]
DependencyKind = Literal["postgres", "redis"]
ThroughputGranularity = Literal["raw", "hour", "day"]


class DependencyHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    kind: DependencyKind
    state: HealthState
    message: str | None = None
    checked_at: datetime


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    readiness: HealthState
    liveness: HealthState
    stale_data: bool
    last_successful_refresh_at: datetime
    dependencies: list[DependencyHealth]
    active_incident_count: int


class ProviderStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_key: str
    last_cycle_start_at: datetime | None = None
    last_cycle_end_at: datetime | None = None
    last_cycle_duration_seconds: float | None = None
    last_fetch_attempt_at: datetime | None = None
    last_non_zero_fetch_at: datetime | None = None
    fetch_count: int = 0
    fetch_error_count: int = 0
    dedupe_drop_count: int = 0
    persist_success_count: int = 0
    publish_success_count: int = 0
    last_error_code: str | None = None
    last_error_at: datetime | None = None


class ProvidersResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    providers: list[ProviderStatus]
    generated_at: datetime


class ThroughputBucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_start: datetime
    source_key: str
    fetch_count: int
    publish_success_count: int
    publish_error_count: int


class ThroughputResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    window: str
    granularity: ThroughputGranularity
    window_start_at: datetime
    window_end_at: datetime
    buckets: list[ThroughputBucket]
    generated_at: datetime


class ThesisBuilderEvidenceWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_id: int
    ticker: str
    exchange_code: str
    strategy: str
    direction: str | None = None
    window_started_at: datetime
    last_evidence_at: datetime
    pending_age_seconds: float
    expires_in_seconds: float
    evidence_count: int
    required_evidence_count: int


class ThesisBuilderActionableCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    card_id: str
    ticker: str
    exchange_code: str
    strategy: str
    direction: str
    time_horizon: str
    confidence: float
    created_at: datetime
    expires_at: datetime
    expires_in_seconds: float
    evidence_count: int
    signal_published: bool


class ThesisCardSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    card_id: str
    ticker: str
    exchange_code: str
    strategy: str
    direction: str
    time_horizon: str
    confidence: float
    created_at: datetime
    expires_at: datetime
    expires_in_seconds: float
    evidence_count: int
    signal_published: bool
    validation_status: str
    rejection_reason_code: str | None = None


class ThesisCardListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    window: str
    window_start_at: datetime
    window_end_at: datetime
    cards: list[ThesisCardSummary] = Field(default_factory=list)
    generated_at: datetime


class WindowArticle(BaseModel):
    model_config = ConfigDict(frozen=True)

    article_id: str
    headline: str
    url: str
    source: str
    summary: str | None = None
    published_at: datetime | None = None
    confidence: float | None = None
    is_market_moving: bool = False
    validation_status: str | None = None
    rejection_reason_code: str | None = None


class WindowArticlesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    window_id: int | None = None
    card_id: str | None = None
    articles: list[WindowArticle] = Field(default_factory=list)
    generated_at: datetime


class FetchedArticle(BaseModel):
    model_config = ConfigDict(frozen=True)

    article_id: str
    source: str
    source_key: str
    headline: str
    summary: str | None = None
    url: str
    tickers: list[str] = Field(default_factory=list)
    published_at: datetime
    fetched_at: datetime
    filter_outcome: str | None = None        # 'accepted' | 'rejected' | None (not yet filtered)
    rejection_reason_code: str | None = None
    matched_article_id: str | None = None    # set when rejected as a duplicate


class FetchedArticlesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    window: str | None = None
    items: list[FetchedArticle] = Field(default_factory=list)
    generated_at: datetime


class NewsAnalysisItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    article_id: str
    ticker: str
    exchange_code: str
    analyzed_at: datetime
    content_type: str | None = None
    validation_status: str
    rejection_reason_code: str | None = None
    confidence: float
    is_market_moving: bool
    direction: str | None = None
    strategy: str | None = None
    reasoning: str | None = None
    headline: str | None = None
    summary: str | None = None
    url: str | None = None
    source: str | None = None


class NewsAnalysesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    items: list[NewsAnalysisItem] = Field(default_factory=list)
    has_more: bool = False
    generated_at: datetime


class ThesisBuilderDeadLetterItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_message_id: str
    article_id: str
    error_code: str | None = None
    failed_at: datetime


class ThesisBuilderMetricsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    window: str
    window_start_at: datetime
    window_end_at: datetime
    articles_processed_count: int
    market_moving_articles_count: int
    articles_included_in_cards_count: int
    stale_articles_count: int
    created_thesis_cards_count: int
    pending_thesis_cards_count: int
    oldest_pending_age_seconds: float | None = None
    average_pending_age_seconds: float | None = None
    minimum_pending_expires_in_seconds: float | None = None
    average_pending_expires_in_seconds: float | None = None
    missed_stale_thesis_cards_count: int
    stale_evidence_exceeded_avg_seconds: float | None = None
    stale_evidence_exceeded_p95_seconds: float | None = None
    stale_evidence_exceeded_max_seconds: float | None = None
    dead_letter_count: int = 0
    recent_dead_letters: list[ThesisBuilderDeadLetterItem] = Field(default_factory=list)
    pending_windows: list[ThesisBuilderEvidenceWindow] = Field(default_factory=list)
    actionable_cards: list[ThesisBuilderActionableCard] = Field(default_factory=list)
    generated_at: datetime


class BacklogResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    pending_count: int
    retrying_count: int
    dead_letter_count: int
    max_attempt_age_seconds: float | None = None
    generated_at: datetime


class DeadLetterItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    obligation_id: str
    source_key: str
    canonical_event_id: str
    reason: str | None = None
    first_failure_at: datetime | None = None
    updated_at: datetime


class DeadLetterResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    items: list[DeadLetterItem]
    limit: int
    offset: int
    generated_at: datetime


FilterQualityRunStatus = Literal["running", "completed", "failed"]


class NewsFilterConfigPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    filter_config_id: str | None = None
    config_name: str = "Test filter"
    config_role: str = "test"
    status: str = "active"
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    watchlist_tickers: list[str] = Field(default_factory=list)
    dedupe_algorithm: str = "rapidfuzz_ratio"
    dedupe_similarity_threshold: float = 0.9
    dedupe_lookback_hours: int = 24
    created_from_run_id: str | None = None


class FilterQualityRunSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: FilterQualityRunStatus
    news_window_start_at: datetime
    news_window_end_at: datetime
    created_at: datetime
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    rejection_precision_proxy: float | None = None
    incorrectly_accepted_rate_estimate: float | None = None
    dataset_input_count: int
    dataset_rejected_count: int
    dataset_accepted_count: int
    rejected_items_evaluated: int
    accepted_items_sampled: int
    correctly_rejected_count: int
    incorrectly_rejected_count: int
    correctly_accepted_count: int
    incorrectly_accepted_count: int
    item_failed_count: int = 0
    item_error_codes: dict[str, int] = Field(default_factory=dict)
    total_filter_quality: float | None = None
    total_correct_count: int = 0
    assumed_correct_accepted_count: int = 0
    evaluation_subject: str = "unknown"
    evaluated_filter_config: NewsFilterConfigPayload | None = None
    summary_json: dict[str, Any]
    recommendation_summary_md: str


class FilterQualityStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    running_run: FilterQualityRunSummary | None
    last_run: FilterQualityRunSummary | None
    generated_at: datetime


class FilterQualityIncorrectlyRejectedItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    assessment_id: str
    run_id: str
    article_id: str
    headline: str
    summary: str | None = None
    url: str
    source: str
    published_at: datetime
    production_matched_article_id: str | None = None
    production_matched_article_headline: str | None = None
    production_matched_article_url: str | None = None
    production_matched_article_source: str | None = None
    production_matched_article_published_at: datetime | None = None
    simulation_matched_article_id: str | None = None
    simulation_matched_article_headline: str | None = None
    simulation_matched_article_url: str | None = None
    simulation_matched_article_source: str | None = None
    simulation_matched_article_published_at: datetime | None = None
    production_filter_outcome: str | None = None
    simulation_filter_outcome: str | None = None
    rejection_reason_code: str | None = None
    production_rejection_reason_code: str | None = None
    simulation_rejection_reason_code: str | None = None
    probable_cause: str | None = None
    improvement_suggestion: str | None = None
    rationale: str | None = None
    classification_confidence: float | None = None
    suggestion_json: dict[str, Any] = Field(default_factory=dict)
    recommended_include_keywords: list[str] = Field(default_factory=list)
    evaluated_at: datetime


class FilterConfigSimulationStartResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["running"]


class FilterQualityIncorrectlyRejectedResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    items: list[FilterQualityIncorrectlyRejectedItem]
    generated_at: datetime


class FilterQualityIncorrectlyAcceptedItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    assessment_id: str
    run_id: str
    article_id: str
    headline: str
    summary: str | None = None
    url: str
    source: str
    published_at: datetime
    production_filter_outcome: str | None = None
    simulation_filter_outcome: str | None = None
    probable_cause: str | None = None
    improvement_suggestion: str | None = None
    rationale: str | None = None
    classification_confidence: float | None = None
    suggestion_json: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime


class FilterQualityIncorrectlyAcceptedResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    items: list[FilterQualityIncorrectlyAcceptedItem]
    generated_at: datetime


class FilterQualityStartRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted_audit_enabled: bool = False


class FilterQualityStartRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["running"]


class WatchlistItemPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str = Field(min_length=1)
    exchange_code: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    source: str = "manual"


class WatchlistItemResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    exchange_code: str
    display_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    is_active: bool = True
    source: str = "manual"
    has_missing_aliases: bool = False


class WatchlistResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    lookup_providers_configured: bool = True
    lookup_message: str | None = None
    items: list[WatchlistItemResponse]
    generated_at: datetime


class WatchlistLookupSuggestionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    exchange_code: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    provider: str


class WatchlistLookupResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    lookup_providers_configured: bool = True
    lookup_message: str | None = None
    suggestions: list[WatchlistLookupSuggestionResponse]
    cached: bool = False
    provider_warnings: list[str] = Field(default_factory=list)
    generated_at: datetime


class AliasDiscoveryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    exchange_code: str
    display_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    provider: str | None = None
    found: bool = False
    cached: bool = False
    generated_at: datetime


class ThesisReprocessRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    days_back: int = Field(ge=1, le=30)


class ThesisReprocessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: str


class ThesisReprocessStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: str
    days_back: int
    articles_found: int | None = None
    analyses_created: int | None = None
    cards_created: int | None = None
    error_code: str | None = None
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


BacktestRunStatus = Literal["running", "completed", "failed"]


class BacktestRunProgress(BaseModel):
    model_config = ConfigDict(frozen=True)

    phase: str  # "prewarming" | "simulating"
    done: int
    total: int
    current_ticker: str | None = None
    updated_at: datetime


class BacktestRunSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: BacktestRunStatus
    window_start_at: datetime
    window_end_at: datetime
    mode: str
    timing_scenario: str
    card_population: str
    strategies_requested: list[str] | None = None
    initial_capital: float
    net_pnl: float | None = None
    total_return: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    max_drawdown: float | None = None
    created_at: datetime
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    progress: BacktestRunProgress | None = None


class BacktestRunsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    window: str
    runs: list[BacktestRunSummary] = Field(default_factory=list)
    active_run: BacktestRunSummary | None = None
    generated_at: datetime


class BacktestScalarMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    net_pnl: float | None = None
    gross_profit: float | None = None
    gross_loss: float | None = None
    total_commission: float | None = None
    total_slippage: float | None = None
    total_return: float | None = None
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = None
    expectancy: float | None = None
    max_drawdown: float | None = None
    max_drawdown_duration_seconds: float | None = None
    sharpe_ratio: float | None = None
    exposure_fraction: float | None = None
    signal_accuracy: float | None = None
    cards_considered: int
    cards_in_population: int
    cards_live_executable: int
    cards_skipped_no_price: int
    trades_opened: int
    trades_closed: int
    trades_risk_blocked: int


class BacktestStrategyMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy: str
    trades_opened: int | None = None
    trades_closed: int | None = None
    trades_risk_blocked: int | None = None
    net_pnl: float | None = None
    gross_profit: float | None = None
    gross_loss: float | None = None
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = None
    expectancy: float | None = None


class BacktestCardStatusMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    bucket: str
    trades_opened: int | None = None
    trades_closed: int | None = None
    trades_risk_blocked: int | None = None
    net_pnl: float | None = None
    gross_profit: float | None = None
    gross_loss: float | None = None
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = None
    expectancy: float | None = None


class BacktestDelayAggregates(BaseModel):
    model_config = ConfigDict(frozen=True)

    avg_news_fetch_delay_seconds: float | None = None
    p95_news_fetch_delay_seconds: float | None = None
    max_news_fetch_delay_seconds: float | None = None
    avg_thesis_build_delay_seconds: float | None = None
    p95_thesis_build_delay_seconds: float | None = None
    max_thesis_build_delay_seconds: float | None = None
    avg_total_pipeline_delay_seconds: float | None = None
    p95_total_pipeline_delay_seconds: float | None = None
    max_total_pipeline_delay_seconds: float | None = None


class BacktestGapMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    pnl_gap: float | None = None
    win_rate_gap: float | None = None
    trades_flipped_by_delay: int | None = None


class BacktestRunDetailResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    run: BacktestRunSummary
    metrics: BacktestScalarMetrics
    per_strategy: list[BacktestStrategyMetrics] = Field(default_factory=list)
    card_status_breakdown: list[BacktestCardStatusMetrics] = Field(default_factory=list)
    delays: BacktestDelayAggregates
    gap: BacktestGapMetrics | None = None
    generated_at: datetime


class BacktestTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    trade_id: str
    ticker: str
    exchange_code: str
    strategy: str
    direction: str
    entry_timing_scenario: str
    entry_at: datetime | None = None
    entry_price: float | None = None
    exit_at: datetime | None = None
    exit_price: float | None = None
    net_pnl: float | None = None
    return_pct: float | None = None
    exit_reason: str
    risk_block_rule: str | None = None
    news_fetch_delay_seconds: float | None = None
    thesis_build_delay_seconds: float | None = None
    total_pipeline_delay_seconds: float | None = None
    card_decision_state: str
    card_was_live_expired: bool = False


class BacktestTradesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    run_id: str
    trades: list[BacktestTrade] = Field(default_factory=list)
    limit: int
    offset: int
    total_count: int
    generated_at: datetime


class BacktestEquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: datetime
    equity: float
    open_positions: int


class BacktestEquitySeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    timing_scenario: str
    points: list[BacktestEquityPoint] = Field(default_factory=list)


class BacktestEquityResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    run_id: str
    series: list[BacktestEquitySeries] = Field(default_factory=list)
    generated_at: datetime


class BacktestCardTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    trade_id: str
    entry_timing_scenario: str
    entry_at: datetime | None = None
    entry_price: float | None = None
    exit_at: datetime | None = None
    exit_price: float | None = None
    net_pnl: float | None = None
    return_pct: float | None = None
    exit_reason: str | None = None
    risk_block_rule: str | None = None


class BacktestCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    thesis_card_id: str
    ticker: str
    exchange_code: str
    direction: str
    strategy: str
    time_horizon: str | None = None
    confidence: float | None = None
    decision_state: str
    card_created_at: datetime
    card_expires_at: datetime | None = None
    trades: list[BacktestCardTrade] = Field(default_factory=list)


class BacktestCardsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    run_id: str
    cards: list[BacktestCard] = Field(default_factory=list)
    generated_at: datetime


class BacktestStartRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_start_at: datetime
    window_end_at: datetime
    mode: str = "replay"
    timing_scenario: str = "ideal"
    card_population: str = "all"
    strategies: list[str] | None = None
    initial_capital: float | None = None
    run_note: str | None = None


class BacktestStartRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["running"]
