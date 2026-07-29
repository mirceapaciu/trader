from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.product_components.monitoring_ui.backend.app import _trusted_taxonomy_actor


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request({"type": "http", "headers": headers})


def test_taxonomy_mutation_is_disabled_by_default():
    settings = SimpleNamespace(
        taxonomy_decisions_enabled=False,
        taxonomy_trusted_actor_header="X-Forwarded-User",
    )

    with pytest.raises(HTTPException) as caught:
        _trusted_taxonomy_actor(request=_request([]), settings=settings)

    assert caught.value.status_code == 503
    assert caught.value.detail == "taxonomy_decisions_disabled"


def test_taxonomy_actor_comes_only_from_configured_trusted_header():
    settings = SimpleNamespace(
        taxonomy_decisions_enabled=True,
        taxonomy_trusted_actor_header="X-Verified-Principal",
    )
    request = _request(
        [
            (b"x-forwarded-user", b"untrusted"),
            (b"x-verified-principal", b"alice@example.test"),
        ]
    )

    assert _trusted_taxonomy_actor(request=request, settings=settings) == "alice@example.test"


def test_enabled_mutation_rejects_missing_trusted_identity():
    settings = SimpleNamespace(
        taxonomy_decisions_enabled=True,
        taxonomy_trusted_actor_header="X-Verified-Principal",
    )

    with pytest.raises(HTTPException) as caught:
        _trusted_taxonomy_actor(request=_request([]), settings=settings)

    assert caught.value.status_code == 401
