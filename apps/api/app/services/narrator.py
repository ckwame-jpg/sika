"""Smarter #31 — grounded LLM narrator for recommendations.

Turns sika's mechanical rationale ("recent 10-game points average:
28.3 / season average: 26.1 / opp factor: 1.05x...") into a
natural-language explanation that operators can scan faster.

## Grounding contract

The narrator's system prompt is strict about staying inside the
provided feature dict. After generation, a verifier pass parses the
output for claims and matches them against the feature dict. Claims
that don't have a feature backing them get the narration rejected —
the operator surface falls back to the mechanical rationale.

## What the verifier checks

The verifier is intentionally **loose** at v1 — it doesn't try to
fact-check every word, just the high-risk failure modes:

- Numeric claims (e.g. ``"averaging 28.3"``) must match a feature
  value within rounding tolerance.
- Forbidden topics (injuries, refs, trades, rivalry history, weather,
  team records) trigger rejection — sika doesn't pass those
  features so the LLM has no grounded source for them.
- Player / team names not present in the recommendation metadata
  trigger rejection (catches "Tatum is feuding with LeBron" style
  hallucinations).

A tighter verifier (full claim extraction → matching) is Phase 2.

## Why this is feature-flagged off by default

LLM narrations are a UX polish, not load-bearing for prediction
quality. Operators turn the flag on via the model-readiness settings
endpoint, eyeball the output on real picks for a week, and decide
whether to leave it on. The mechanical rationale is the always-present
fallback.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings


logger = logging.getLogger(__name__)


class NarratorNotConfiguredError(RuntimeError):
    """Raised when the narrator is invoked without an OpenAI key.

    Callers should treat this as "narrator unavailable" and fall back
    to the mechanical rationale.
    """


@dataclass(frozen=True, slots=True)
class NarratorOutput:
    """Result of a narration call.

    ``verifier_passed = False`` means the LLM output contained at
    least one unsupported claim — operators should NOT surface this
    text; the cache stores it for debugging.
    """
    text: str
    verifier_passed: bool
    rejected_claims: list[str]
    model_name: str


# Forbidden topics — these don't appear in sika's feature dict so any
# narration that references them is hallucinating. The verifier
# checks for case-insensitive substring matches.
_FORBIDDEN_PHRASES: tuple[str, ...] = (
    "injury",
    "injured",
    "ruled out",
    "questionable",
    "doubtful",
    "trade",
    "traded",
    "rivalry",
    "feud",
    "weather",
    "rain",
    "wind",
    "snow",
    "referee",
    "ref crew",
    "crew chief",
    "umpire",
)


_NUMERIC_CLAIM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")


def _build_system_prompt(max_words: int = 120) -> str:
    """Strict system prompt — ground every claim in the supplied feature
    dict, NEVER mention forbidden topics, cap the output length."""
    forbidden = ", ".join(_FORBIDDEN_PHRASES)
    return (
        "You are a betting-recommendation explainer. You receive a "
        "JSON feature dict and recommendation metadata. Your output:\n"
        f"- MUST be {max_words} words or fewer\n"
        "- MUST only reference numeric values that appear in the "
        "feature dict (round to one decimal place)\n"
        "- MUST NOT invent player history, team records, prior "
        "matchups, motivational narratives, or coaching quotes\n"
        "- MUST NOT mention any of these topics (sika doesn't pass "
        f"this data, so any reference is fabricated): {forbidden}\n"
        "- MUST NOT name any player or team not present in the "
        "recommendation metadata\n"
        "- SHOULD explain WHY the recommendation is what it is in "
        "plain English — translate factor values into intuition\n"
        "- SHOULD NOT use bullets or headings — one paragraph\n"
        "Return only the explanation text. No preamble, no markdown."
    )


def _serialize_features_for_prompt(features: dict[str, Any]) -> str:
    """Build a compact key=value lines view of the feature dict for
    the user prompt. Skips None values and non-numeric keys to keep
    the prompt focused."""
    lines: list[str] = []
    for key, value in sorted(features.items()):
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
            continue
        if isinstance(value, (int, float)):
            lines.append(f"{key}: {round(float(value), 4)}")
            continue
        if isinstance(value, str) and value.strip():
            lines.append(f"{key}: {value.strip()}")
    return "\n".join(lines)


def _build_user_prompt(features: dict[str, Any], recommendation_metadata: dict[str, Any]) -> str:
    """Compose the user prompt from features + metadata."""
    metadata_lines = [
        f"subject_name: {recommendation_metadata.get('subject_name') or 'unknown'}",
        f"subject_team: {recommendation_metadata.get('subject_team') or 'unknown'}",
        f"market_title: {recommendation_metadata.get('market_title') or 'unknown'}",
        f"market_family: {recommendation_metadata.get('market_family') or 'unknown'}",
        f"side: {recommendation_metadata.get('side') or 'unknown'}",
        f"threshold: {recommendation_metadata.get('threshold') if recommendation_metadata.get('threshold') is not None else 'unknown'}",
        f"edge: {recommendation_metadata.get('edge')}",
        f"confidence: {recommendation_metadata.get('confidence')}",
    ]
    return (
        "Recommendation metadata:\n"
        + "\n".join(metadata_lines)
        + "\n\nFeatures:\n"
        + _serialize_features_for_prompt(features)
        + "\n\nExplain this recommendation in plain English."
    )


def _allowed_names(recommendation_metadata: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("subject_name", "subject_team", "event_name"):
        value = recommendation_metadata.get(key)
        if isinstance(value, str) and value.strip():
            names.add(value.strip().lower())
            for token in re.split(r"[\s\-]+", value.strip().lower()):
                if len(token) >= 4:
                    names.add(token)
    return names


def verify_narration(
    text: str,
    *,
    features: dict[str, Any],
    recommendation_metadata: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Loose verifier — return ``(passed, rejected_claims)``.

    Checks the three classes of high-risk hallucination:

    1. Forbidden topics (injuries, refs, weather, rivalries, trades).
    2. Numeric claims that don't match any feature value within rounding.
    3. Player / team names not in the recommendation metadata.

    Returns ``(True, [])`` only when ALL checks pass. The rejected-
    claims list contains short human-readable strings the operator
    can inspect when debugging.
    """
    rejected: list[str] = []
    lowered = text.lower()

    for phrase in _FORBIDDEN_PHRASES:
        if phrase in lowered:
            rejected.append(f"forbidden_phrase:{phrase}")

    numeric_feature_values: set[float] = set()
    for value in features.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            numeric_feature_values.add(round(float(value), 1))
    metadata_threshold = recommendation_metadata.get("threshold")
    if isinstance(metadata_threshold, (int, float)) and not isinstance(metadata_threshold, bool):
        numeric_feature_values.add(round(float(metadata_threshold), 1))

    # Tolerate numbers an LLM commonly produces as CONTEXT rather than
    # CLAIM — "last 10 games", "3 of 5", "T-12 minutes". The verifier
    # only flags numbers that look like stat claims:
    #   * decimals: ``28.3 points`` — almost always a stat figure
    #   * whole numbers ≥ 13: ``25 points``, ``112 DRtg``, ``47%`` —
    #     ranges where context references are rare
    # Whole numbers ≤ 12 are unchecked. False-negative rate is OK at
    # v1; a tighter verifier (full claim extraction) is Phase 2.
    for match in _NUMERIC_CLAIM_RE.finditer(text):
        raw = match.group(1)
        value = float(raw)
        if "." not in raw and value < 13:
            continue
        rounded = round(value, 1)
        # Match within a small tolerance — LLMs sometimes round
        # 28.32 → 28.3 → 28.0 in their narration.
        if any(abs(rounded - feature_value) <= 0.6 for feature_value in numeric_feature_values):
            continue
        rejected.append(f"ungrounded_number:{raw}")

    allowed = _allowed_names(recommendation_metadata)
    # Capitalized words / phrases — likely proper nouns. Look for
    # capitalized tokens at least 4 chars long.
    candidate_names = re.findall(r"\b[A-Z][a-zA-Z'\-]{3,}(?:\s+[A-Z][a-zA-Z'\-]{2,})?", text)
    for name in candidate_names:
        normalized = name.strip().lower()
        if not normalized:
            continue
        # Strip possessives ("Brooklyn's" → "brooklyn") so the
        # tokenizer can match against the team name.
        stripped = re.sub(r"'s\b|s'\b", "", normalized)
        tokens = re.split(r"[\s\-]+", stripped)
        if stripped in allowed or normalized in allowed:
            continue
        if any(token in allowed for token in tokens if len(token) >= 4):
            continue
        # Skip common English sentence-start words the LLM uses.
        first_token = tokens[0] if tokens else ""
        if first_token in {
            "over", "under", "with", "this", "that", "the", "their",
            "above", "below", "given", "expected", "projection",
            "recent", "season", "watch", "even",
        }:
            continue
        rejected.append(f"unrecognized_name:{name}")

    return (len(rejected) == 0, rejected)


