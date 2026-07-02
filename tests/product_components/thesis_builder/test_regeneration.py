from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import SimpleNamespace

from src.product_components.thesis_builder import regeneration as regeneration_module
from src.product_components.thesis_builder.llm_client import TokenBudgetExhausted
from src.product_components.thesis_builder.models import NewsArticle
from src.product_components.thesis_builder.regeneration import (
    RegenerationRunner,
    RegenerationThresholds,
)

_WINDOW_START = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
_WINDOW_END = _WINDOW_START + timedelta(days=1)


class _Status(StrEnum):
    STALE = "stale"


@dataclass(frozen=True)
class _ContextSnapshot:
    as_of: datetime
    source_status: _Status
    current_price: float


def _article(idx: int) -> NewsArticle:
    ts = _WINDOW_START + timedelta(hours=idx)
    return NewsArticle(
        id=f"a-{idx}",
        source="test",
        headline=f"headline {idx}",
        summary=None,
        url=f"https://example.com/{idx}",
        tickers=["AAA"],
        published_at=ts,
        fetched_at=ts,
    )


class _FakeAnalyzer:
    model = "gpt-4o"
    max_tokens_per_run = 1000

    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.contexts: list = []

    def analyze_article(self, *, article, ticker, exchange_code, market_context_snapshot):
        self.contexts.append(market_context_snapshot)
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _FakeRepo:
    def __init__(self, signals=None):
        self.persisted: list[dict] = []
        self.rejected: list[str] = []
        self._signals = list(signals or [])

    def persist_analysis_and_update_evidence(self, *, article, result, market_context_snapshot,
                                             reprocess_run_id, **_kwargs):
        signal = self._signals.pop(0) if self._signals else None
        self.persisted.append(
            {"article": article.id, "ctx": market_context_snapshot, "run": reprocess_run_id}
        )
        return SimpleNamespace(analysis_id=len(self.persisted), signal=signal)

    def persist_rejected_analysis(self, *, article, instrument, rejection_reason_code,
                                  llm_model, validation_errors):
        self.rejected.append(rejection_reason_code)
        return len(self.rejected)


class _Registry:
    def list_active_instruments(self):
        return []


_THRESHOLDS = RegenerationThresholds(
    required_evidence_count=1,
    min_confidence=0.0,
    min_relevance=0.0,
    risk_max_loss_usd=120.0,
    default_time_horizon="swing_1d_5d",
    evidence_collection_max_minutes=120,
    max_evidence_age_minutes=180,
)


def _instrument():
    return SimpleNamespace(ticker="AAA", exchange_code="XNAS")


def _runner(monkeypatch, *, articles, analyzer, repo, market_context_provider=None):
    runner = RegenerationRunner(
        dsn="postgresql://unused",
        news_fetcher_schema="news_fetcher",
        run_id="bt_run1",
        repository=repo,
        analyzer=analyzer,
        instrument_registry=_Registry(),
        thresholds=_THRESHOLDS,
        market_context_provider=market_context_provider,
        card_delay_seconds=180,
    )
    monkeypatch.setattr(
        runner, "_fetch_articles", lambda *, window_start_at, window_end_at: list(articles)
    )
    monkeypatch.setattr(
        regeneration_module,
        "_resolve_instruments",
        lambda *, article, active_instruments: [_instrument()],
    )
    return runner


def test_regeneration_analyzes_articles_and_creates_cards(monkeypatch):
    articles = [_article(0), _article(1)]
    analyzer = _FakeAnalyzer([object(), object()])
    # A card (signal) is produced on the second article only.
    repo = _FakeRepo(signals=[None, SimpleNamespace(thesis_card_id="c-1")])
    runner = _runner(monkeypatch, articles=articles, analyzer=analyzer, repo=repo)

    result = runner.run(window_start_at=_WINDOW_START, window_end_at=_WINDOW_END)

    assert result.articles_found == 2
    assert result.articles_relevant == 2
    assert result.articles_analyzed == 2
    assert result.analyses_created == 2
    assert result.cards_created == 1
    assert not result.budget_exhausted
    # Every persisted analysis is tagged with the run id (isolates it in the sim schema).
    assert [p["run"] for p in repo.persisted] == ["bt_run1", "bt_run1"]


