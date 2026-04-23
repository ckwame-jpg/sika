import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EventsFeed } from "@/components/events/events-feed";
import type { EventParticipantRead, EventRead } from "@/lib/types";
import { renderWithProviders } from "@/test/render";

const { mockFetchEvents } = vi.hoisted(() => ({
  mockFetchEvents: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchEvents: mockFetchEvents,
  };
});

interface EventOverrides {
  id?: number;
  sport_key?: string;
  name?: string;
  status?: string;
  starts_at?: string;
  home?: Partial<EventParticipantRead> & { display_name: string };
  away?: Partial<EventParticipantRead> & { display_name: string };
}

function participant(
  role: "home" | "away",
  overrides: Partial<EventParticipantRead> & { display_name: string },
): EventParticipantRead {
  return {
    participant_id: overrides.participant_id ?? (role === "home" ? 1 : 2),
    display_name: overrides.display_name,
    role: overrides.role ?? role,
    is_home: role === "home",
    score: overrides.score ?? null,
    result: overrides.result ?? null,
  };
}

function makeEvent(overrides: EventOverrides = {}): EventRead {
  const id = overrides.id ?? 1;
  return {
    id,
    external_id: `fixture-${id}`,
    sport_key: overrides.sport_key ?? "NBA",
    name: overrides.name ?? "Lakers at Celtics",
    status: overrides.status ?? "scheduled",
    starts_at: overrides.starts_at ?? "2099-01-01T20:00:00Z",
    completed_at: null,
    participants: [
      participant("home", overrides.home ?? { display_name: "Celtics" }),
      participant("away", overrides.away ?? { display_name: "Lakers" }),
    ],
    raw_data: {},
  };
}

describe("EventsFeed", () => {
  it("renders the themed error card when the events fetch rejects", async () => {
    mockFetchEvents.mockRejectedValue(new Error("boom"));

    renderWithProviders(<EventsFeed mode="dashboard" />);

    expect(
      await screen.findByText("Couldn\u2019t reach the events feed."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The API didn\u2019t respond. Check that the backend is running, then try again.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(mockFetchEvents).toHaveBeenCalled();
    });
  });

  it("shows the dashboard empty state when no events are live or upcoming", async () => {
    mockFetchEvents.mockResolvedValue([]);

    renderWithProviders(<EventsFeed mode="dashboard" />);

    const messages = await screen.findAllByText(
      "No live or upcoming events found.",
    );
    expect(messages.length).toBeGreaterThan(0);
  });

  it("shows the day-mode empty state when no events match the selected date", async () => {
    mockFetchEvents.mockResolvedValue([
      makeEvent({
        id: 10,
        name: "Lakers at Celtics",
        status: "scheduled",
        starts_at: "2099-03-15T20:00:00Z",
      }),
    ]);

    renderWithProviders(<EventsFeed mode="day" day="2099-12-31" />);

    const messages = await screen.findAllByText(
      "No events matched the selected date.",
    );
    expect(messages.length).toBeGreaterThan(0);
  });

  it("renders live and scheduled events with the matching status pill variants and away-home score order", async () => {
    mockFetchEvents.mockResolvedValue([
      makeEvent({
        id: 1,
        sport_key: "SOCCER",
        name: "Arsenal vs Man City",
        status: "in_progress",
        starts_at: "2099-01-01T19:00:00Z",
        home: { display_name: "Arsenal", score: 1 },
        away: { display_name: "Man City", score: 2 },
      }),
      makeEvent({
        id: 2,
        sport_key: "NBA",
        name: "Lakers at Celtics",
        status: "scheduled",
        starts_at: "2099-01-01T23:30:00Z",
      }),
    ]);

    const { container } = renderWithProviders(
      <EventsFeed mode="dashboard" />,
    );

    await screen.findAllByText("Arsenal vs Man City");
    expect(screen.getAllByText("Lakers at Celtics").length).toBeGreaterThan(0);

    // Live pill renders with the live variant and the pulsing dot.
    const livePills = container.querySelectorAll(".event-status-pill.live");
    expect(livePills.length).toBeGreaterThan(0);
    livePills.forEach((pill) => {
      expect(pill.textContent).toContain("Live");
      expect(pill.querySelector(".live-dot")).not.toBeNull();
    });

    // Scheduled pill renders with the dashed scheduled variant.
    const scheduledPills = container.querySelectorAll(
      ".event-status-pill.scheduled",
    );
    expect(scheduledPills.length).toBeGreaterThan(0);
    scheduledPills.forEach((pill) => {
      expect(pill.textContent).toContain("Scheduled");
    });

    // Score is rendered away – home, not home – away.
    expect(screen.getAllByText("2 – 1").length).toBeGreaterThan(0);
    expect(screen.queryByText("1 – 2")).toBeNull();

    // Sport pill renders the sport label for each fixture row.
    const sportPillTexts = Array.from(
      container.querySelectorAll(".sport-pill"),
    ).map((pill) => pill.textContent ?? "");
    expect(sportPillTexts.some((text) => text.includes("Soccer"))).toBe(true);
    expect(sportPillTexts.some((text) => text.includes("NBA"))).toBe(true);
  });

  it("renders the final status pill for completed events in day mode", async () => {
    const pastDate = "2000-01-01";
    mockFetchEvents.mockResolvedValue([
      makeEvent({
        id: 3,
        sport_key: "NBA",
        name: "Knicks at Heat",
        status: "completed",
        starts_at: `${pastDate}T23:00:00Z`,
        home: { display_name: "Heat", score: 108 },
        away: { display_name: "Knicks", score: 114 },
      }),
    ]);

    const { container } = renderWithProviders(
      <EventsFeed mode="day" day={pastDate} />,
    );

    await screen.findAllByText("Knicks at Heat");

    const finalPills = container.querySelectorAll(
      ".event-status-pill.final",
    );
    expect(finalPills.length).toBeGreaterThan(0);
    finalPills.forEach((pill) => {
      expect(pill.textContent).toContain("Final");
    });

    expect(screen.getAllByText("114 – 108").length).toBeGreaterThan(0);
  });

  it("renders an em dash when no participant has a score", async () => {
    mockFetchEvents.mockResolvedValue([
      makeEvent({
        id: 4,
        name: "Alcaraz vs Sinner",
        status: "scheduled",
        starts_at: "2099-01-01T13:00:00Z",
        home: { display_name: "Sinner" },
        away: { display_name: "Alcaraz" },
      }),
    ]);

    renderWithProviders(<EventsFeed mode="dashboard" />);

    await screen.findAllByText("Alcaraz vs Sinner");

    // Appears in the desktop table cell + the mobile card tile.
    const emDashElements = screen.getAllByText("—");
    expect(emDashElements.length).toBeGreaterThan(0);

    // Em dash is rendered with muted styling in both layouts.
    const hasMutedStyling = emDashElements.some(
      (element) =>
        element.className.includes("text-muted-foreground") ||
        element.className.includes("muted"),
    );
    expect(hasMutedStyling).toBe(true);
  });
});
