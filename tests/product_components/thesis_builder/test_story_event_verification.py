"""Unit tests for the story-assignment verification band logic (260715-05 / 260716-01).

Covers the three-band decision in `_verify_story_assignment_target`:
  overlap == 0  -> deterministic downgrade
  overlap >= 2  -> deterministic pass
  overlap == 1  -> ambiguous band, consult the LLM event check (fail open to pass)
and the fail-safe behaviour of `ThesisStoryAssigner.confirm_same_event`.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.product_components.thesis_builder.llm_client import ThesisStoryAssigner
from src.product_components.thesis_builder.repository import _verify_story_assignment_target


def _article(headline: str, summary: str = "") -> SimpleNamespace:
    return SimpleNamespace(headline=headline, summary=summary, source="rss")


def _result(ticker: str = "NVDA") -> SimpleNamespace:
    return SimpleNamespace(ticker=ticker, event_type=None, evidence_bullet_candidates=[])


def _verify(headline: str, narrative: str, confirmer):
    return _verify_story_assignment_target(
        article=_article(headline),
        result=_result(),
        target="window:1",
        narrative=f"Headline: {narrative}",
        event_confirmer=confirmer,
    )


def test_zero_overlap_downgrades_and_skips_confirmer():
    calls = []

    def confirmer(_narrative):
        calls.append(_narrative)
        return True

    out = _verify("zeta widget alpha", "omega parcel gamma", confirmer)
    assert out["resolved_target"] == "new_story"
    assert out["verification_status"] == "downgraded"
    assert out["verification_reason_code"] == "story_text_mismatch"
    assert calls == []  # deterministic; the LLM is not consulted on zero overlap


def test_strong_overlap_passes_and_skips_confirmer():
    calls = []

    def confirmer(_narrative):
        calls.append(_narrative)
        return False

    out = _verify("quantum omega widget", "quantum omega parcel", confirmer)
    assert out["resolved_target"] == "window:1"
    assert out["verification_status"] == "passed"
    assert calls == []  # >=2 shared tokens is deterministic; the LLM is not consulted


def test_single_overlap_confirmer_false_downgrades():
    out = _verify("quantum widget alpha", "quantum parcel gamma", lambda _n: False)
    assert out["resolved_target"] == "new_story"
    assert out["verification_status"] == "downgraded"
    assert out["verification_reason_code"] == "story_event_mismatch"
    assert out["verification_details"]["event_check"] == "different"
    assert out["verification_details"]["overlap"] == ["quantum"]


def test_single_overlap_confirmer_true_passes():
    out = _verify("quantum widget alpha", "quantum parcel gamma", lambda _n: True)
    assert out["resolved_target"] == "window:1"
    assert out["verification_status"] == "passed"
    assert out["verification_details"]["event_check"] == "same"


def test_single_overlap_no_confirmer_fails_open_to_pass():
    # Pre-260716-01 behaviour: a single-token overlap passes when no event check is available.
    out = _verify("quantum widget alpha", "quantum parcel gamma", None)
    assert out["resolved_target"] == "window:1"
    assert out["verification_status"] == "passed"
    assert "event_check" not in out["verification_details"]


def test_single_overlap_confirmer_none_fails_open_to_pass():
    out = _verify("quantum widget alpha", "quantum parcel gamma", lambda _n: None)
    assert out["resolved_target"] == "window:1"
    assert out["verification_status"] == "passed"


class _FakeClient:
    def __init__(self, response=None, raises=False):
        self._response = response
        self._raises = raises
        self.calls = 0

    def confirm_story_event(self, *, model, prompt, max_output_tokens):
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return dict(self._response)


def _assigner(client, *, enabled=True):
    return ThesisStoryAssigner(
        client=client,
        model="m",
        max_tokens_per_run=1_000_000,
        max_tokens_per_item=40,
        event_check_enabled=enabled,
    )


_ART = SimpleNamespace(headline="h", summary="s", source="rss")
_RES = SimpleNamespace(event_type=None, evidence_bullet_candidates=[])


def test_confirm_same_event_true():
    a = _assigner(_FakeClient({"same_event": True, "estimated_tokens": 5}))
    assert a.confirm_same_event(article=_ART, analysis=_RES, narrative="n") is True


def test_confirm_same_event_false():
    a = _assigner(_FakeClient({"same_event": False, "estimated_tokens": 5}))
    assert a.confirm_same_event(article=_ART, analysis=_RES, narrative="n") is False


def test_confirm_disabled_returns_none_without_calling_client():
    client = _FakeClient({"same_event": False, "estimated_tokens": 5})
    a = _assigner(client, enabled=False)
    assert a.confirm_same_event(article=_ART, analysis=_RES, narrative="n") is None
    assert client.calls == 0


def test_confirm_transport_error_returns_none():
    a = _assigner(_FakeClient(raises=True))
    assert a.confirm_same_event(article=_ART, analysis=_RES, narrative="n") is None


def test_confirm_unparseable_returns_none():
    a = _assigner(_FakeClient({"unexpected": 1, "estimated_tokens": 5}))
    assert a.confirm_same_event(article=_ART, analysis=_RES, narrative="n") is None