def test_regeneration_stops_on_token_budget_exhaustion(monkeypatch):
    articles = [_article(0), _article(1), _article(2)]
    # First analysis ok, second exhausts the budget -> stop before the third.
    analyzer = _FakeAnalyzer([object(), TokenBudgetExhausted("budget"), object()])
    repo = _FakeRepo()
    runner = _runner(monkeypatch, articles=articles, analyzer=analyzer, repo=repo)

    result = runner.run(window_start_at=_WINDOW_START, window_end_at=_WINDOW_END)

    assert result.budget_exhausted is True
    assert result.analyses_created == 1
    assert result.articles_relevant == 2  # article 1 and 2 matched; 3rd never reached
    assert result.articles_analyzed == 1  # only article 1 got a persisted analysis
    assert len(repo.persisted) == 1


def test_regeneration_records_rejected_analysis(monkeypatch):
    articles = [_article(0)]
    analyzer = _FakeAnalyzer([ValueError("invalid_llm_response")])
    repo = _FakeRepo()
    runner = _runner(monkeypatch, articles=articles, analyzer=analyzer, repo=repo)

    result = runner.run(window_start_at=_WINDOW_START, window_end_at=_WINDOW_END)

    assert repo.rejected == ["invalid_llm_response"]
    assert result.analyses_created == 1
    assert result.cards_created == 0
    # A rejected LLM response is not a valid analysis persistence.
    assert repo.persisted == []


def test_regeneration_passes_reconstructed_context_as_json(monkeypatch):
    articles = [_article(0)]
    analyzer = _FakeAnalyzer([object()])
    repo = _FakeRepo()

    captured_as_of: list[datetime] = []

    def _context(ticker, exchange_code, as_of):
        captured_as_of.append(as_of)
        return _ContextSnapshot(as_of=as_of, source_status=_Status.STALE, current_price=101.5)

    runner = _runner(
        monkeypatch, articles=articles, analyzer=analyzer, repo=repo,
        market_context_provider=_context,
    )

    runner.run(window_start_at=_WINDOW_START, window_end_at=_WINDOW_END)

    # Context is requested as-of published_at + card delay (no look-ahead beyond analysis time).
    assert captured_as_of[0] == articles[0].published_at + timedelta(seconds=180)
    # The snapshot dataclass is converted to the same JSON shape production feeds the LLM:
    # datetimes -> isoformat, enums -> their value.
    ctx = analyzer.contexts[0]
    assert ctx["source_status"] == "stale"
    assert ctx["current_price"] == 101.5
    assert isinstance(ctx["as_of"], str)
    # Same dict reaches persistence.
    assert repo.persisted[0]["ctx"] == ctx


def test_regeneration_progress_reports_instrument_label_not_article_id(monkeypatch):
    articles = [_article(0)]
    analyzer = _FakeAnalyzer([object()])
    repo = _FakeRepo()
    events: list[tuple[int, int, str | None]] = []
    runner = RegenerationRunner(
        dsn="postgresql://unused",
        news_fetcher_schema="news_fetcher",
        run_id="bt_run1",
        repository=repo,
        analyzer=analyzer,
        instrument_registry=_Registry(),
        thresholds=_THRESHOLDS,
        card_delay_seconds=180,
        progress=lambda done, total, label: events.append((done, total, label)),
    )
    monkeypatch.setattr(
        runner, "_fetch_articles", lambda *, window_start_at, window_end_at: list(articles)
    )
    monkeypatch.setattr(
        regeneration_module,
        "_resolve_instruments",
        lambda *, article, active_instruments: [SimpleNamespace(ticker="AAA", exchange_code="XNAS")],
    )

    runner.run(window_start_at=_WINDOW_START, window_end_at=_WINDOW_END)

    # Progress carries the resolved instrument ticker, never the opaque article id.
    assert events and events[-1] == (1, 1, "AAA")
    assert not any(str(label or "").startswith("a-") for _d, _t, label in events)


def test_regeneration_without_context_provider_passes_none(monkeypatch):
    articles = [_article(0)]
    analyzer = _FakeAnalyzer([object()])
    repo = _FakeRepo()
    runner = _runner(monkeypatch, articles=articles, analyzer=analyzer, repo=repo)

    runner.run(window_start_at=_WINDOW_START, window_end_at=_WINDOW_END)

    assert analyzer.contexts == [None]
