from __future__ import annotations

import re

from src.product_components.monitoring_ui.backend.app import _local_dev_origin_regex


def test_local_dev_origin_regex_allows_localhost_with_any_port() -> None:
    pattern = re.compile(_local_dev_origin_regex())

    assert pattern.match("http://localhost:5173")
    assert pattern.match("http://127.0.0.1:5174")
    assert pattern.match("https://localhost:8443")
    assert not pattern.match("http://example.com:5173")
