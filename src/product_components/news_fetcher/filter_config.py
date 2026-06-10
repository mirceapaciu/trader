from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from src.core_components.event_ingestion_engine.models import FilterRun, FilterRunMode


@dataclass(frozen=True)
class NewsFilterConfig:
    filter_config_id: str
    config_name: str
    config_role: str
    status: str
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    watchlist_tickers: tuple[str, ...]
    dedupe_algorithm: str
    dedupe_similarity_threshold: float
    dedupe_lookback_hours: int
    created_from_run_id: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "include_keywords": list(self.include_keywords),
            "exclude_keywords": list(self.exclude_keywords),
            "watchlist_tickers": list(self.watchlist_tickers),
            "dedupe_algorithm": self.dedupe_algorithm,
            "dedupe_similarity_threshold": self.dedupe_similarity_threshold,
            "dedupe_lookback_hours": self.dedupe_lookback_hours,
        }

    def fingerprint(self) -> str:
        normalized = json.dumps(self.snapshot(), separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def production_filter_run(self) -> FilterRun:
        fingerprint = self.fingerprint()
        return FilterRun(
            filter_run_id=f"prod_{fingerprint[:24]}",
            run_mode=FilterRunMode.PRODUCTION,
            filter_config_fingerprint=fingerprint,
            filter_config_snapshot_json=self.snapshot(),
        )


def new_filter_config_id(prefix: str = "nfc") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def normalize_keywords(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_dedupe_normalized(values, lower=True))


def normalize_tickers(values: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    return tuple(_dedupe_normalized(values, lower=False))


def config_from_snapshot(
    snapshot: dict[str, Any],
    *,
    filter_config_id: str,
    config_name: str,
    config_role: str,
    status: str,
    created_from_run_id: str | None = None,
) -> NewsFilterConfig:
    return NewsFilterConfig(
        filter_config_id=filter_config_id,
        config_name=config_name,
        config_role=config_role,
        status=status,
        include_keywords=normalize_keywords(_string_list(snapshot.get("include_keywords"))),
        exclude_keywords=normalize_keywords(_string_list(snapshot.get("exclude_keywords"))),
        watchlist_tickers=normalize_tickers(_string_list(snapshot.get("watchlist_tickers"))),
        dedupe_algorithm=str(snapshot.get("dedupe_algorithm") or "rapidfuzz_ratio"),
        dedupe_similarity_threshold=float(snapshot.get("dedupe_similarity_threshold", 0.9)),
        dedupe_lookback_hours=int(snapshot.get("dedupe_lookback_hours", 24)),
        created_from_run_id=created_from_run_id,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe_normalized(values, *, lower: bool) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if lower:
            normalized = normalized.lower()
        else:
            normalized = normalized.upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
