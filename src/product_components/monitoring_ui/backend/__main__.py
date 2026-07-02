from __future__ import annotations

import logging

import uvicorn

from src.product_components.news_fetcher.env_loader import load_env_files

from .settings import MonitoringUiSettings


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[4]


def main() -> None:
    load_env_files(
        _repo_root(),
        filenames=(
            ".env.shared",
            ".env.prod",
            ".env.monitoring-ui",
            ".env.news-fetcher",
            ".env.filter-quality-evaluator",
            ".env.thesis-builder",
            ".env.backtester",
            ".env.secrets",
        ),
        override_existing=False,
    )
    settings = MonitoringUiSettings.from_env()
    _root = logging.getLogger()
    _root.setLevel(logging.INFO)
    if not _root.handlers:
        import sys
        _handler = logging.StreamHandler(sys.stdout)
        _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        _root.addHandler(_handler)
    uvicorn.run(
        "src.product_components.monitoring_ui.backend.app:app",
        host="127.0.0.1",
        port=settings.ui_port,
        reload=False,
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "%(asctime)s %(levelprefix)s %(message)s",
                    "use_colors": False,
                },
                "access": {
                    "()": "uvicorn.logging.AccessFormatter",
                    "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                    "use_colors": False,
                },
            },
            "handlers": {
                "default": {
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
                "access": {
                    "formatter": "access",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"level": "INFO"},
                "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
                # Application loggers for in-process work (e.g. the regeneration
                # backtest) must reach the log file via the root handler; without a
                # root entry uvicorn's dictConfig leaves these records with nowhere
                # to go and they are silently dropped.
                "backtester": {"level": "INFO"},
                "thesis_builder": {"level": "INFO"},
                "market_data": {"level": "INFO"},
            },
            "root": {"handlers": ["default"], "level": "INFO"},
        },
    )


if __name__ == "__main__":
    main()
