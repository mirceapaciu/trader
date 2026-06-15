from __future__ import annotations

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
            ".env.secrets",
        ),
        override_existing=False,
    )
    settings = MonitoringUiSettings.from_env()
    uvicorn.run(
        "src.product_components.monitoring_ui.backend.app:app",
        host="127.0.0.1",
        port=settings.ui_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
