from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger("market_data")

_FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
_EARNINGS_LOOKAHEAD_DAYS = 180


class FundamentalsRateLimitError(RuntimeError):
    """Raised when Finnhub responds with HTTP 429."""


@dataclass(frozen=True)
class FundamentalsFetchResult:
    """Normalized company scale facts plus the raw endpoint payloads for audit."""

    market_cap_usd: float | None
    shares_outstanding: float | None
    revenue_ttm_usd: float | None
    next_earnings_date: date | None
    payload: dict[str, Any] = field(default_factory=dict)


class FinnhubFundamentalsClient:
    """Fetches company fundamentals from Finnhub (profile2 + metric + earnings calendar).

    A single ``fetch`` makes up to three HTTP calls; each endpoint failure other
    than rate limiting degrades that endpoint's fields to None. Only rate
    limiting or all three endpoints failing raise.
    """

    endpoints = ("stock_profile2", "stock_metric", "calendar_earnings")

    def __init__(self, *, api_key: str, timeout_seconds: float = 10.0) -> None:
        if not api_key.strip():
            raise ValueError("FINNHUB_API_KEY is required for FinnhubFundamentalsClient")
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds

    def fetch(self, *, ticker: str) -> FundamentalsFetchResult:
        symbol = ticker.strip().upper()
        payload: dict[str, Any] = {}
        failures = 0

        profile = self._get_json("/stock/profile2", {"symbol": symbol})
        if profile is None:
            failures += 1
            profile = {}
        payload["profile2"] = profile

        metric_response = self._get_json("/stock/metric", {"symbol": symbol, "metric": "all"})
        if metric_response is None:
            failures += 1
            metric_response = {}
        payload["metric"] = metric_response

        today = datetime.now(timezone.utc).date()
        earnings = self._get_json(
            "/calendar/earnings",
            {
                "symbol": symbol,
                "from": today.isoformat(),
                "to": (today + timedelta(days=_EARNINGS_LOOKAHEAD_DAYS)).isoformat(),
            },
        )
        if earnings is None:
            failures += 1
            earnings = {}
        payload["earnings_calendar"] = earnings

        if failures == len(self.endpoints):
            raise RuntimeError("finnhub_fundamentals_all_endpoints_failed")

        # profile2 reports marketCapitalization and shareOutstanding in millions.
        market_cap_usd = _millions_to_usd(profile.get("marketCapitalization"))
        shares_outstanding = _millions_to_usd(profile.get("shareOutstanding"))
        return FundamentalsFetchResult(
            market_cap_usd=market_cap_usd,
            shares_outstanding=shares_outstanding,
            revenue_ttm_usd=_revenue_ttm_usd(metric_response, shares_outstanding),
            next_earnings_date=_next_earnings_date(earnings, today),
            payload=payload,
        )

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                f"{_FINNHUB_BASE_URL}{path}",
                params={**params, "token": self._api_key},
                timeout=self._timeout_seconds,
            )
        except requests.RequestException:
            logger.warning("finnhub fundamentals request failed: %s", path, exc_info=True)
            return None
        if response.status_code == 429:
            raise FundamentalsRateLimitError("finnhub_rate_limited")
        try:
            response.raise_for_status()
            parsed = response.json()
        except (requests.RequestException, ValueError):
            logger.warning("finnhub fundamentals response invalid: %s", path, exc_info=True)
            return None
        return parsed if isinstance(parsed, dict) else None


def _millions_to_usd(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed * 1e6 if parsed > 0 else None


def _revenue_ttm_usd(metric_response: dict[str, Any], shares_outstanding: float | None) -> float | None:
    metric = metric_response.get("metric")
    if not isinstance(metric, dict):
        return None
    # No direct TTM revenue field exists on /stock/metric; derive it from
    # revenue per share TTM and shares outstanding.
    revenue_per_share = metric.get("revenuePerShareTTM")
    try:
        per_share = float(revenue_per_share)
    except (TypeError, ValueError):
        return None
    if per_share <= 0 or shares_outstanding is None:
        return None
    return per_share * shares_outstanding


def _next_earnings_date(earnings_response: dict[str, Any], today: date) -> date | None:
    calendar = earnings_response.get("earningsCalendar")
    if not isinstance(calendar, list):
        return None
    upcoming: list[date] = []
    for entry in calendar:
        if not isinstance(entry, dict):
            continue
        try:
            entry_date = date.fromisoformat(str(entry.get("date")))
        except (TypeError, ValueError):
            continue
        if entry_date >= today:
            upcoming.append(entry_date)
    return min(upcoming) if upcoming else None
