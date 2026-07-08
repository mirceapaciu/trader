from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.product_components.thesis_builder.llm_client import ThesisLlmClient

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LlmAnalysisCacheStore(Protocol):
    def get(
        self, *, llm_model: str, max_output_tokens: int, prompt_sha256: str
    ) -> dict[str, Any] | None:
        """Return the cached raw response JSON, if present."""

    def put(
        self,
        *,
        llm_model: str,
        max_output_tokens: int,
        prompt_sha256: str,
        response_json: dict[str, Any],
        article_id: str | None,
        ticker: str | None,
        exchange_code: str | None,
    ) -> None:
        """Persist a raw response JSON for future regeneration runs."""


@dataclass
class CachedThesisLlmClient:
    """Cache wrapper for deterministic regeneration LLM article analyses."""

    inner: ThesisLlmClient
    cache: LlmAnalysisCacheStore
    cache_hits: int = 0
    llm_calls: int = 0

    def get_cached_analysis(
        self, *, model: str, prompt: str, max_output_tokens: int
    ) -> dict[str, Any] | None:
        cached = self.cache.get(
            llm_model=model,
            max_output_tokens=max_output_tokens,
            prompt_sha256=prompt_sha256(prompt),
        )
        if cached is None:
            return None
        self.cache_hits += 1
        return _budget_free_copy(cached)

    def analyze(
        self, *, model: str, prompt: str, max_output_tokens: int
    ) -> dict[str, Any]:
        cached = self.get_cached_analysis(
            model=model, prompt=prompt, max_output_tokens=max_output_tokens
        )
        if cached is not None:
            return cached

        raw = self.inner.analyze(
            model=model, prompt=prompt, max_output_tokens=max_output_tokens
        )
        if not isinstance(raw, dict):
            raise ValueError("thesis_response_not_object")
        self.llm_calls += 1
        support = _extract_support_columns(prompt)
        self.cache.put(
            llm_model=model,
            max_output_tokens=max_output_tokens,
            prompt_sha256=prompt_sha256(prompt),
            response_json=dict(raw),
            article_id=support["article_id"],
            ticker=support["ticker"],
            exchange_code=support["exchange_code"],
        )
        return raw


@dataclass(frozen=True)
class PostgresLlmAnalysisCache:
    dsn: str
    backtester_schema: str = "backtester"

    def __post_init__(self) -> None:
        _safe_identifier(self.backtester_schema)

    def get(
        self, *, llm_model: str, max_output_tokens: int, prompt_sha256: str
    ) -> dict[str, Any] | None:
        sql = (
            f"UPDATE {self.backtester_schema}.t_llm_analysis_cache "
            f"SET last_used_at = NOW() "
            f"WHERE llm_model = %s AND max_output_tokens = %s AND prompt_sha256 = %s "
            f"RETURNING response_json"
        )
        with closing(psycopg.connect(self.dsn)) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, (llm_model, max_output_tokens, prompt_sha256))
                row = cursor.fetchone()
                connection.commit()
        if row is None:
            return None
        return dict(row["response_json"])

    def put(
        self,
        *,
        llm_model: str,
        max_output_tokens: int,
        prompt_sha256: str,
        response_json: dict[str, Any],
        article_id: str | None,
        ticker: str | None,
        exchange_code: str | None,
    ) -> None:
        sql = (
            f"INSERT INTO {self.backtester_schema}.t_llm_analysis_cache "
            f"(llm_model, max_output_tokens, prompt_sha256, article_id, ticker, exchange_code, response_json) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (llm_model, max_output_tokens, prompt_sha256) DO UPDATE SET "
            f"article_id = EXCLUDED.article_id, "
            f"ticker = EXCLUDED.ticker, "
            f"exchange_code = EXCLUDED.exchange_code, "
            f"response_json = EXCLUDED.response_json, "
            f"last_used_at = NOW()"
        )
        with closing(psycopg.connect(self.dsn)) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql,
                    (
                        llm_model,
                        max_output_tokens,
                        prompt_sha256,
                        article_id,
                        ticker,
                        exchange_code,
                        Json(response_json),
                    ),
                )
                connection.commit()


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _budget_free_copy(raw: dict[str, Any]) -> dict[str, Any]:
    copied = dict(raw)
    copied["estimated_tokens"] = 0
    return copied


def _extract_support_columns(prompt: str) -> dict[str, str | None]:
    try:
        payload = json.loads(prompt)
    except json.JSONDecodeError:
        return {"article_id": None, "ticker": None, "exchange_code": None}
    article = payload.get("article") if isinstance(payload, dict) else None
    instrument = payload.get("instrument") if isinstance(payload, dict) else None
    return {
        "article_id": str(article.get("id")) if isinstance(article, dict) and article.get("id") else None,
        "ticker": str(instrument.get("ticker")).upper()
        if isinstance(instrument, dict) and instrument.get("ticker")
        else None,
        "exchange_code": str(instrument.get("exchange_code")).upper()
        if isinstance(instrument, dict) and instrument.get("exchange_code")
        else None,
    }


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value
