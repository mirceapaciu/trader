from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core_components.backtest_engine import Bar
from src.product_components.backtester.models import (
    BacktestMode,
    BacktestRunParams,
    ExecutionMode,
    ExecutionModel,
)
from src.product_components.backtester.repository import (
    render_thesis_schema_sql,
    sim_schema_name,
)
from src.product_components.backtester.service import BacktesterService
from src.product_components.thesis_builder.export import (
    ExportedEvidenceArticle,
    ExportedThesisCard,
)
from src.product_components.thesis_builder.regeneration import RegenerationResult

UTC = timezone.utc
T0 = datetime(2025, 1, 2, 14, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[3]


# ----- schema helpers ----------------------------------------------------


def test_sim_schema_name_is_safe_identifier() -> None:
    assert sim_schema_name("bt_abc123") == "sim_bt_abc123"
    # Hyphens (not typical for run ids) are normalized to keep a valid identifier.
    assert sim_schema_name("bt-abc") == "sim_bt_abc"


def test_render_thesis_schema_sql_rebinds_schema() -> None:
    rendered = render_thesis_schema_sql(repo_root=_REPO_ROOT, target_schema="sim_bt_1")
    assert "CREATE SCHEMA IF NOT EXISTS sim_bt_1;" in rendered
    assert "sim_bt_1.t_thesis_cards" in rendered
    assert "sim_bt_1.t_news_analyses" in rendered
    # No references to the production schema leak through.
    assert "thesis_builder" not in rendered


# ----- fakes -------------------------------------------------------------


def _card(card_id: str = "card-1", confidence: float = 0.9) -> ExportedThesisCard:
    published = T0
    created = T0 + timedelta(seconds=180)
    return ExportedThesisCard(
        id=card_id,
        ticker="AAPL",
        exchange_code="NASDAQ",
        direction="buy",
        strategy="sentiment_momentum",
        time_horizon="intraday",
        confidence=confidence,
        risk_max_loss_usd=100.0,
        risk_stop_condition="stop",
        risk_invalidation_condition="invalidate",
        validation_status="valid",
        rejection_reason_code=None,
        created_at=created,
        expires_at=created + timedelta(hours=4),
        signal_published_at=created,
        evidence=[
            ExportedEvidenceArticle(
                article_id="art-1", published_at=published, fetched_at=published
            )
        ],
        news_ready_at=published,
    )


class _FakeCards:
    def __init__(self, cards):
        self._cards = cards

    def export_cards(self, *, window_start_at, window_end_at, validation_status=None, strategy=None):
        return list(self._cards)


class _FakeBars:
    def __init__(self, bars):
        self._bars = bars

    def historical_bars(self, *, ticker, exchange_code, interval, start, end):
        return list(self._bars)

    def warm(self, instruments, *, interval, start, end, progress=None):
        return None


def _rising_bars(start: datetime, count: int = 30):
    bars = []
    price = 100.0
    for i in range(count):
        nxt = price * 1.002
        bars.append(
            Bar(start_at=start + timedelta(minutes=i), open=price, high=nxt, low=price, close=nxt, volume=1000)
        )
        price = nxt
    return bars


class _RecordingRepo:
    def __init__(self):
        self.calls: list[str] = []
        self.create_run_kwargs: dict | None = None
        self.finalized_success = False
        self.finalized_failure: dict | None = None

    def bootstrap_sim_thesis_schema(self, *, repo_root, sim_schema):
        self.calls.append(f"bootstrap:{sim_schema}")

    def count_sim_evidence_windows(self, *, sim_schema):
        return 0

    def create_run(self, *, params, dataset_snapshot_hash, thesis_config_snapshot=None,
                   llm_token_budget_limit=None, llm_model=None):
        self.create_run_kwargs = {
            "dataset_snapshot_hash": dataset_snapshot_hash,
            "thesis_config_snapshot": thesis_config_snapshot,
            "llm_token_budget_limit": llm_token_budget_limit,
            "llm_model": llm_model,
        }
        self.calls.append("create_run")

    def insert_card_snapshots(self, *, run_id, snapshots):
        self.calls.append("insert_card_snapshots")

    def insert_trades(self, *, run_id, trades):
        self.calls.append("insert_trades")

    def insert_equity_points(self, *, run_id, points):
        self.calls.append("insert_equity_points")

    def finalize_run_success(
        self,
        *,
        run_id,
        metrics,
        llm_tokens_used=None,
        budget_exhausted=None,
        analysis_coverage_until_at=None,
    ):
        self.finalized_success = True
        self.finalized_summary_json = dict(metrics.summary_json)
        self.finalized_summary_md = metrics.summary_md
        self.finalized_coverage = {
            "llm_tokens_used": llm_tokens_used,
            "budget_exhausted": budget_exhausted,
            "analysis_coverage_until_at": analysis_coverage_until_at,
        }
        self.calls.append("finalize_success")

    def finalize_run_failure(self, *, run_id, error_code, details=None):
        self.finalized_failure = {"error_code": error_code, "details": details}
        self.calls.append("finalize_failure")


class _RecordingRegeneration:
    def __init__(self, cards, *, raise_error: Exception | None = None, result=None):
        self._cards = cards
        self._raise = raise_error
        self._result = result
        self.regenerate_kwargs: dict | None = None
        self.config_model: str | None = None

    def thesis_config_snapshot(self, *, llm_model, required_evidence_count=None,
                               evidence_collection_max_minutes=None):
        self.config_model = llm_model
        return {
            "llm_model": llm_model,
            "required_evidence_count": required_evidence_count or 1,
            "evidence_collection_max_minutes": evidence_collection_max_minutes or 120,
        }

    def regenerate(self, *, run_id, sim_schema, window_start_at, window_end_at,
                   llm_model, token_budget, card_delay_seconds,
                   required_evidence_count=None, evidence_collection_max_minutes=None,
                   progress=None):
        self.regenerate_kwargs = {
            "run_id": run_id,
            "sim_schema": sim_schema,
            "llm_model": llm_model,
            "token_budget": token_budget,
            "card_delay_seconds": card_delay_seconds,
            "required_evidence_count": required_evidence_count,
            "evidence_collection_max_minutes": evidence_collection_max_minutes,
        }
        if self._raise is not None:
            raise self._raise
        if self._result is not None:
            return self._result
        return RegenerationResult(
            run_id=run_id, articles_found=1, articles_relevant=1, articles_analyzed=1,
            analyses_created=1, cards_created=len(self._cards), budget_exhausted=False,
        )

    def cards_provider(self, *, sim_schema):
        return _FakeCards(self._cards)


def _settings(regeneration_enabled: bool = True):
    return SimpleNamespace(
        regeneration_enabled=regeneration_enabled,
        persist_card_snapshots=True,
        persist_equity_points=True,
    )


def _params(mode: BacktestMode = BacktestMode.REGENERATION) -> BacktestRunParams:
    return BacktestRunParams(
        run_id="bt_run1",
        window_start_at=T0 - timedelta(minutes=5),
        window_end_at=T0 + timedelta(hours=6),
        mode=mode,
        ideal_fetch_delay_seconds=120,
        ideal_thesis_delay_seconds=60,
        execution_model=ExecutionModel(mode=ExecutionMode.LEGACY_FLAT_PERCENT),
        llm_model="gpt-4o",
        llm_max_tokens_per_run=12345,
    )


# ----- service regeneration orchestration --------------------------------


def test_regeneration_happy_path_populates_sim_and_simulates():
    repo = _RecordingRepo()
    entry = T0 + timedelta(seconds=180)
    regeneration = _RecordingRegeneration([_card()])
    service = BacktesterService(
        settings=_settings(),
        repository=repo,
        cards_provider=_FakeCards([]),  # production cards must NOT be used
        bars_provider=_FakeBars(_rising_bars(entry)),
        regeneration_provider=regeneration,
        repo_root=Path("."),
    )

    service.run(_params())

    # The run row is created FIRST (so failures are always persisted), then the
    # isolated sim schema is bootstrapped.
    assert repo.calls[0] == "create_run"
    assert "bootstrap:sim_bt_run1" in repo.calls
    assert repo.calls.index("create_run") < repo.calls.index("bootstrap:sim_bt_run1")
    assert repo.finalized_success is True
    # create_run recorded regeneration config + token budget + model (satisfies DB constraints).
    assert repo.create_run_kwargs["llm_model"] == "gpt-4o"
    assert repo.create_run_kwargs["llm_token_budget_limit"] == 12345
    assert repo.create_run_kwargs["thesis_config_snapshot"]["llm_model"] == "gpt-4o"
    assert repo.create_run_kwargs["dataset_snapshot_hash"]
    # regenerate invoked with the chosen model, budget, and card delay = ideal delays.
    assert regeneration.regenerate_kwargs["llm_model"] == "gpt-4o"
    assert regeneration.regenerate_kwargs["token_budget"] == 12345
    assert regeneration.regenerate_kwargs["card_delay_seconds"] == 180
    assert regeneration.regenerate_kwargs["sim_schema"] == "sim_bt_run1"
    # A trade was simulated over the regenerated (sim) cards.
    assert "insert_trades" in repo.calls
    # Regeneration funnel stats are merged into the run summary the UI reads.
    regen = repo.finalized_summary_json["regeneration"]
    assert regen["articles_relevant"] == 1
    assert regen["articles_analyzed"] == 1
    assert regen["evidence_windows_created"] == 0
    assert regen["cards_created"] == 1
    assert regen["llm_token_budget_limit"] == 12345
    # A fully-covered run records no exhaustion and no coverage boundary.
    assert regen["budget_exhausted"] is False
    assert regen["analysis_coverage_until_at"] is None
    assert repo.finalized_coverage == {
        "llm_tokens_used": 0,
        "budget_exhausted": False,
        "analysis_coverage_until_at": None,
    }
    # summary_md is unchanged (no exhaustion note).
    assert "budget exhausted" not in repo.finalized_summary_md.lower()


def test_regeneration_budget_exhaustion_persists_coverage_and_summary():
    repo = _RecordingRepo()
    entry = T0 + timedelta(seconds=180)
    # Window is [T0 - 5m, T0 + 6h]; coverage stops ~halfway through.
    coverage_until = T0 + timedelta(hours=3)
    exhausted = RegenerationResult(
        run_id="bt_run1",
        articles_found=10,
        articles_relevant=6,
        articles_analyzed=4,
        analyses_created=4,
        cards_created=1,
        budget_exhausted=True,
        llm_tokens_used=12000,
        analysis_coverage_until_at=coverage_until,
    )
    regeneration = _RecordingRegeneration([_card()], result=exhausted)
    service = BacktesterService(
        settings=_settings(),
        repository=repo,
        cards_provider=_FakeCards([]),
        bars_provider=_FakeBars(_rising_bars(entry)),
        regeneration_provider=regeneration,
        repo_root=Path("."),
    )

    service.run(_params())

    assert repo.finalized_success is True
    # First-class coverage facts are persisted on the run row.
    assert repo.finalized_coverage["budget_exhausted"] is True
    assert repo.finalized_coverage["llm_tokens_used"] == 12000
    assert repo.finalized_coverage["analysis_coverage_until_at"] == coverage_until
    # summary_json carries the boundary + fraction for the detail projection.
    regen = repo.finalized_summary_json["regeneration"]
    assert regen["budget_exhausted"] is True
    assert regen["analysis_coverage_until_at"] == coverage_until.isoformat()
    # Window span is 6h5m; ~3h5m covered -> ~50%.
    assert 0.45 <= regen["analysis_coverage_fraction"] <= 0.55
    # summary_md states the exhaustion + covered range.
    assert "token budget exhausted" in repo.finalized_summary_md.lower()
    assert "of window" in repo.finalized_summary_md.lower()


def test_regeneration_passes_evidence_threshold_overrides():
    repo = _RecordingRepo()
    entry = T0 + timedelta(seconds=180)
    regeneration = _RecordingRegeneration([_card()])
    params = BacktestRunParams(
        run_id="bt_run1",
        window_start_at=T0 - timedelta(minutes=5),
        window_end_at=T0 + timedelta(hours=6),
        mode=BacktestMode.REGENERATION,
        execution_model=ExecutionModel(mode=ExecutionMode.LEGACY_FLAT_PERCENT),
        required_evidence_count=2,
        evidence_collection_max_minutes=1440,
    )
    BacktesterService(
        settings=_settings(),
        repository=repo,
        cards_provider=_FakeCards([]),
        bars_provider=_FakeBars(_rising_bars(entry)),
        regeneration_provider=regeneration,
        repo_root=Path("."),
    ).run(params)

    assert regeneration.regenerate_kwargs["required_evidence_count"] == 2
    assert regeneration.regenerate_kwargs["evidence_collection_max_minutes"] == 1440


def test_regeneration_disabled_records_failed_run():
    # Even when regeneration is disabled, the run must be persisted and marked
    # failed (so the UI shows an error) rather than left without a DB row.
    repo = _RecordingRepo()
    service = BacktesterService(
        settings=_settings(regeneration_enabled=False),
        repository=repo,
        cards_provider=_FakeCards([]),
        bars_provider=_FakeBars([]),
        regeneration_provider=_RecordingRegeneration([_card()]),
        repo_root=Path("."),
    )
    with pytest.raises(RuntimeError, match="regeneration_disabled"):
        service.run(_params())
    assert "create_run" in repo.calls
    assert repo.finalized_failure is not None
    assert repo.finalized_failure["error_code"] == "RuntimeError"


def test_regeneration_without_provider_raises():
    service = BacktesterService(
        settings=_settings(),
        repository=_RecordingRepo(),
        cards_provider=_FakeCards([]),
        bars_provider=_FakeBars([]),
        regeneration_provider=None,
        repo_root=Path("."),
    )
    with pytest.raises(RuntimeError, match="regeneration_provider_unavailable"):
        service.run(_params())


def test_regeneration_failure_finalizes_run_failed_and_reraises():
    repo = _RecordingRepo()
    boom = ValueError("kaboom")
    service = BacktesterService(
        settings=_settings(),
        repository=repo,
        cards_provider=_FakeCards([]),
        bars_provider=_FakeBars([]),
        regeneration_provider=_RecordingRegeneration([_card()], raise_error=boom),
        repo_root=Path("."),
    )
    with pytest.raises(ValueError, match="kaboom"):
        service.run(_params())
    # Run row exists (created before regeneration) and is marked failed.
    assert "create_run" in repo.calls
    assert repo.finalized_failure is not None
    assert repo.finalized_failure["error_code"] == "ValueError"


def test_provider_resolves_threshold_defaults_and_overrides():
    from src.product_components.backtester.regeneration import ThesisRegenerationProvider

    thesis_settings = SimpleNamespace(
        llm_max_output_tokens=1200,
        required_evidence_count=3,
        min_confidence=0.6,
        min_relevance=0.5,
        risk_max_loss_usd=120.0,
        tradeability_max_entry_price=1000.0,
        tradeability_atr_stop_mult=1.5,
        default_time_horizon="swing_1d_5d",
        evidence_collection_max_minutes=120,
        max_evidence_age_minutes=180,
        triage_enabled=False,
        triage_model="triage-model",
        triage_max_output_tokens=200,
        synthesis_enabled=False,
        synthesis_model="synthesis-model",
        synthesis_max_output_tokens=1200,
        synthesis_fallback_to_mechanical=False,
        listicle_prefilter_enabled=False,
        listicle_prefilter_tag_threshold=6,
        already_priced_event_driven_atr_multiple=1.5,
        already_priced_event_driven_return_threshold=0.04,
        already_priced_sentiment_momentum_atr_multiple=2.0,
        already_priced_sentiment_momentum_return_threshold=0.06,
    )
    provider = ThesisRegenerationProvider(
        dsn="postgresql://unused",
        thesis_settings=thesis_settings,
        instrument_registry=SimpleNamespace(),
        market_data_service=SimpleNamespace(),
        quote_max_age_seconds=300,
    )
    # None -> production defaults
    default_cfg = provider.thesis_config_snapshot(llm_model="m")
    assert default_cfg["required_evidence_count"] == 3
    assert default_cfg["evidence_collection_max_minutes"] == 120
    assert default_cfg["triage_enabled"] is False
    assert default_cfg["synthesis_enabled"] is False
    assert default_cfg["synthesis_model"] == "synthesis-model"
    assert default_cfg["tradeability_max_entry_price"] == 1000.0
    assert default_cfg["tradeability_atr_stop_mult"] == 1.5
    assert default_cfg["already_priced_event_driven_atr_multiple"] == 1.5
    # explicit overrides win
    override_cfg = provider.thesis_config_snapshot(
        llm_model="m", required_evidence_count=2, evidence_collection_max_minutes=1440
    )
    assert override_cfg["required_evidence_count"] == 2
    assert override_cfg["evidence_collection_max_minutes"] == 1440


def test_market_context_requests_bars_as_of_without_lookahead():
    from src.product_components.backtester.regeneration import ThesisRegenerationProvider

    captured: dict = {}

    class _FakeMarketData:
        def get_historical_bars(self, *, ticker, exchange_code, interval, start, end):
            captured.update(
                {"ticker": ticker, "interval": interval, "start": start, "end": end}
            )
            return []  # no bars -> context is None

    provider = ThesisRegenerationProvider(
        dsn="postgresql://unused",
        thesis_settings=SimpleNamespace(),  # unused in this path
        instrument_registry=SimpleNamespace(),
        market_data_service=_FakeMarketData(),
        quote_max_age_seconds=300,
    )

    as_of = T0 + timedelta(minutes=30)
    result = provider._market_context("AAPL", "XNAS", as_of)

    assert result is None
    assert captured["interval"] == "1d"
    # No look-ahead: bars are only requested up to the analysis time.
    assert captured["end"] == as_of
    assert captured["start"] < as_of


def test_replay_run_records_no_thesis_config():
    repo = _RecordingRepo()
    entry = T0 + timedelta(seconds=180)
    service = BacktesterService(
        settings=_settings(),
        repository=repo,
        cards_provider=_FakeCards([_card()]),
        bars_provider=_FakeBars(_rising_bars(entry)),
        regeneration_provider=_RecordingRegeneration([_card()]),
        repo_root=Path("."),
    )

    service.run(_params(mode=BacktestMode.REPLAY))

    # Replay must not bootstrap a sim schema or record regeneration config.
    assert not any(c.startswith("bootstrap:") for c in repo.calls)
    assert repo.create_run_kwargs["thesis_config_snapshot"] is None
    assert repo.create_run_kwargs["llm_token_budget_limit"] is None
    assert repo.create_run_kwargs["llm_model"] is None
    assert repo.finalized_success is True
