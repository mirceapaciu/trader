from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ThesisStrategy(StrEnum):
    EVENT_DRIVEN = "event_driven"
    SENTIMENT_MOMENTUM = "sentiment_momentum"
    SECTOR_ROTATION = "sector_rotation"
    CONTRARIAN_REVERSAL = "contrarian_reversal"
    TREND_FOLLOW = "trend_follow"


class TradeDirection(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class ValidationStatus(StrEnum):
    VALID = "valid"
    REJECTED = "rejected"


class ContentType(StrEnum):
    NEWS_CATALYST = "news_catalyst"
    OPINION = "opinion"


@dataclass(frozen=True)
class LlmTriageResult:
    ticker: str
    exchange_code: str
    instrument_is_subject: bool
    content_type: ContentType
    reasoning: str
    estimated_tokens: int = 0
    llm_model: str = ""


@dataclass(frozen=True)
class NewsArticle:
    id: str
    source: str
    headline: str
    summary: str | None
    url: str
    tickers: list[str]
    published_at: datetime
    fetched_at: datetime
    sentiment_source: float | None = None


@dataclass(frozen=True)
class InstrumentIdentity:
    ticker: str
    exchange_code: str


@dataclass(frozen=True)
class LlmAnalysisResult:
    ticker: str
    exchange_code: str
    sentiment: float
    relevance: float
    urgency: str
    suggested_action: str
    candidate_strategy: ThesisStrategy
    direction: TradeDirection
    confidence: float
    reasoning: str
    is_market_moving: bool
    instrument_is_subject: bool = False
    content_type: ContentType = ContentType.OPINION
    event_type: str | None = None
    price_impact_magnitude: str | None = None
    evidence_bullet_candidates: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    llm_model: str = ""


@dataclass(frozen=True)
class LlmSynthesisResult:
    verdict: str
    confidence: float
    thesis_summary: str
    evidence_bullets: list[str]
    risk_stop_condition: str
    risk_invalidation_condition: str
    risk_rationale: str
    reasoning: str
    reason_code: str | None = None
    estimated_tokens: int = 0
    llm_model: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PersistedAnalysis:
    id: int
    article_id: str
    article: NewsArticle
    ticker: str
    exchange_code: str
    strategy: ThesisStrategy | None
    direction: TradeDirection | None
    confidence: float
    reasoning: str | None
    validation_status: ValidationStatus
    rejection_reason_code: str | None
    analyzed_at: datetime


@dataclass(frozen=True)
class ThesisCardSignal:
    thesis_card_id: str
    ticker: str
    exchange_code: str
    direction: str
    time_horizon: str
    strategy: str
    confidence: float
    risk_box: dict[str, Any]
    source_analysis_ids: list[int]
    created_at: datetime
    expires_at: datetime

    def envelope(self) -> dict[str, Any]:
        return {
            "event_id": f"evt_thesis_card_{self.thesis_card_id}",
            "event_type": "thesis_card.created",
            "event_version": "1.0",
            "occurred_at": self.created_at.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "producer": "thesis_builder",
            "dedupe_key": self.thesis_card_id,
            "payload": {
                "thesis_card_id": self.thesis_card_id,
                "ticker": self.ticker,
                "exchange_code": self.exchange_code,
                "direction": self.direction,
                "time_horizon": self.time_horizon,
                "strategy": self.strategy,
                "confidence": self.confidence,
                "risk_box": self.risk_box,
                "source_analysis_ids": self.source_analysis_ids,
                "created_at": self.created_at.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": self.expires_at.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }
