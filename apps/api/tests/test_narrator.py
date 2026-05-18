"""Tests for Smarter #31 — grounded LLM narrator.

Covers:
- ``verify_narration`` rejects forbidden topics (injuries, refs, etc.),
  ungrounded numeric claims, and unrecognized player / team names.
- ``generate_narration`` short-circuits with ``NarratorNotConfiguredError``
  when ``OPENAI_API_KEY`` is empty.
- HTTP-client stub exercises the OpenAI chat-completions call shape +
  end-to-end flow including verifier rejection.
- Operator-settings toggle round-trips via ``effective_narrator_enabled``
  / ``set_narrator_enabled``.
- The ``/ops/recommendations/{id}/narrator`` endpoint enforces the
  toggle and 503s when missing key / off.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import get_settings
from app.services.narrator import (
    NarratorNotConfiguredError,
    NarratorOutput,
    generate_narration,
    verify_narration,
)
from app.services.operator_settings import (
    effective_narrator_enabled,
    set_narrator_enabled,
)


# -- verify_narration: forbidden topics -------------------------------


_BASE_METADATA: dict[str, Any] = {
    "subject_name": "Jayson Tatum",
    "subject_team": "Boston Celtics",
    "event_name": "Boston Celtics @ Brooklyn Nets",
    "side": "yes",
    "threshold": 25.5,
    "edge": 0.05,
    "confidence": 0.66,
    "market_title": "Tatum 25.5 Points",
    "market_family": "player_prop",
}


_BASE_FEATURES: dict[str, Any] = {
    "recent_10_average": 28.3,
    "season_average": 26.1,
    "opponent_def_rating_recent_5": 112.0,
    "recent_usage_pct": 0.32,
    "recent_workload_minutes_per_game": 36.0,
    "threshold": 25.5,
    "confidence": 0.66,
    "edge": 0.05,
}


def _verify(text: str, *, features=None, metadata=None) -> tuple[bool, list[str]]:
    return verify_narration(
        text,
        features=features or _BASE_FEATURES,
        recommendation_metadata=metadata or _BASE_METADATA,
    )


def test_verifier_passes_on_well_grounded_text() -> None:
    text = (
        "Tatum has been averaging 28.3 points over his last 10 games, "
        "well above the 26.1 season pace. Brooklyn's defensive rating "
        "of 112 grades the matchup as favorable for offense."
    )
    passed, rejected = _verify(text)
    assert passed is True, f"unexpected rejections: {rejected}"


def test_verifier_rejects_injury_mentions() -> None:
    text = "Tatum is dealing with a minor injury but should still play."
    passed, rejected = _verify(text)
    assert passed is False
    assert any("forbidden_phrase:injury" in r for r in rejected)


def test_verifier_rejects_referee_mentions() -> None:
    text = "Tony Brothers' crew chief reputation supports an over on this pick."
    passed, rejected = _verify(text)
    assert passed is False
    assert any("forbidden_phrase:crew chief" in r for r in rejected)


def test_verifier_rejects_weather_mentions() -> None:
    text = "Heavy rain at the venue suggests a defensive game."
    passed, rejected = _verify(text)
    assert passed is False
    assert any("forbidden_phrase:rain" in r for r in rejected)


def test_verifier_rejects_trade_mentions() -> None:
    text = "Tatum's been more aggressive since the trade-deadline moves."
    passed, rejected = _verify(text)
    assert passed is False
    assert any("forbidden_phrase:trade" in r for r in rejected)


def test_verifier_rejects_rivalry_narrative() -> None:
    text = "Tatum has historically risen to the occasion against this rivalry."
    passed, rejected = _verify(text)
    assert passed is False
    assert any("forbidden_phrase:rivalry" in r for r in rejected)


# -- verify_narration: ungrounded numeric claims ----------------------


def test_verifier_rejects_ungrounded_numeric_claim() -> None:
    text = "Tatum is shooting 47.5% from three this season, a career high."
    # 47.5 doesn't appear in any feature.
    passed, rejected = _verify(text)
    assert passed is False
    assert any("ungrounded_number:47.5" in r for r in rejected)


def test_verifier_passes_within_rounding_tolerance() -> None:
    # 28.3 in features; 28.0 in text should be accepted (tolerance ±0.6).
    text = "Recent stretch around 28.0 points per game keeps him projected over."
    passed, rejected = _verify(text)
    assert passed is True, f"unexpected rejections: {rejected}"


def test_verifier_ignores_small_integer_numerals() -> None:
    # "3 of 5" and "2 nights" etc. shouldn't trip the gate.
    text = "Recent 10-game average of 28.3 points sits well above the 25.5 line."
    passed, rejected = _verify(text)
    assert passed is True


def test_verifier_accepts_threshold_from_metadata() -> None:
    # 25.5 isn't in features dict — only in metadata. Verifier should
    # still accept it because the threshold is a recognized numeric.
    text = "The 25.5 line looks soft given the season pace."
    passed, rejected = _verify(text)
    assert passed is True, f"unexpected rejections: {rejected}"


# -- verify_narration: unrecognized names -----------------------------


def test_verifier_rejects_unknown_player_name() -> None:
    text = (
        "Tatum's recent average of 28.3 holds up well against Brooklyn. "
        "Watch for Mikal Bridges as the primary defender."
    )
    # Mikal Bridges isn't in the metadata.
    passed, rejected = _verify(text)
    assert passed is False
    assert any("unrecognized_name:Mikal Bridges" in r for r in rejected)


def test_verifier_accepts_metadata_names() -> None:
    text = (
        "Tatum has been averaging 28.3 points. Boston Celtics offense ranks well "
        "against Brooklyn Nets defense."
    )
    passed, rejected = _verify(text)
    assert passed is True, f"unexpected rejections: {rejected}"


# -- generate_narration: configuration short-circuit -------------------


def test_generate_raises_when_api_key_empty(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(NarratorNotConfiguredError):
        generate_narration(features=_BASE_FEATURES, recommendation_metadata=_BASE_METADATA)
    get_settings.cache_clear()


# -- generate_narration: stubbed HTTP flow ----------------------------


class _StubHttpClient:
    def __init__(self, *, response_text: str, status_code: int = 200) -> None:
        self.response_text = response_text
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(
            {"url": url, "headers": kwargs.get("headers"), "json": kwargs.get("json")}
        )
        return httpx.Response(
            status_code=self.status_code,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": self.response_text}}
                ]
            },
            request=httpx.Request("POST", url),
        )


def test_generate_calls_openai_chat_completions_with_grounded_prompt(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    get_settings.cache_clear()
    stub = _StubHttpClient(
        response_text=(
            "Tatum has been averaging 28.3 points over his last 10 games, above the "
            "26.1 season pace. Brooklyn's defensive rating of 112 grades as soft."
        )
    )
    output = generate_narration(
        features=_BASE_FEATURES,
        recommendation_metadata=_BASE_METADATA,
        http_client=stub,
    )
    assert isinstance(output, NarratorOutput)
    assert output.verifier_passed is True
    assert "28.3" in output.text
    # Verify the call shape.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["url"].endswith("/chat/completions")
    assert call["headers"]["Authorization"] == "Bearer test-key"
    body = call["json"]
    assert body["model"] == "gpt-4o-mini"
    assert any("MUST NOT" in m["content"] for m in body["messages"] if m["role"] == "system")
    get_settings.cache_clear()


def test_generate_records_verifier_rejection_when_output_mentions_injury(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    get_settings.cache_clear()
    stub = _StubHttpClient(
        response_text="Tatum's recent 28.3-point average suggests, despite his injury, an over.",
    )
    output = generate_narration(
        features=_BASE_FEATURES,
        recommendation_metadata=_BASE_METADATA,
        http_client=stub,
    )
    assert output.verifier_passed is False
    assert any("forbidden_phrase:injury" in r for r in output.rejected_claims)
    get_settings.cache_clear()


def test_generate_propagates_http_status_error_on_4xx(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    get_settings.cache_clear()
    stub = _StubHttpClient(response_text="", status_code=401)
    with pytest.raises(httpx.HTTPStatusError):
        generate_narration(
            features=_BASE_FEATURES,
            recommendation_metadata=_BASE_METADATA,
            http_client=stub,
        )
    get_settings.cache_clear()


# -- operator-settings toggle round-trip ------------------------------


def test_narrator_toggle_defaults_to_off(db_session) -> None:
    assert effective_narrator_enabled(db_session) is False


def test_narrator_toggle_round_trips_true_then_false(db_session) -> None:
    set_narrator_enabled(db_session, True)
    db_session.commit()
    assert effective_narrator_enabled(db_session) is True
    set_narrator_enabled(db_session, False)
    db_session.commit()
    assert effective_narrator_enabled(db_session) is False


def test_narrator_toggle_is_idempotent(db_session) -> None:
    set_narrator_enabled(db_session, True)
    set_narrator_enabled(db_session, True)  # re-applying True
    db_session.commit()
    assert effective_narrator_enabled(db_session) is True


# -- endpoint behavior ------------------------------------------------


def test_endpoint_503s_when_toggle_off(client, db_session) -> None:
    # Default toggle is False.
    response = client.post("/ops/recommendations/9999/narrator")
    assert response.status_code == 503
    assert "toggle is OFF" in response.json()["detail"]


def test_endpoint_404s_when_toggle_on_but_recommendation_missing(client, db_session) -> None:
    set_narrator_enabled(db_session, True)
    db_session.commit()
    response = client.post("/ops/recommendations/9999/narrator")
    assert response.status_code == 404


def test_readiness_summary_surfaces_narrator_enabled(client, db_session) -> None:
    set_narrator_enabled(db_session, True)
    db_session.commit()
    response = client.get("/ops/models/readiness")
    assert response.status_code == 200
    assert response.json()["narrator_enabled"] is True


def test_readiness_settings_patch_accepts_narrator_enabled(client, db_session) -> None:
    response = client.patch(
        "/ops/models/readiness/settings",
        json={"narrator_enabled": True, "enqueue_shadow_backfill": False},
    )
    # Bug #235 — PATCH returns a lightweight ack; the persisted toggle
    # surfaces on the next GET (and via the writer assertion below).
    assert response.status_code == 200
    assert response.json() == {"applied": True}
    assert effective_narrator_enabled(db_session) is True