def _call_openai_chat_completion(
    *,
    system_prompt: str,
    user_prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    max_output_tokens: int,
    timeout_seconds: float,
    http_client: httpx.Client | None = None,
) -> str:
    """Single chat-completion call. Returns the raw text.

    Raises ``httpx.HTTPStatusError`` on 4xx/5xx so the upstream
    health recorder (Smarter #23) can register failures.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_output_tokens,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if http_client is not None:
        response = http_client.post(url, headers=headers, json=body, timeout=timeout_seconds)
    else:
        response = httpx.post(url, headers=headers, json=body, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    return str(message.get("content") or "").strip()


def generate_narration(
    *,
    features: dict[str, Any],
    recommendation_metadata: dict[str, Any],
    http_client: httpx.Client | None = None,
) -> NarratorOutput:
    """Generate a narration for a single recommendation.

    Raises ``NarratorNotConfiguredError`` when ``OPENAI_API_KEY`` is
    empty — caller should treat as "narrator unavailable" and surface
    the mechanical rationale instead.
    """
    settings = get_settings()
    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise NarratorNotConfiguredError("OPENAI_API_KEY is not configured")

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(features, recommendation_metadata)
    text = _call_openai_chat_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        api_key=api_key,
        base_url=settings.narrator_openai_base_url,
        model=settings.narrator_openai_model,
        max_output_tokens=settings.narrator_max_output_tokens,
        timeout_seconds=settings.narrator_request_timeout_seconds,
        http_client=http_client,
    )
    verifier_passed, rejected = verify_narration(
        text,
        features=features,
        recommendation_metadata=recommendation_metadata,
    )
    if not verifier_passed:
        logger.info(
            "narrator.verifier_rejected: rejected_claims=%s text=%r",
            rejected,
            text[:200],
        )
    return NarratorOutput(
        text=text,
        verifier_passed=verifier_passed,
        rejected_claims=rejected,
        model_name=settings.narrator_openai_model,
    )
