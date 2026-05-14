"""Tests for Smarter #13 Phase 1 — NBA referee scraper.

The HTML parser is the load-bearing piece — we test it directly with
fixture markup mirroring official.nba.com's actual structure. The
HTTP client is exercised with a stub so no network is touched.
"""

from datetime import date as date_cls
from typing import Any

import httpx
import pytest

from app.clients.nba_referee_scraper import (
    NbaCrewMember,
    NbaRefereeAssignmentsClient,
    NbaRefereeAssignmentDay,
    parse_nba_referee_assignments_html,
)


_FIXTURE_PAGE_DATE = "May 14, 2026"


def _build_page(*, nba_rows_html: str = "", wnba_rows_html: str = "") -> str:
    """Compose a fixture HTML page mirroring official.nba.com's layout.

    The real page has separate NBA and WNBA sections wrapped in
    ``<div class="nba-refs-content">`` / ``<div class="wnba-refs-content">``.
    Parser should only pick up the NBA section.
    """
    return f"""
    <html><body>
      <div id="main">
        <section role="main">
          <article class="referee-assignments">
            <header class="entry-header">
              <h1 class="entry-title">NBA Referee Assignments</h1>
              <div class="entry-meta">{_FIXTURE_PAGE_DATE}</div>
            </header>
            <div class="entry-content">
              <div class="dayContent">
                <div class="nba-refs-content">
                  <table class="table">
                    <thead><tr>
                      <th class="tableTitle">Game</th>
                      <th class="tableTitle">Crew Chief</th>
                      <th class="tableTitle">Referee</th>
                      <th class="tableTitle">Umpire</th>
                      <th class="tableTitle">Alternate</th>
                    </tr></thead>
                    <tbody>{nba_rows_html}</tbody>
                  </table>
                </div>
              </div>
            </div>
          </article>
        </section>
        <section role="main">
          <article class="referee-assignments">
            <div class="entry-content">
              <div class="dayContent">
                <div class="wnba-refs-content">
                  <table class="table">
                    <thead><tr>
                      <th class="tableTitle">Game</th>
                      <th class="tableTitle">Crew Chief</th>
                      <th class="tableTitle">Referee</th>
                      <th class="tableTitle">Umpire</th>
                      <th class="tableTitle">Alternate</th>
                    </tr></thead>
                    <tbody>{wnba_rows_html}</tbody>
                  </table>
                </div>
              </div>
            </div>
          </article>
        </section>
      </div>
    </body></html>
    """


def _nba_row(matchup: str, crew: str, ref: str, umpire: str, alt: str = "") -> str:
    return (
        "<tr>"
        f"<td>{matchup}</td>"
        f"<td>{crew}</td>"
        f"<td>{ref}</td>"
        f"<td>{umpire}</td>"
        f"<td>{alt}</td>"
        "</tr>"
    )


# -- parse branches ------------------------------------------------------


def test_parse_returns_empty_on_empty_body() -> None:
    assert parse_nba_referee_assignments_html("") == NbaRefereeAssignmentDay(
        page_date=None, assignments=[]
    )
    assert parse_nba_referee_assignments_html("   ") == NbaRefereeAssignmentDay(
        page_date=None, assignments=[]
    )


def test_parse_extracts_page_date_from_entry_meta() -> None:
    out = parse_nba_referee_assignments_html(_build_page())
    assert out.page_date == _FIXTURE_PAGE_DATE


def test_parse_returns_empty_assignments_when_table_body_empty() -> None:
    # NBA section exists but has no rows — common during the offseason.
    out = parse_nba_referee_assignments_html(_build_page())
    assert out.assignments == []


def test_parse_extracts_single_assignment() -> None:
    row = _nba_row(
        "Minnesota @ Dallas",
        "Tony Brothers (#25)",
        "Charles Watson (#6)",
        "Blanca Burns (#8)",
    )
    out = parse_nba_referee_assignments_html(_build_page(nba_rows_html=row))
    assert len(out.assignments) == 1
    a = out.assignments[0]
    assert a.matchup == "Minnesota @ Dallas"
    assert a.away_team == "Minnesota"
    assert a.home_team == "Dallas"
    assert a.crew_chief == NbaCrewMember(name="Tony Brothers", number=25)
    assert a.referee == NbaCrewMember(name="Charles Watson", number=6)
    assert a.umpire == NbaCrewMember(name="Blanca Burns", number=8)
    assert a.alternate is None


def test_parse_extracts_multiple_assignments_in_order() -> None:
    rows = "".join(
        [
            _nba_row("BOS @ NYK", "Tony Brothers (#25)", "C. Watson (#6)", "B. Burns (#8)"),
            _nba_row("LAL @ GSW", "Scott Foster (#48)", "J. Goble (#30)", "S. Wright (#42)"),
        ]
    )
    out = parse_nba_referee_assignments_html(_build_page(nba_rows_html=rows))
    assert len(out.assignments) == 2
    assert out.assignments[0].matchup == "BOS @ NYK"
    assert out.assignments[1].matchup == "LAL @ GSW"
    assert out.assignments[1].crew_chief.number == 48


def test_parse_extracts_alternate_when_present() -> None:
    row = _nba_row(
        "BOS @ NYK",
        "Tony Brothers (#25)",
        "C. Watson (#6)",
        "B. Burns (#8)",
        alt="Karl Lane (#77)",
    )
    out = parse_nba_referee_assignments_html(_build_page(nba_rows_html=row))
    assert out.assignments[0].alternate == NbaCrewMember(name="Karl Lane", number=77)


