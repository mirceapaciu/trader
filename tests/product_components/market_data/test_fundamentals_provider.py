from __future__ import annotations

from datetime import date

import pytest
import requests

from src.product_components.market_data.fundamentals_provider import (
    FinnhubFundamentalsClient,
    FundamentalsRateLimitError,
)


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("error", response=self)

    def json(self):
        return self._payload


class _FakeSession:
    """Routes each endpoint path to a (payload, status_code) pair."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.trust_env = True
        self.calls: list[tuple[str, dict, bool]] = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, self.trust_env))
        for path, (payload, status_code) in self.routes.items():
            if url.endswith(path):
                return _FakeResponse(payload, status_code)
        raise AssertionError(f"unexpected url: {url}")


_PROFILE = {"marketCapitalization": 3_100_000.0, "shareOutstanding": 15_000.0}
_METRIC = {"metric": {"revenuePerShareTTM": 26.0}}
_EARNINGS = {
    "earningsCalendar": [
        {"date": "2099-05-01"},
        {"date": "2099-01-30"},
        {"date": "2001-01-01"},  # historical entry must be ignored
    ]
}


def _client(monkeypatch, routes) -> FinnhubFundamentalsClient:
    fake_session = _FakeSession(routes)
    monkeypatch.setattr("requests.Session", lambda: fake_session)
    client = FinnhubFundamentalsClient(api_key="key")
    client._fake_session = fake_session  # test-only handle
    return client


def test_fetch_normalizes_millions_and_derives_revenue(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        {
            "/stock/profile2": (_PROFILE, 200),
            "/stock/metric": (_METRIC, 200),
            "/calendar/earnings": (_EARNINGS, 200),
        },
    )

    result = client.fetch(ticker="aapl")

    # profile2 reports millions; values are normalized to USD/shares.
    assert result.market_cap_usd == 3_100_000.0 * 1e6
    assert result.shares_outstanding == 15_000.0 * 1e6
    # Revenue TTM = revenuePerShareTTM x shares outstanding.
    assert result.revenue_ttm_usd == 26.0 * 15_000.0 * 1e6
    # Earliest upcoming earnings date wins; past entries are ignored.
    assert result.next_earnings_date == date(2099, 1, 30)
    # Raw payloads retained for audit.
    assert result.payload["profile2"] == _PROFILE
    # Every call disables ambient proxies and carries the token + upper-cased symbol.
    session = client._fake_session
    assert all(call[2] is False for call in session.calls)
    assert all(call[1]["token"] == "key" for call in session.calls)
    assert session.calls[0][1]["symbol"] == "AAPL"


def test_fetch_rate_limit_raises(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        {
            "/stock/profile2": ({}, 429),
            "/stock/metric": (_METRIC, 200),
            "/calendar/earnings": (_EARNINGS, 200),
        },
    )

    with pytest.raises(FundamentalsRateLimitError):
        client.fetch(ticker="AAPL")


def test_fetch_partial_endpoint_failure_degrades_fields_to_none(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        {
            "/stock/profile2": (_PROFILE, 200),
            "/stock/metric": ({}, 500),
            "/calendar/earnings": ({}, 500),
        },
    )

    result = client.fetch(ticker="AAPL")

    assert result.market_cap_usd == 3_100_000.0 * 1e6
    assert result.revenue_ttm_usd is None
    assert result.next_earnings_date is None


def test_fetch_all_endpoints_failed_raises(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        {
            "/stock/profile2": ({}, 500),
            "/stock/metric": ({}, 500),
            "/calendar/earnings": ({}, 500),
        },
    )

    with pytest.raises(RuntimeError, match="finnhub_fundamentals_all_endpoints_failed"):
        client.fetch(ticker="AAPL")


def test_fetch_handles_malformed_payloads(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        {
            "/stock/profile2": ({"marketCapitalization": "not-a-number"}, 200),
            "/stock/metric": ({"metric": None}, 200),
            "/calendar/earnings": ({"earningsCalendar": [{"date": "garbage"}]}, 200),
        },
    )

    result = client.fetch(ticker="AAPL")

    assert result.market_cap_usd is None
    assert result.shares_outstanding is None
    assert result.revenue_ttm_usd is None
    assert result.next_earnings_date is None


def test_client_requires_api_key() -> None:
    with pytest.raises(ValueError):
        FinnhubFundamentalsClient(api_key="  ")
