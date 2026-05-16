// Smarter #25 — vitest coverage for the mapping review queue + detail
// drawer. Mocks the API layer; verifies wire-up + override flow end
// to end.

import { render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { SWRConfig } from "swr";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MappingsDesk } from "@/components/ops/mappings-desk";
import type {
  MarketMappingListItemRead,
  MarketMappingStateRead,
} from "@/lib/types";

const {
  mockFetchOpsMappings,
  mockFetchOpsMapping,
  mockSubmitOpsMappingOverride,
} = vi.hoisted(() => ({
  mockFetchOpsMappings: vi.fn(),
  mockFetchOpsMapping: vi.fn(),
  mockSubmitOpsMappingOverride: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchOpsMappings: mockFetchOpsMappings,
    fetchOpsMapping: mockFetchOpsMapping,
    submitOpsMappingOverride: mockSubmitOpsMappingOverride,
  };
});

// Local provider — keeps SWR cache isolated per test and disables
// auto-revalidation so the assertions don't race against background
// refetches.
function renderWithProviders(ui: ReactElement) {
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <SWRConfig
        value={{
          provider: () => new Map(),
          dedupingInterval: 0,
          shouldRetryOnError: false,
          revalidateOnFocus: false,
          revalidateOnReconnect: false,
        }}
      >
        {children}
      </SWRConfig>
    );
  }
  return rtlRender(ui, { wrapper: Wrapper });
}

function buildListRow(
  overrides: Partial<MarketMappingListItemRead> = {},
): MarketMappingListItemRead {
  return {
    ticker: "NBA-T1",
    title: "Lakers @ Celtics",
    sport_key: "NBA",
    event_id: 100,
    event_name: "Lakers @ Celtics",
    mapping_confidence: 0.5,
    candidate_count: 2,
    top_candidate_event_id: 100,
    top_candidate_event_name: "Lakers @ Celtics",
    top_candidate_score: 0.5,
    mapping_overridden_at: null,
    mapping_overridden_reason: null,
    ...overrides,
  };
}

function buildDetail(
  overrides: Partial<MarketMappingStateRead> = {},
): MarketMappingStateRead {
  return {
    ticker: "NBA-T1",
    event_id: 100,
    sport_key: "NBA",
    mapping_confidence: 0.5,
    mapping_candidates: [
      {
        event_id: 100,
        event_name: "Lakers @ Celtics",
        sport_key: "NBA",
        score: 0.5,
        time_delta_seconds: 600,
      },
      {
        event_id: 200,
        event_name: "Warriors @ Suns",
        sport_key: "NBA",
        score: 0.42,
        time_delta_seconds: 7200,
      },
    ],
    mapping_overridden_at: null,
    mapping_overridden_reason: null,
    ...overrides,
  };
}