def test_parse_handles_trailing_whitespace_in_crew_cells() -> None:
    # Real page has nbsp-padded cells: ``"Tony Brothers (#25)                              "``.
    row = _nba_row(
        "BOS @ NYK",
        "Tony Brothers (#25)                              ",
        "C. Watson (#6)",
        "B. Burns (#8)",
    )
    out = parse_nba_referee_assignments_html(_build_page(nba_rows_html=row))
    assert out.assignments[0].crew_chief == NbaCrewMember(name="Tony Brothers", number=25)


def test_parse_handles_crew_member_without_number() -> None:
    # Defensive: occasional rows omit the "(#NN)" suffix. Keep the name.
    row = _nba_row("BOS @ NYK", "Tony Brothers", "C. Watson (#6)", "B. Burns (#8)")
    out = parse_nba_referee_assignments_html(_build_page(nba_rows_html=row))
    assert out.assignments[0].crew_chief == NbaCrewMember(name="Tony Brothers", number=None)


def test_parse_handles_matchup_without_separator() -> None:
    # Defensive: the page occasionally renders just a team name during
    # special events. Preserve the raw matchup but leave teams empty.
    row = _nba_row("Special Event", "Brothers (#25)", "Watson (#6)", "Burns (#8)")
    out = parse_nba_referee_assignments_html(_build_page(nba_rows_html=row))
    assert out.assignments[0].matchup == "Special Event"
    assert out.assignments[0].away_team == ""
    assert out.assignments[0].home_team == ""


def test_parse_skips_rows_with_insufficient_cells() -> None:
    # 3-cell row instead of 4 — likely a heading/spacer artifact.
    rows = (
        "<tr><td>HEADER</td><td>colspan?</td><td></td></tr>"
        + _nba_row("BOS @ NYK", "Brothers (#25)", "Watson (#6)", "Burns (#8)")
    )
    out = parse_nba_referee_assignments_html(_build_page(nba_rows_html=rows))
    assert len(out.assignments) == 1
    assert out.assignments[0].matchup == "BOS @ NYK"


def test_parse_ignores_wnba_section_entirely() -> None:
    # WNBA assignments share the table shape but live under a sibling
    # ``<div class="wnba-refs-content">``. They must NOT leak into the
    # NBA result list.
    out = parse_nba_referee_assignments_html(
        _build_page(
            nba_rows_html=_nba_row("BOS @ NYK", "Brothers (#25)", "Watson (#6)", "Burns (#8)"),
            wnba_rows_html=_nba_row("LIB @ FEV", "Cissoko (#15)", "Gatling (#24)", "Reed (#46)"),
        )
    )
    assert [a.matchup for a in out.assignments] == ["BOS @ NYK"]


def test_parse_falls_back_to_global_table_when_wrapper_class_changes() -> None:
    # If official.nba.com refactors away from ``nba-refs-content`` we
    # still want to find the table by its header signature. Strip the
    # wrapper class out of the fixture and confirm the parser keeps
    # working.
    page = _build_page(
        nba_rows_html=_nba_row("BOS @ NYK", "Brothers (#25)", "Watson (#6)", "Burns (#8)")
    ).replace("nba-refs-content", "renamed-content-wrapper")
    out = parse_nba_referee_assignments_html(page)
    assert len(out.assignments) == 1


def test_parse_skips_other_tables_lacking_required_headers() -> None:
    # Pages often include a footer / navigation table. We match only on
    # the header signature ``Game / Crew Chief / Referee / Umpire``.
    page = _build_page(
        nba_rows_html=_nba_row("BOS @ NYK", "Brothers (#25)", "Watson (#6)", "Burns (#8)")
    ).replace(
        "</body>",
        "<table><thead><tr><th>Footer</th><th>Nav</th></tr></thead>"
        "<tbody><tr><td>x</td><td>y</td></tr></tbody></table></body>",
    )
    out = parse_nba_referee_assignments_html(page)
    assert len(out.assignments) == 1


# -- HTTP client wrapper -------------------------------------------------


class _StubHttpClient:
    def __init__(self, *, status_code: int = 200, body: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, "params": kwargs.get("params"), "headers": kwargs.get("headers")})
        return httpx.Response(
            status_code=self.status_code,
            text=self.body,
            request=httpx.Request("GET", url),
        )


def test_client_fetches_and_parses_today_when_no_date_passed() -> None:
    body = _build_page(
        nba_rows_html=_nba_row("BOS @ NYK", "Brothers (#25)", "Watson (#6)", "Burns (#8)")
    )
    stub = _StubHttpClient(body=body)
    client = NbaRefereeAssignmentsClient(http_client=stub)
    out = client.fetch_assignments()
    assert isinstance(out, NbaRefereeAssignmentDay)
    assert len(out.assignments) == 1
    assert out.assignments[0].matchup == "BOS @ NYK"
    # No date param when no date passed.
    assert stub.calls[0]["params"] is None


def test_client_sends_mmddyyyy_date_filter() -> None:
    stub = _StubHttpClient(body=_build_page())
    client = NbaRefereeAssignmentsClient(http_client=stub)
    client.fetch_assignments(target_date=date_cls(2026, 1, 5))
    assert stub.calls[0]["params"] == {"date": "01/05/2026"}


def test_client_sends_user_agent_header_by_default() -> None:
    stub = _StubHttpClient(body=_build_page())
    client = NbaRefereeAssignmentsClient(http_client=stub)
    client.fetch_assignments()
    headers = stub.calls[0]["headers"] or {}
    assert headers.get("User-Agent")  # non-empty


def test_client_raises_on_http_5xx() -> None:
    stub = _StubHttpClient(status_code=503, body="<html>oops</html>")
    client = NbaRefereeAssignmentsClient(http_client=stub)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_assignments()
