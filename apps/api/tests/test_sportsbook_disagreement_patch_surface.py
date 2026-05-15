"""Tests for Smarter #18 PATCH surface — sportsbook disagreement
threshold + min_book_count.

PR #106 (phase 2d) shipped the suppression rule with operator-side
writers (``set_sportsbook_disagreement_threshold`` /
``set_sportsbook_disagreement_min_book_count``) reachable only from a
Python REPL. Reviewer HIGH catch deferred the PATCH surface to a
follow-up; this PR is that follow-up.

Tested behaviors:
- ``ModelReadinessSummaryRead`` exposes the current (clamped)
  effective values via ``sportsbook_disagreement_threshold`` +
  ``sportsbook_disagreement_min_book_count``.
- ``ModelReadinessSettingsUpdate`` accepts both fields as optional
  partial-PATCH inputs.
- PATCH ``/ops/models/readiness/settings`` writes whichever fields
  the payload provides and leaves the others alone.
- Invalid values that pass schema validation still clamp at read
  time (the writer is permissive — see
  ``set_sportsbook_disagreement_threshold`` docstring) — operators
  see the clamp on the next read instead of a silent reject.
"""

from __future__ import annotations

from app.services.operator_settings import (
    DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT,
    DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD,
    effective_sportsbook_disagreement_min_book_count,
    effective_sportsbook_disagreement_threshold,
    set_sportsbook_disagreement_min_book_count,
    set_sportsbook_disagreement_threshold,
)
from app.services.ml.readiness import build_model_readiness_summary


# -- Readiness summary surface -----------------------------------------


def test_readiness_summary_exposes_default_threshold_when_unset(db_session) -> None:
    summary = build_model_readiness_summary(db_session)
    assert (
        summary["sportsbook_disagreement_threshold"]
        == DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD
    )
    assert (
        summary["sportsbook_disagreement_min_book_count"]
        == DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT
    )


def test_readiness_summary_exposes_persisted_threshold(db_session) -> None:
    set_sportsbook_disagreement_threshold(db_session, 0.10)
    set_sportsbook_disagreement_min_book_count(db_session, 5)
    db_session.commit()

    summary = build_model_readiness_summary(db_session)
    assert summary["sportsbook_disagreement_threshold"] == 0.10
    assert summary["sportsbook_disagreement_min_book_count"] == 5


# -- Schema acceptance --------------------------------------------------


def test_settings_update_schema_accepts_threshold_and_min_book_count() -> None:
    from app.schemas import ModelReadinessSettingsUpdate

    payload = ModelReadinessSettingsUpdate(
        sportsbook_disagreement_threshold=0.12,
        sportsbook_disagreement_min_book_count=4,
    )
    assert payload.sportsbook_disagreement_threshold == 0.12
    assert payload.sportsbook_disagreement_min_book_count == 4


def test_settings_update_schema_allows_partial_patch_omits_sportsbook_fields() -> None:
    """Phase 2d's PATCH surface follows the existing partial-PATCH
    idiom — None for any field means "don't touch this setting." A
    payload that only updates pick_history_default_n must NOT clobber
    a previously-set sportsbook threshold."""
    from app.schemas import ModelReadinessSettingsUpdate

    payload = ModelReadinessSettingsUpdate(pick_history_default_n=10)
    assert payload.sportsbook_disagreement_threshold is None
    assert payload.sportsbook_disagreement_min_book_count is None


def test_settings_update_schema_rejects_threshold_outside_unit_interval() -> None:
    """Schema-level validation catches obvious typos before the writer
    sees them. The writer is permissive (accepts any numeric and clamps
    at read time) but the API surface is the operator's first line of
    feedback — reject 1.5 / -0.1 / etc. with a clear validation error."""
    import pytest
    from pydantic import ValidationError

    from app.schemas import ModelReadinessSettingsUpdate

    with pytest.raises(ValidationError):
        ModelReadinessSettingsUpdate(sportsbook_disagreement_threshold=1.5)
    with pytest.raises(ValidationError):
        ModelReadinessSettingsUpdate(sportsbook_disagreement_threshold=-0.1)


def test_settings_update_schema_rejects_min_book_count_below_one() -> None:
    import pytest
    from pydantic import ValidationError

    from app.schemas import ModelReadinessSettingsUpdate

    with pytest.raises(ValidationError):
        ModelReadinessSettingsUpdate(sportsbook_disagreement_min_book_count=0)
    with pytest.raises(ValidationError):
        ModelReadinessSettingsUpdate(sportsbook_disagreement_min_book_count=-3)


# -- PATCH endpoint integration ----------------------------------------


def test_patch_writes_threshold_and_min_book_count(client, db_session) -> None:
    response = client.patch(
        "/ops/models/readiness/settings",
        json={
            "sportsbook_disagreement_threshold": 0.08,
            "sportsbook_disagreement_min_book_count": 7,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sportsbook_disagreement_threshold"] == 0.08
    assert body["sportsbook_disagreement_min_book_count"] == 7
    # And the values are actually persisted (next read returns them).
    assert effective_sportsbook_disagreement_threshold(db_session) == 0.08
    assert effective_sportsbook_disagreement_min_book_count(db_session) == 7


def test_patch_partial_leaves_other_sportsbook_setting_untouched(
    client, db_session,
) -> None:
    set_sportsbook_disagreement_threshold(db_session, 0.10)
    set_sportsbook_disagreement_min_book_count(db_session, 5)
    db_session.commit()

    response = client.patch(
        "/ops/models/readiness/settings",
        json={"sportsbook_disagreement_min_book_count": 8},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sportsbook_disagreement_min_book_count"] == 8
    # threshold untouched.
    assert body["sportsbook_disagreement_threshold"] == 0.10
    assert effective_sportsbook_disagreement_threshold(db_session) == 0.10
    assert effective_sportsbook_disagreement_min_book_count(db_session) == 8


def test_patch_omitting_sportsbook_fields_doesnt_clobber_existing_values(
    client, db_session,
) -> None:
    """A PATCH that only changes pick_history_default_n must leave a
    previously-set sportsbook threshold AND min_book_count alone."""
    set_sportsbook_disagreement_threshold(db_session, 0.07)
    set_sportsbook_disagreement_min_book_count(db_session, 6)
    db_session.commit()

    response = client.patch(
        "/ops/models/readiness/settings",
        json={"pick_history_default_n": 10},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sportsbook_disagreement_threshold"] == 0.07
    assert body["sportsbook_disagreement_min_book_count"] == 6
