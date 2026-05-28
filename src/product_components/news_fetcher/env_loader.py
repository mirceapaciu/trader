from __future__ import annotations

import os
import re
from pathlib import Path

_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_files(
    repo_root: Path,
    *,
    filenames: tuple[str, ...] = (".env.shared", ".env.prod", ".env.news-fetcher"),
    override_existing: bool = False,
) -> list[Path]:
    """Load env vars from files in order, without overriding existing vars by default."""
    loaded_files: list[Path] = []

    for name in filenames:
        env_file = repo_root / name
        if not env_file.exists():
            continue

        _load_env_file(env_file, override_existing=override_existing)
        loaded_files.append(env_file)

    return loaded_files


def _load_env_file(path: Path, *, override_existing: bool) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_NAME_PATTERN.match(key):
            continue

        if not override_existing and key in os.environ:
            continue

        cleaned = value.strip().strip('"').strip("'")
        os.environ[key] = cleaned