describe("MappingsDesk", () => {
  beforeEach(() => {
    mockFetchOpsMappings.mockReset();
    mockFetchOpsMapping.mockReset();
    mockSubmitOpsMappingOverride.mockReset();
    // Radix Select uses Element.hasPointerCapture / setPointerCapture
    // (Pointer Events spec). jsdom doesn't implement them; without
    // these polyfills the Select trigger crashes on click. Patch
    // before each test so the Select tests run cleanly.
    if (typeof Element !== "undefined") {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (Element.prototype as any).hasPointerCapture = (Element.prototype as any).hasPointerCapture
        ?? (() => false);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (Element.prototype as any).setPointerCapture = (Element.prototype as any).setPointerCapture
        ?? (() => {});
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (Element.prototype as any).releasePointerCapture = (Element.prototype as any).releasePointerCapture
        ?? (() => {});
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (Element.prototype as any).scrollIntoView = (Element.prototype as any).scrollIntoView
        ?? (() => {});
    }
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders the queue header even when the list is empty", async () => {
    mockFetchOpsMappings.mockResolvedValue([]);

    renderWithProviders(<MappingsDesk />);

    expect(await screen.findByText(/Review queue/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/Nothing to review/)).toBeInTheDocument();
    });
  });

  it("auto-selects the first row and fetches its detail", async () => {
    mockFetchOpsMappings.mockResolvedValue([buildListRow()]);
    mockFetchOpsMapping.mockResolvedValue(buildDetail());

    renderWithProviders(<MappingsDesk />);

    await waitFor(() => {
      expect(mockFetchOpsMapping).toHaveBeenCalledWith("NBA-T1");
    });
    // Candidate names show up once detail loads.
    expect(await screen.findByText("Warriors @ Suns")).toBeInTheDocument();
  });

  it("marks the current mapping with the 'current' button label", async () => {
    mockFetchOpsMappings.mockResolvedValue([buildListRow()]);
    mockFetchOpsMapping.mockResolvedValue(buildDetail());

    renderWithProviders(<MappingsDesk />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "current" })).toBeInTheDocument();
    });
    // Non-current candidates get a "pin" button.
    expect(screen.getByRole("button", { name: "pin" })).toBeInTheDocument();
  });

  it("submits the override + reason when pin is clicked", async () => {
    mockFetchOpsMappings.mockResolvedValue([buildListRow()]);
    mockFetchOpsMapping.mockResolvedValue(buildDetail());
    mockSubmitOpsMappingOverride.mockResolvedValue(
      buildDetail({ event_id: 200 }),
    );

    const user = userEvent.setup();
    renderWithProviders(<MappingsDesk />);

    const reason = await screen.findByLabelText(/Override note/);
    await user.type(reason, "doubleheader disambiguation");
    await user.click(screen.getByRole("button", { name: "pin" }));

    await waitFor(() => {
      expect(mockSubmitOpsMappingOverride).toHaveBeenCalledWith("NBA-T1", {
        event_id: 200,
        reason: "doubleheader disambiguation",
      });
    });
  });

  it("clear mapping passes event_id=null", async () => {
    mockFetchOpsMappings.mockResolvedValue([buildListRow()]);
    mockFetchOpsMapping.mockResolvedValue(buildDetail());
    mockSubmitOpsMappingOverride.mockResolvedValue(
      buildDetail({ event_id: null }),
    );

    const user = userEvent.setup();
    renderWithProviders(<MappingsDesk />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /clear mapping/ })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: /clear mapping/ }));

    await waitFor(() => {
      expect(mockSubmitOpsMappingOverride).toHaveBeenCalledWith("NBA-T1", {
        event_id: null,
        reason: null,
      });
    });
  });

  it("disables clear mapping when nothing is mapped", async () => {
    mockFetchOpsMappings.mockResolvedValue([
      buildListRow({ event_id: null, event_name: null }),
    ]);
    mockFetchOpsMapping.mockResolvedValue(
      buildDetail({ event_id: null }),
    );

    renderWithProviders(<MappingsDesk />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /clear mapping/ })).toBeDisabled();
    });
  });

  it("re-fetches the list when the sport filter changes", async () => {
    mockFetchOpsMappings.mockResolvedValue([buildListRow()]);
    mockFetchOpsMapping.mockResolvedValue(buildDetail());

    const user = userEvent.setup();
    renderWithProviders(<MappingsDesk />);

    await waitFor(() => {
      expect(mockFetchOpsMappings).toHaveBeenCalled();
    });
    const callsBefore = mockFetchOpsMappings.mock.calls.length;

    await user.click(screen.getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "MLB" }));

    await waitFor(() => {
      expect(mockFetchOpsMappings.mock.calls.length).toBeGreaterThan(callsBefore);
    });
    const lastCallArgs = mockFetchOpsMappings.mock.calls.at(-1)?.[0];
    expect(lastCallArgs).toMatchObject({ sport: "MLB" });
  });

  it("toggles include-overridden", async () => {
    mockFetchOpsMappings.mockResolvedValue([buildListRow()]);
    mockFetchOpsMapping.mockResolvedValue(buildDetail());

    const user = userEvent.setup();
    renderWithProviders(<MappingsDesk />);

    await waitFor(() => {
      expect(mockFetchOpsMappings).toHaveBeenCalled();
    });
    const callsBefore = mockFetchOpsMappings.mock.calls.length;

    await user.click(screen.getByLabelText(/include overridden/));

    await waitFor(() => {
      expect(mockFetchOpsMappings.mock.calls.length).toBeGreaterThan(callsBefore);
    });
    const lastArgs = mockFetchOpsMappings.mock.calls.at(-1)?.[0];
    expect(lastArgs).toMatchObject({ includeOverridden: true });
  });

  it("changing the confidence preset re-fetches with the new ceiling", async () => {
    mockFetchOpsMappings.mockResolvedValue([buildListRow()]);
    mockFetchOpsMapping.mockResolvedValue(buildDetail());

    const user = userEvent.setup();
    renderWithProviders(<MappingsDesk />);

    await waitFor(() => {
      expect(mockFetchOpsMappings).toHaveBeenCalled();
    });
    const callsBefore = mockFetchOpsMappings.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "< 0.3" }));

    await waitFor(() => {
      expect(mockFetchOpsMappings.mock.calls.length).toBeGreaterThan(callsBefore);
    });
    expect(mockFetchOpsMappings.mock.calls.at(-1)?.[0]).toMatchObject({
      maxConfidence: 0.3,
    });
  });
});
