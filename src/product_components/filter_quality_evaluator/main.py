from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.news_fetcher.settings import NewsFetcherSettings

from .models import FilterQualityRunParams
from .repository import FilterQualityRepository
from .service import FilterQualityEvaluatorService, new_run_id
from .settings import FilterQualityEvaluatorSettings

LOGGER = logging.getLogger("filter_quality_evaluator")


def main() -> None:
    repo_root = _repo_root()
    load_env_files(
        repo_root,
        filenames=(
            ".env.shared",
            ".env.prod",
            ".env.news-fetcher",
            ".env.filter-quality-evaluator",
            ".env.secrets",
        ),
        override_existing=False,
    )
    args = _parse_args()
    settings = FilterQualityEvaluatorSettings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repository = FilterQualityRepository(
        dsn=settings.postgres_dsn,
        evaluator_schema=settings.db_schema,
        news_schema=settings.newsfetcher_db_schema,
    )
    repository.bootstrap_schema(repo_root=repo_root)
    params = FilterQualityRunParams(
        run_id=args.run_id or new_run_id(),
        news_window_start_at=_parse_utc(args.news_window_start_at),
        news_window_end_at=_parse_utc(args.news_window_end_at),
        filter_config_fingerprint=args.filter_config_fingerprint,
        filter_config_snapshot_json=_load_snapshot(args.filter_config_snapshot_json),
        run_note=args.run_note,
        accepted_audit_enabled=args.accepted_audit_enabled,
        accepted_audit_sample_size=args.accepted_audit_sample_size,
    )
    LOGGER.info("starting filter quality evaluator run_id=%s", params.run_id)
    FilterQualityEvaluatorService(
        settings=settings,
        news_settings=NewsFetcherSettings.from_env(),
        repository=repository,
    ).run(params)
    print(json.dumps({"run_id": params.run_id, "status": "completed"}, sort_keys=True))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="filter_quality_evaluator")
    parser.add_argument("--run-id")
    parser.add_argument("--news-window-start-at", required=True)
    parser.add_argument("--news-window-end-at", required=True)
    parser.add_argument("--filter-config-fingerprint")
    parser.add_argument("--filter-config-snapshot-json")
    parser.add_argument("--run-note")
    parser.add_argument("--accepted-audit-enabled", action="store_true")
    parser.add_argument("--accepted-audit-sample-size", type=int)
    return parser.parse_args()


def _load_snapshot(value: str | None) -> dict[str, Any] | None:
    if value is None or not value.strip():
        return None
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def _parse_utc(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    main()
