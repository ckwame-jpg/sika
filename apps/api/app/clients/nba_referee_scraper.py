"""Smarter #13 Phase 1 — scrape NBA referee assignments.

Source: ``https://official.nba.com/referee-assignments/`` is a server-
rendered static HTML page that lists each day's game-by-game crew
assignment (crew chief / referee / umpire / alternate). The page
exposes one date at a time; an optional ``date=MM/DD/YYYY`` query
parameter lets us pull a specific past or future date.

Phase 1 ships the scraper + cache. Per-referee tendency stats (used to
nudge total-points / fouls / FT props) ship in a follow-up PR — the
mechanism flows producer → consumer with the harder HTML-parsing
work isolated here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Any

import httpx
from lxml import html as lxml_html

from app.config import get_settings


logger = logging.getLogger(__name__)


_REFEREE_ASSIGNMENTS_URL = "https://official.nba.com/referee-assignments/"
_HEADER_REQUIRED = ("Game", "Crew Chief", "Referee", "Umpire")

# The crew column cells are rendered as ``"Tony Brothers (#25)"`` (with
# trailing whitespace sometimes present). Extract the number — useful
# both as a stable identifier (names change spellings between sources)
# and as a sanity check that we parsed a real cell rather than the
# alternate column's blank value.
_OFFICIAL_NUMBER_RE = re.compile(r"\(#(\d+)\)")


@dataclass(frozen=True, slots=True)
class NbaCrewMember:
    """One crew slot for a game (crew chief / referee / umpire / alternate)."""
    name: str
    number: int | None


@dataclass(frozen=True, slots=True)
class NbaRefereeAssignment:
    """Crew assignment for one game on a given date.

    ``away_team`` and ``home_team`` are normalized from the upstream
    ``"Away @ Home"`` matchup string — sika's event matcher can use
    them directly for the consumer-side wiring in Phase 2.
    """
    matchup: str
    away_team: str
    home_team: str
    crew_chief: NbaCrewMember | None
    referee: NbaCrewMember | None
    umpire: NbaCrewMember | None
    alternate: NbaCrewMember | None


@dataclass(frozen=True, slots=True)
class NbaRefereeAssignmentDay:
    """All NBA crew assignments scraped from a single page render.

    ``page_date`` is the date string the page displays in its
    ``entry-meta`` header (e.g. ``"May 14, 2026"``); operators can
    use it to verify the scrape returned the day they intended.
    """
    page_date: str | None
    assignments: list[NbaRefereeAssignment]


def _parse_crew_member(text: str | None) -> NbaCrewMember | None:
    """Parse a ``"Tony Brothers (#25)"`` cell into a ``NbaCrewMember``.

    Returns ``None`` for empty / whitespace-only cells (the alternate
    column is frequently blank).
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    match = _OFFICIAL_NUMBER_RE.search(stripped)
    number: int | None = None
    name = stripped
    if match:
        number = int(match.group(1))
        # Trim the "(#NN)" suffix and any trailing whitespace so the name
        # field is a clean display name.
        name = stripped[: match.start()].strip()
    return NbaCrewMember(name=name, number=number)


def _parse_matchup(text: str | None) -> tuple[str, str, str]:
    """Split ``"Minnesota @ Dallas"`` into ``(matchup, away, home)``.

    Falls back to ``(matchup, "", "")`` when the separator is missing
    or malformed so the row is still queryable by matchup string.
    """
    matchup = (text or "").strip()
    if " @ " not in matchup:
        return matchup, "", ""
    away, home = matchup.split(" @ ", 1)
    return matchup, away.strip(), home.strip()


def _is_referee_table(table: Any) -> bool:
    """Return True when a ``<table>`` is the referee-assignments table.

    The page contains other tables (WNBA assignments, footer
    navigation) — match on the canonical header set so we only parse
    the NBA section.
    """
    header_cells = [
        (cell.text_content() or "").strip()
        for cell in table.xpath("./thead/tr/th")
    ]
    return all(required in header_cells for required in _HEADER_REQUIRED)


