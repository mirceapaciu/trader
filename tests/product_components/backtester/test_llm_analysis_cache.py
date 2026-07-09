from __future__ import annotations

import json

import pytest

from src.product_components.backtester.llm_analysis_cache import (
    CachedThesisLlmClient,
    prompt_sha256,
)


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int, str], dict] = {}
        self.puts: list[dict] = []

    def get(self, *, llm_model: str, max_output_tokens: int, prompt_sha256: str):
        return self.rows.get((llm_model, max_output_tokens, prompt_sha256))

    def put(
        self,
        *,
        llm_model: str,
        max_output_tokens: int,
        prompt_sha256: str,
        response_json: dict,
        article_id: str | None,
        ticker: str | None,
        exchange_code: str | None,
    ) -> None:
        self.puts.append(
            {
                "llm_model": llm_model,
                "max_output_tokens": max_output_tokens,
                "prompt_sha256": prompt_sha256,
                "response_json": response_json,
                "article_id": article_id,
                "ticker": ticker,
                "exchange_code": exchange_code,
            }
        )
        self.rows[(llm_model, max_output_tokens, prompt_sha256)] = response_json


class _FakeInner:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response or _raw_response(estimated_tokens=321)
        self.error = error
        self.calls = 0

    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.response)

    def analyze_synthesis(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {
            "verdict": "approve",
            "confidence": 0.82,
            "thesis_summary": "Guidance supports the thesis.",
            "evidence_bullets": ["Guidance raised."],
            "risk_stop_condition": "close_below_support",
            "risk_invalidation_condition": "guidance_reversed",
            "risk_rationale": "Risk tied to catalyst.",
            "reasoning": "Coherent dossier.",
            "reason_code": None,
            "estimated_tokens": 222,
        }


def test_cached_client_miss_delegates_and_stores_raw_response() -> None:
    store = _FakeStore()
    inner = _FakeInner()
    client = CachedThesisLlmClient(inner=inner, cache=store)
    prompt = _prompt(article_id="article-1", ticker="aapl", exchange_code="xnas")

    raw = client.analyze(model="model-a", prompt=prompt, max_output_tokens=1200)

    assert raw["estimated_tokens"] == 321
    assert inner.calls == 1
    assert client.llm_calls == 1
    assert client.cache_hits == 0
    assert store.puts == [
        {
            "llm_model": "model-a",
            "max_output_tokens": 1200,
            "prompt_sha256": prompt_sha256(prompt),
            "response_json": raw,
            "article_id": "article-1",
            "ticker": "AAPL",
            "exchange_code": "XNAS",
        }
    ]


def test_cached_client_hit_returns_budget_free_copy_without_delegating() -> None:
    prompt = _prompt(article_id="article-1", ticker="AAPL", exchange_code="XNAS")
    store = _FakeStore()
    store.rows[("model-a", 1200, prompt_sha256(prompt))] = _raw_response(estimated_tokens=321)
    inner = _FakeInner()
    client = CachedThesisLlmClient(inner=inner, cache=store)

    raw = client.analyze(model="model-a", prompt=prompt, max_output_tokens=1200)

    assert raw["ticker"] == "AAPL"
    assert raw["estimated_tokens"] == 0
    assert inner.calls == 0
    assert client.cache_hits == 1
    assert client.llm_calls == 0
    assert store.rows[("model-a", 1200, prompt_sha256(prompt))]["estimated_tokens"] == 321


def test_cached_client_does_not_store_when_inner_raises() -> None:
    store = _FakeStore()
    client = CachedThesisLlmClient(
        inner=_FakeInner(error=TimeoutError("request timed out")),
        cache=store,
    )

    with pytest.raises(TimeoutError):
        client.analyze(
            model="model-a",
            prompt=_prompt(article_id="article-1", ticker="AAPL", exchange_code="XNAS"),
            max_output_tokens=1200,
        )

    assert store.puts == []


def test_cached_client_key_is_sensitive_to_model_tokens_and_prompt() -> None:
    store = _FakeStore()
    prompt = _prompt(article_id="article-1", ticker="AAPL", exchange_code="XNAS")
    variant_prompt = _prompt(article_id="article-2", ticker="AAPL", exchange_code="XNAS")
    store.rows[("model-a", 1200, prompt_sha256(prompt))] = _raw_response(estimated_tokens=111)
    client = CachedThesisLlmClient(inner=_FakeInner(), cache=store)

    assert client.get_cached_analysis(model="model-a", prompt=prompt, max_output_tokens=1200)
    assert client.get_cached_analysis(model="model-b", prompt=prompt, max_output_tokens=1200) is None
    assert client.get_cached_analysis(model="model-a", prompt=prompt, max_output_tokens=900) is None
    assert client.get_cached_analysis(model="model-a", prompt=variant_prompt, max_output_tokens=1200) is None


def test_cached_client_caches_synthesis_prompt_with_candidate_support_columns() -> None:
    store = _FakeStore()
    inner = _FakeInner()
    client = CachedThesisLlmClient(inner=inner, cache=store)
    prompt = json.dumps(
        {
            "dossier": {
                "candidate": {
                    "ticker": "msft",
                    "exchange_code": "xnas",
                }
            }
        },
        sort_keys=True,
    )

    raw = client.analyze_synthesis(
        model="synthesis-model",
        prompt=prompt,
        max_output_tokens=700,
    )
    cached = client.analyze_synthesis(
        model="synthesis-model",
        prompt=prompt,
        max_output_tokens=700,
    )

    assert raw["estimated_tokens"] == 222
    assert cached["estimated_tokens"] == 0
    assert inner.calls == 1
    assert client.llm_calls == 1
    assert client.cache_hits == 1
    assert store.puts[0]["ticker"] == "MSFT"
    assert store.puts[0]["exchange_code"] == "XNAS"


def _prompt(*, article_id: str, ticker: str, exchange_code: str) -> str:
    return json.dumps(
        {
            "article": {"id": article_id},
            "instrument": {"ticker": ticker, "exchange_code": exchange_code},
        },
        sort_keys=True,
    )


def _raw_response(*, estimated_tokens: int) -> dict:
    return {
        "ticker": "AAPL",
        "exchange_code": "XNAS",
        "sentiment": 0.8,
        "relevance": 0.9,
        "urgency": "today",
        "suggested_action": "buy",
        "candidate_strategy": "event_driven",
        "direction": "buy",
        "confidence": 0.75,
        "reasoning": "Guidance improved.",
        "is_market_moving": True,
        "instrument_is_subject": True,
        "content_type": "news_catalyst",
        "evidence_bullet_candidates": ["Guidance improved."],
        "estimated_tokens": estimated_tokens,
    }
