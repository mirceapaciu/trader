from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _load_integration_env_files() -> None:
    """Load shared defaults and test overrides for integration test runs."""
    repo_root = Path(__file__).resolve().parents[2]
    _load_env_file(repo_root / ".env.shared")
    _load_env_file(repo_root / ".env.test")


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value
