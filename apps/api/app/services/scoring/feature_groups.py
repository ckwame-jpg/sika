"""Architecture #5 — feature freshness layer.

Before this module: each feature emitter dumped its outputs into a
flat ``features: dict[str, Any]`` via ``features.update(emit_X(...))``.
Scoring read from that dict with no idea whether a value was fresh
from a refresh 5 min ago, served from a 6-hour-stale cache, or
defaulted to zero because the cache missed entirely. The result was
inconsistent ``*_data_complete`` markers and "missing context"
penalties operators couldn't explain.

This module replaces the flat dict with a **group-level snapshot**
structure: every emitter's outputs land in a ``FeatureGroupSnapshot``
that carries ``fresh_at`` / ``source`` / ``completeness`` metadata
alongside the values. The scoring kernel still reads from a flat
``features`` dict — but that dict is now **derived** from the
feature_groups via :func:`features_view`, not maintained separately.
This is the "pure A" shape per the Architecture #5 design discussion:
single source of truth in-process, with the persistence layer
dual-writing for backward compat with historical prediction rows + the
ML training pipeline.

## The freshness policy registry

Each group is mapped to a :class:`FeatureGroupPolicy` describing what
scoring should do when the group is stale (older than its TTL):

- ``SUPPRESS`` — drop the recommendation entirely. Used for groups
  where stale data is actively misleading (the policy registry today
  uses bespoke kernel paths for these; see notes below).
- ``PENALIZE`` — reduce confidence by a configurable delta. Used for
  groups where stale data is wrong-but-not-catastrophic (weather,
  bullpen rest, workload).
- ``IGNORE`` — no scoring impact; surface in diagnostics only. Used
  for season-stable aggregates (park factors, player season stats,
  referee tendencies), groups gated by bespoke kernel logic
  (nba_injury / mlb_lineup / mlb_starter), and toggle-gated advisory
  groups (sportsbook_consensus).

Unknown groups fall through to ``DEFAULT_POLICY`` (IGNORE) so adding
a new emitter without a registry entry doesn't accidentally gate
scoring — the operator has to opt in.

## SUPPRESS-policy callbacks (Architecture #5 follow-up 2)

Two groups now consolidate their bespoke suppression logic into the
unified registry as SUPPRESS-policy entries with ``suppress_when``
callbacks:

- **nba_injury** — Smarter #17. :func:`nba_injury_suppress_when` fires
  on OUT / DOUBTFUL when the injury report is fresh (within 12h);
  gated to ``nba_props``.
- **mlb_lineup** — Smarter #16. :func:`mlb_lineup_suppress_when` fires
  on confirmed-and-scratched (lineup data complete AND player not in
  starting lineup); gated to props that set ``copilot_requires_lineup``.

Both callbacks are invoked via :func:`check_suppressions` at scoring
time and surface group-keyed suppression reasons. The scoring kernel
maps those reasons onto its existing intermediate diagnostic keys
(``lineup_suppression_reason``, ``injury_suppression_reason``) so the
downstream ``suppression_reasons`` translation and the operator-facing
``scoring_diagnostics`` shape stay byte-compatible.

**mlb_starter** is still bespoke (``has_probable_starter_context``
missing-context gate plus graceful degradation on partial data) and
not consolidated here — its policy stays IGNORE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable


class FeatureGroupSeverity(str, Enum):
    """How the scoring kernel responds when a group is stale."""

    SUPPRESS = "suppress"
    PENALIZE = "penalize"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class SuppressionContext:
    """Runtime context passed to a SUPPRESS-policy group's
    ``suppress_when`` callback at scoring time.

    Callbacks inspect the runtime feature values + market metadata +
    scoring family to decide whether the recommendation should be
    suppressed (bespoke gates were value-driven, not just staleness-
    driven — e.g. mlb_lineup suppresses on "lineup confirmed AND player
    not in starting lineup", which depends on the lineup payload
    contents, not just its age).

    Frozen so the callback can't mutate the kernel's context mid-pass.
    """

    features: dict[str, Any]
    metadata: dict[str, Any]
    family_key: str


SuppressWhenFn = Callable[[SuppressionContext], "str | None"]


@dataclass(frozen=True, slots=True)
class FeatureGroupPolicy:
    """Per-group freshness policy. Operators tune by editing
    :data:`FEATURE_GROUP_POLICIES`; scoring reads via
    :func:`policy_for_group`."""

    severity: FeatureGroupSeverity
    ttl: timedelta
    # Only meaningful when ``severity == PENALIZE``. Confidence delta
    # applied when the group is stale; should be negative.
    penalty_confidence_delta: float = 0.0
    # Only meaningful when ``severity == SUPPRESS``. Invoked at scoring
    # time by :func:`check_suppressions`; receives a
    # :class:`SuppressionContext` and returns the suppression reason
    # string (which lands in ``suppression_reasons``) or ``None`` when
    # the group's current state doesn't warrant suppression. The bespoke
    # gates Smarter #16 / #17 implement here are value-driven (lineup
    # confirmed-and-scratched, injury OUT/DOUBTFUL with fresh report)
    # rather than purely TTL-driven, so the callback gets the live
    # features + metadata + family_key context the inline gates used.
    suppress_when: SuppressWhenFn | None = None


@dataclass(frozen=True, slots=True)
class FeatureGroupSnapshot:
    """The freshness-aware contents of one feature group.

    Replaces the flat ``features.update({...})`` pattern. ``values``
    carries the actual feature key-value pairs (same shape they had
    before the migration). The metadata fields describe where the
    data came from, when it was last refreshed, and how complete the
    group is — so the kernel can make per-group freshness decisions
    and operators can audit provenance.

    Frozen + slots: snapshots are immutable per scoring pass. Emitters
    construct a new snapshot per call; the kernel reads but never
    mutates.
    """

    group_key: str
    values: dict[str, Any] = field(default_factory=dict)
    # When the underlying data was last refreshed. ``None`` for
    # derived feature groups (interaction terms, schedule-density
    # signals) that don't have an externally-refreshed cache; these
    # are never flagged stale.
    fresh_at: datetime | None = None
    # Free-form operator-facing source label (e.g.
    # ``"load_weather"`` / ``"NbaInjuryReportCache"``). Used for
    # diagnostics; not read by scoring.
    source: str = ""
    # 0.0 = no expected values populated; 1.0 = every expected key
    # populated. Derived from the emitter's input availability;
    # surfaced in diagnostics, not used for gating (the existing
    # ``*_data_complete`` markers in ``values`` remain the kernel's
    # gating signal).
    completeness: float = 0.0


def mlb_lineup_suppress_when(ctx: SuppressionContext) -> str | None:
    """Smarter #16 bespoke gate, unified callback form.

    Suppresses when ``copilot_requires_lineup`` is set in the market
    metadata AND the lineup payload confirms the player is NOT in the
    starting lineup (scratch / DNP). Mirrors the inline logic the
    scoring kernel used pre-consolidation; ``_single_scoring_adjustments``
    delegates here so the registry is the single source of truth for
    the policy decision.

    Returns ``"player_not_in_starting_lineup"`` on suppression, ``None``
    otherwise (pre-lineup-window, confirmed-in-lineup,
    ``copilot_requires_lineup`` not set, or a non-prop family).

    Codex Pattern 9: family gate to ``_props``. The original inline
    logic was nested inside the ``elif family_key.endswith("_props")``
    branch, so a winner / game-line market that somehow set
    ``copilot_requires_lineup=True`` was never reached by this gate.
    Preserving the same gate here keeps the behavior bit-identical.
    """
    if not ctx.family_key.endswith("_props"):
        return None
    if not ctx.metadata.get("copilot_requires_lineup"):
        return None
    lineup_data_complete = (
        float(ctx.features.get("lineup_data_complete") or 0.0) >= 1.0
    )
    player_in_starting_lineup = (
        float(ctx.features.get("player_in_starting_lineup") or 0.0) >= 1.0
    )
    if lineup_data_complete and not player_in_starting_lineup:
        return "player_not_in_starting_lineup"
    return None


def _injury_suppress_when(ctx: SuppressionContext, *, family_key: str) -> str | None:
    """Shared OUT/DOUBTFUL-on-fresh-report suppression logic.

    Both NBA (Smarter #17) and WNBA (Smarter WNBA PR 7) follow the
    same gate: family-key gated to ``{nba,wnba}_props``, fresh report
    within 12h, OUT > DOUBTFUL in priority. The per-sport wrappers
    below pin the family-key check so each callback only fires on its
    own sport's rows — keeping the diagnostics group-key clean and
    matching the parallel ``nba_workload`` / ``wnba_workload``
    registry layout.

    Returns ``"player_injury_out"`` / ``"player_injury_doubtful"`` on
    suppression, ``None`` otherwise.
    """
    if ctx.family_key != family_key:
        return None
    if not (float(ctx.features.get("injury_data_complete") or 0.0) >= 1.0):
        return None
    if not (float(ctx.features.get("injury_report_is_fresh") or 0.0) >= 1.0):
        return None
    if float(ctx.features.get("player_injury_status_out") or 0.0) >= 1.0:
        return "player_injury_out"
    if float(ctx.features.get("player_injury_status_doubtful") or 0.0) >= 1.0:
        return "player_injury_doubtful"
    return None


def nba_injury_suppress_when(ctx: SuppressionContext) -> str | None:
    """Smarter #17 bespoke gate, unified callback form.

    Suppresses when the family is ``nba_props`` AND a fresh injury
    report (within 12h, encoded as ``injury_report_is_fresh == 1.0``
    by the emitter) reports the player as OUT or DOUBTFUL. Stale
    reports DON'T suppress — the lineup-confirmation gate covers the
    pre-game uncertainty; injury data only adds value when recent.

    NBA-gated (codex Pattern 9 — a stray injury feature on an MLB row
    must not trip this check).

    Returns ``"player_injury_out"`` / ``"player_injury_doubtful"`` on
    suppression, ``None`` otherwise.
    """
    return _injury_suppress_when(ctx, family_key="nba_props")


def wnba_injury_suppress_when(ctx: SuppressionContext) -> str | None:
    """Smarter WNBA PR 7 — WNBA counterpart of
    :func:`nba_injury_suppress_when`. Same fresh-report OUT/DOUBTFUL
    semantics, gated to ``wnba_props`` so a stray injury feature on
    an NBA / MLB row never reaches this branch (codex Pattern 9)."""
    return _injury_suppress_when(ctx, family_key="wnba_props")


def nfl_injury_suppress_when(ctx: SuppressionContext) -> str | None:
    """Smarter NFL PR 6 — NFL prop counterpart of the shared
    OUT/DOUBTFUL gate, family-gated to ``nfl_props`` (Pattern 9)."""
    return _injury_suppress_when(ctx, family_key="nfl_props")


def nfl_qb_status_suppress_when(ctx: SuppressionContext) -> str | None:
    """Smarter NFL PR 6 — the questionable-QB gate on NFL game lines.

    A starting QB listed OUT or DOUBTFUL is PRICED (the -4.5-point
    margin adjustment in the game model); *Questionable* is the
    unpriceable state — the line could move 5+ points either way on a
    pre-kick decision sika can't predict — so the pick is suppressed
    rather than nudged (the user-confirmed design decision).

    Fires only for ``nfl_singles`` winner / game-line markets with a
    fresh QB-status read (``nfl_qb_report_is_fresh``, computed by the
    game model from the ESPN intraday feed / official report age).
    """
    if ctx.family_key != "nfl_singles":
        return None
    market_family = str(ctx.metadata.get("copilot_market_family") or "")
    if market_family not in {"winner", "game_line"}:
        return None
    if not (float(ctx.features.get("nfl_qb_status_data_complete") or 0.0) >= 1.0):
        return None
    if not (float(ctx.features.get("nfl_qb_report_is_fresh") or 0.0) >= 1.0):
        return None
    if float(ctx.features.get("nfl_qb_status_questionable") or 0.0) >= 1.0:
        return "starting_qb_questionable"
    return None


# Default policy: ignore. Adding a new emitter without an explicit
# registry entry never accidentally gates scoring; operators opt in.
DEFAULT_POLICY = FeatureGroupPolicy(
    severity=FeatureGroupSeverity.IGNORE,
    ttl=timedelta(days=365),
)


# Per-group freshness policy. Operators tune by editing this dict.
# The registry intentionally lists only groups with non-default
# behavior — IGNORE-policy groups (the vast majority) fall through to
# ``DEFAULT_POLICY``.
FEATURE_GROUP_POLICIES: dict[str, FeatureGroupPolicy] = {
    # PENALIZE: weather drifts but doesn't binary-flip. -5% confidence
    # when the cached forecast is older than 6h.
    "mlb_weather": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=6),
        penalty_confidence_delta=-0.05,
    ),
    # PENALIZE: bullpen rest is computed from schedule density.
    # Stale data means we missed a game in the window.
    "mlb_bullpen": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=4),
        penalty_confidence_delta=-0.05,
    ),
    # PENALIZE: workload windows change daily. -3% if no fresh game
    # log fetch in 24h.
    "nba_workload": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=24),
        penalty_confidence_delta=-0.03,
    ),
    # PENALIZE: WNBA workload mirrors NBA — same emitter
    # (``emit_nba_workload_features``) reading WNBA gamelog rows, same
    # daily refresh cadence, same stale-data consequences.
    "wnba_workload": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=24),
        penalty_confidence_delta=-0.03,
    ),
    # SUPPRESS (Architecture #5 follow-up 2): Smarter #16 — the bespoke
    # confirmed-and-scratched gate now lives in
    # ``mlb_lineup_suppress_when`` and the registry is the single
    # source of truth. ``ttl`` is unused for SUPPRESS groups (the gate
    # is value-driven, not time-driven) — left at the upstream
    # cache TTL for diagnostic display only.
    "mlb_lineup": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.SUPPRESS,
        ttl=timedelta(hours=12),
        suppress_when=mlb_lineup_suppress_when,
    ),
    # SUPPRESS (Architecture #5 follow-up 2): Smarter #17 — NBA-only
    # OUT / DOUBTFUL gate. ``ttl`` is informational; the freshness
    # window is enforced inside ``nba_injury_suppress_when`` via
    # ``injury_report_is_fresh`` (the emitter does the 12h math).
    "nba_injury": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.SUPPRESS,
        ttl=timedelta(hours=12),
        suppress_when=nba_injury_suppress_when,
    ),
    # SUPPRESS (Smarter WNBA PR 7): WNBA counterpart of ``nba_injury``,
    # parallel to the ``wnba_workload`` PENALIZE entry above. Separate
    # registry entry (rather than widening the NBA family-key gate)
    # keeps diagnostics group-keyed by sport and matches D1 in
    # SMARTER_WNBA_PREP.md (parallel cache tables, parallel groups).
    "wnba_injury": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.SUPPRESS,
        ttl=timedelta(hours=12),
        suppress_when=wnba_injury_suppress_when,
    ),
    # SUPPRESS (Smarter NFL PR 6): NFL prop OUT/DOUBTFUL gate, parallel
    # to nba_injury / wnba_injury. Consumed once PR 7 emits player
    # injury features on the NFL prop path.
    "nfl_injury": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.SUPPRESS,
        ttl=timedelta(hours=12),
        suppress_when=nfl_injury_suppress_when,
    ),
    # SUPPRESS (Smarter NFL PR 6): questionable-QB gate on NFL game
    # lines. OUT/Doubtful QBs are priced; Questionable suppresses.
    "nfl_qb_status": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.SUPPRESS,
        ttl=timedelta(hours=12),
        suppress_when=nfl_qb_status_suppress_when,
    ),
    # PENALIZE (Smarter NFL PR 5): the sportsbook consensus anchor is a
    # first-class NFL scoring input (unlike the advisory-only
    # sportsbook_consensus group). Book lines older than 6h mislead —
    # they miss injury news and line movement — but don't binary-flip.
    "nfl_consensus": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=6),
        penalty_confidence_delta=-0.05,
    ),
    # PENALIZE (Smarter NFL PR 5): mirrors mlb_weather — forecasts
    # drift; a stale wind reading shifts totals but not catastrophically.
    "nfl_weather": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=6),
        penalty_confidence_delta=-0.05,
    ),
    # PENALIZE (Smarter NFL PR 5): EPA ratings refresh daily via
    # nfl_data_refresh; 48h staleness means the job has missed a cycle
    # (weekly sport — one missed nightly rebuild is tolerable, two are
    # a data problem).
    "nfl_team_ratings": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=48),
        penalty_confidence_delta=-0.03,
    ),
    # IGNORE groups (registry entries omitted; fall through to
    # ``DEFAULT_POLICY``). Documented here for operator visibility:
    #
    # Bespoke gates not yet consolidated:
    # - mlb_starter — has_probable_starter_context missing-context gate
    #   plus graceful degradation in opposing_starter_* factors. Not
    #   suppression-shaped; stays bespoke for now.
    #
    # Season-stable / aggregates:
    # - mlb_park (park factors stable across season)
    # - mlb_batter (season + statcast aggregates)
    # - mlb_platoon (season splits)
    # - nba_advanced (player advanced; recent + season)
    # - nba_opponent_team (opponent advanced)
    # - nba_referee (per-referee season stats)
    # - nba_hustle / nba_drives / nba_clutch / nba_player_defense
    #   (long-tail season aggregates)
    #
    # Toggle-gated advisory:
    # - sportsbook_consensus (consumed via separate diagnostics dict;
    #   suppression toggle is operator-controlled and OFF by default)
}


def policy_for_group(group_key: str) -> FeatureGroupPolicy:
    """Look up a group's freshness policy.

    Unknown ``group_key`` falls through to :data:`DEFAULT_POLICY`
    (IGNORE, 365-day TTL) so a new emitter without a registry entry
    never accidentally gates scoring.
    """
    return FEATURE_GROUP_POLICIES.get(group_key, DEFAULT_POLICY)


def register_group(
    feature_groups: dict[str, FeatureGroupSnapshot],
    group_key: str,
    values: dict[str, Any],
    *,
    fresh_at: datetime | None = None,
    source: str = "",
) -> None:
    """Build a snapshot and stash it in ``feature_groups``.

    Lower-level than :func:`emit_to_group` — used by call sites that
    only care about the structured snapshot (e.g. test fixtures,
    persistence-layer reconstruction).

    ``completeness`` is derived from ``values`` truthiness: an empty
    values dict means the emitter didn't have data to publish
    (cache miss with no payload), which the kernel surfaces via the
    existing ``*_data_complete`` markers AND the structural 0.0
    completeness on the snapshot.
    """
    feature_groups[group_key] = FeatureGroupSnapshot(
        group_key=group_key,
        values=values,
        fresh_at=fresh_at,
        source=source,
        completeness=1.0 if values else 0.0,
    )


def emit_to_group(
    feature_groups: dict[str, FeatureGroupSnapshot],
    features: dict[str, Any],
    group_key: str,
    values: dict[str, Any],
    *,
    fresh_at: datetime | None = None,
    source: str = "",
) -> None:
    """Migration helper for the scoring kernel.

    Replaces the pre-Architecture-#5 idiom

        features.update(emit_X(payload))

    with

        emit_to_group(
            feature_groups, features, "X", emit_X(payload),
            fresh_at=..., source="...",
        )

    Writes ``values`` to BOTH ``feature_groups[group_key]`` (the
    Architecture #5 source of truth carrying freshness metadata) AND
    ``features`` (the derived flat view the kernel and heuristic
    factors continue to read from). Because both writes happen
    atomically from the same ``values`` dict, drift between them is
    structurally impossible — this is the in-process "pure A" shape
    despite the surface appearance of dual writes.

    The kernel reads ``features.get("k")`` immediately after this
    call (e.g. the Smarter #12 interaction term reads
    ``recent_usage_pct`` right after ``emit_nba_player_features``
    populates it) so the dual-write keeps the existing read order
    working unchanged.
    """
    register_group(
        feature_groups, group_key, values, fresh_at=fresh_at, source=source,
    )
    features.update(values)


def features_view(
    feature_groups: dict[str, FeatureGroupSnapshot],
) -> dict[str, Any]:
    """Derive the flat ``features`` dict the scoring kernel expects.

    This is the core of Architecture #5's pure-A shape: emitters
    write to ``feature_groups`` (single source of truth); the kernel
    reads via this derived view. Existing read sites in
    ``scoring/__init__.py`` and ``heuristic_factors.py`` (~141
    ``features.get(...)`` calls combined) keep their shape unchanged.

    Last-group-wins on key conflicts. In practice groups don't share
    keys — the emitter migration enforces disjoint key namespaces per
    group — but the test suite pins this with an explicit overlap
    check.
    """
    flat: dict[str, Any] = {}
    for snapshot in feature_groups.values():
        flat.update(snapshot.values)
    return flat


@dataclass(frozen=True, slots=True)
class FreshnessAssessment:
    """One group's freshness check result.

    Built by :func:`check_freshness`; consumed by the scoring kernel
    when applying per-group policy. Surfaced into diagnostics so
    operators can audit which groups were stale at scoring time.
    """

    group_key: str
    severity: FeatureGroupSeverity
    is_stale: bool
    # Time since ``fresh_at``; ``None`` when the snapshot opted out
    # of the freshness check by leaving ``fresh_at`` unset.
    age: timedelta | None
    # Nonzero only when ``severity == PENALIZE`` and ``is_stale``.
    confidence_delta: float


def check_freshness(
    feature_groups: dict[str, FeatureGroupSnapshot],
    *,
    now: datetime,
) -> list[FreshnessAssessment]:
    """Evaluate every group's freshness against its policy.

    Returns one assessment per group present in ``feature_groups``.
    Groups whose ``fresh_at`` is ``None`` are treated as
    not-applicable (``is_stale=False``, no penalty) — emitters that
    don't have an externally-refreshed cache (derived features,
    schedule-density computations) opt out of the freshness check by
    leaving ``fresh_at`` unset.

    Naive ``fresh_at`` datetimes are coerced to UTC before the age
    arithmetic — matches the convention every other freshness check
    in the codebase follows (SQLite drops tz on DateTime columns; the
    coercion keeps Postgres + SQLite output identical).
    """
    assessments: list[FreshnessAssessment] = []
    for group_key, snapshot in feature_groups.items():
        policy = policy_for_group(group_key)
        fresh_at = snapshot.fresh_at
        if fresh_at is None:
            assessments.append(
                FreshnessAssessment(
                    group_key=group_key,
                    severity=policy.severity,
                    is_stale=False,
                    age=None,
                    confidence_delta=0.0,
                )
            )
            continue
        if fresh_at.tzinfo is None:
            fresh_at = fresh_at.replace(tzinfo=timezone.utc)
        age = now - fresh_at
        is_stale = age > policy.ttl
        confidence_delta = (
            policy.penalty_confidence_delta
            if is_stale and policy.severity is FeatureGroupSeverity.PENALIZE
            else 0.0
        )
        assessments.append(
            FreshnessAssessment(
                group_key=group_key,
                severity=policy.severity,
                is_stale=is_stale,
                age=age,
                confidence_delta=confidence_delta,
            )
        )
    return assessments


def check_suppressions(context: SuppressionContext) -> dict[str, str]:
    """Evaluate every SUPPRESS-policy group's ``suppress_when``
    callback and collect non-None reasons by group_key.

    The scoring kernel calls this from
    ``_single_scoring_adjustments`` (post Architecture #5 follow-up 2)
    instead of inlining the bespoke gates. Group keys returned by this
    function map onto the existing intermediate diagnostic keys
    (``mlb_lineup`` → ``lineup_suppression_reason`` etc.) so the
    downstream translation in ``_build_scored_recommendation`` keeps
    its existing shape.

    Iteration order is insertion order over ``FEATURE_GROUP_POLICIES``;
    callbacks must be independent — no callback may rely on another
    having (or not having) fired for the same scoring pass.
    """
    result: dict[str, str] = {}
    for group_key, policy in FEATURE_GROUP_POLICIES.items():
        if policy.severity is not FeatureGroupSeverity.SUPPRESS:
            continue
        if policy.suppress_when is None:
            continue
        reason = policy.suppress_when(context)
        if reason is not None:
            result[group_key] = reason
    return result


def serialize_feature_groups(
    feature_groups: dict[str, FeatureGroupSnapshot],
) -> dict[str, dict[str, Any]]:
    """JSON-friendly representation for persistence.

    Persistence dual-writes the flat ``features`` dict (derived view,
    backward compat with historical rows + the ML training
    vectorizer) AND the nested ``feature_groups`` map (the
    Architecture #5 source of truth). This function produces the
    nested half.

    Datetimes serialize as ISO 8601 strings; everything else passes
    through. The reader path lives in :func:`deserialize_feature_groups`.
    """
    result: dict[str, dict[str, Any]] = {}
    for group_key, snapshot in feature_groups.items():
        result[group_key] = {
            "values": dict(snapshot.values),
            "fresh_at": (
                snapshot.fresh_at.isoformat() if snapshot.fresh_at else None
            ),
            "source": snapshot.source,
            "completeness": snapshot.completeness,
        }
    return result


def deserialize_feature_groups(
    raw: dict[str, dict[str, Any]] | None,
) -> dict[str, FeatureGroupSnapshot]:
    """Reverse of :func:`serialize_feature_groups`.

    Tolerant of missing fields so prediction rows written before
    Architecture #5 shipped (which won't have ``feature_groups`` at
    all) round-trip as an empty dict. The reader side at the scoring
    kernel handles "no feature_groups" by falling back to the flat
    ``features`` dict on the same row.
    """
    if not raw:
        return {}
    result: dict[str, FeatureGroupSnapshot] = {}
    for group_key, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        fresh_at_raw = payload.get("fresh_at")
        fresh_at: datetime | None
        if isinstance(fresh_at_raw, str) and fresh_at_raw:
            try:
                fresh_at = datetime.fromisoformat(fresh_at_raw)
            except ValueError:
                fresh_at = None
        else:
            fresh_at = None
        result[group_key] = FeatureGroupSnapshot(
            group_key=group_key,
            values=dict(payload.get("values") or {}),
            fresh_at=fresh_at,
            source=str(payload.get("source") or ""),
            completeness=float(payload.get("completeness") or 0.0),
        )
    return result


__all__ = [
    "FeatureGroupSeverity",
    "FeatureGroupPolicy",
    "FeatureGroupSnapshot",
    "FreshnessAssessment",
    "SuppressionContext",
    "SuppressWhenFn",
    "DEFAULT_POLICY",
    "FEATURE_GROUP_POLICIES",
    "policy_for_group",
    "register_group",
    "emit_to_group",
    "features_view",
    "check_freshness",
    "check_suppressions",
    "mlb_lineup_suppress_when",
    "nba_injury_suppress_when",
    "wnba_injury_suppress_when",
    "nfl_injury_suppress_when",
    "nfl_qb_status_suppress_when",
    "serialize_feature_groups",
    "deserialize_feature_groups",
]