def parse_nba_referee_assignments_html(html_body: str) -> NbaRefereeAssignmentDay:
    """Parse the raw HTML response into structured assignments.

    Tolerant by design: malformed rows are skipped (logged at debug)
    rather than raising — the upstream page mixes blanks, abbreviated
    formats, and the alternate column is often empty.
    """
    if not html_body or not html_body.strip():
        return NbaRefereeAssignmentDay(page_date=None, assignments=[])
    tree = lxml_html.fromstring(html_body)
    page_date_nodes = tree.xpath("//div[contains(@class, 'entry-meta')]/text()")
    page_date = page_date_nodes[0].strip() if page_date_nodes else None

    # XPath ``contains(@class, 'nba-refs-content')`` is a SUBSTRING test
    # and would falsely match ``wnba-refs-content`` too. Use the canonical
    # whole-word idiom so the WNBA section can't leak into NBA results.
    nba_tables = [
        table for table in tree.xpath(
            "//div[contains(concat(' ', normalize-space(@class), ' '),"
            " ' nba-refs-content ')]//table"
        )
        if _is_referee_table(table)
    ]
    if not nba_tables:
        # Fallback: the page wraps the NBA section in ``<div
        # class="nba-refs-content">`` today, but if the wrapper changes
        # we still find the referee table via its header. This keeps
        # the scraper working through a minor markup refactor upstream.
        nba_tables = [table for table in tree.xpath("//table") if _is_referee_table(table)]

    assignments: list[NbaRefereeAssignment] = []
    for table in nba_tables:
        for row in table.xpath("./tbody/tr"):
            cells = row.xpath("./td")
            if len(cells) < 4:
                continue
            matchup_text = cells[0].text_content()
            crew_chief = _parse_crew_member(cells[1].text_content())
            referee = _parse_crew_member(cells[2].text_content())
            umpire = _parse_crew_member(cells[3].text_content())
            alternate = (
                _parse_crew_member(cells[4].text_content())
                if len(cells) >= 5
                else None
            )
            matchup, away_team, home_team = _parse_matchup(matchup_text)
            if not matchup:
                continue
            assignments.append(
                NbaRefereeAssignment(
                    matchup=matchup,
                    away_team=away_team,
                    home_team=home_team,
                    crew_chief=crew_chief,
                    referee=referee,
                    umpire=umpire,
                    alternate=alternate,
                )
            )
    return NbaRefereeAssignmentDay(page_date=page_date, assignments=assignments)


class NbaRefereeAssignmentsClient:
    """Thin httpx wrapper around the NBA referee-assignments page.

    Single-purpose for Phase 1 — fetches today's (or a specific
    date's) assignment table and returns it parsed. The consumer-side
    feature emission and the per-ref historical-tendency join land in
    Phase 2.
    """

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http_client = http_client

    def _user_agent(self) -> str:
        # Reuse the NWS user-agent setting as a generic identifier so we
        # don't ship blank UA strings (NBA's CDN occasionally rejects
        # bare httpx UAs). Operators can override via the setting.
        return get_settings().nws_user_agent or "sika-sports-copilot"

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", {}).setdefault("User-Agent", self._user_agent())
        if self._http_client is not None:
            return self._http_client.get(url, **kwargs)
        return httpx.get(url, **kwargs)

    def fetch_assignments(self, *, target_date: date_cls | None = None) -> NbaRefereeAssignmentDay:
        """Return the parsed assignments for ``target_date`` (or today
        if not provided).

        Raises ``httpx.HTTPStatusError`` on 4xx/5xx so the upstream-
        health recorder (Smarter #23) can register the failure.
        """
        params: dict[str, str] = {}
        if target_date is not None:
            # The page expects MM/DD/YYYY in the date filter form.
            params["date"] = target_date.strftime("%m/%d/%Y")
        response = self._get(_REFEREE_ASSIGNMENTS_URL, params=params or None, timeout=20)
        response.raise_for_status()
        return parse_nba_referee_assignments_html(response.text)
