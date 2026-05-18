import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { updateModelReadinessSettings } from "./api";

/**
 * Bug #235 — the PATCH ``/ops/models/readiness/settings`` endpoint
 * used to return the full ``ModelReadinessSummaryRead`` shape, which
 * forced ~22s of server-side summary-build work inside the request
 * handler. That blew past the 15s client timeout in
 * ``apps/web/lib/api.ts``, so every settings-page click surfaced a
 * "request timed out" overlay even though the write itself
 * completed in milliseconds.
 *
 * The fix splits the response: PATCH returns the lightweight
 * ``{applied: true}`` ack, and the frontend re-fetches the
 * canonical summary via ``GET /ops/models/readiness`` (the SWR
 * mutate in ``settings/page.tsx`` and ``model-readiness-panel.tsx``).
 *
 * These tests pin the wire contract the rest of the frontend
 * relies on:
 *
 * 1. The fetcher sends a PATCH with the JSON body verbatim — no
 *    fields are dropped, renamed, or stringified differently.
 * 2. The fetcher's return type matches the new lightweight shape.
 *    If a future change re-introduces the summary shape on the
 *    response, the consumer in ``model-readiness-panel.tsx`` (which
 *    no longer types-checks against the summary) breaks loudly.
 */
describe("updateModelReadinessSettings — Bug #235 PATCH ack contract", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("sends a PATCH to /ops/models/readiness/settings with the payload as JSON", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ applied: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await updateModelReadinessSettings({ narrator_enabled: true });

    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/ops/models/readiness/settings");
    expect(init.method).toBe("PATCH");
    expect(init.body).toBe(JSON.stringify({ narrator_enabled: true }));
  });

  it("returns the lightweight ack body to the caller", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ applied: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await updateModelReadinessSettings({
      pick_history_default_n: 10,
    });

    // The new contract is just ``{applied: true}`` — pinning the
    // exact shape so a future change that brings back summary
    // fields trips this test before it ships.
    expect(result).toEqual({ applied: true });
  });

  it("forwards partial payloads verbatim (narrator-only)", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ applied: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await updateModelReadinessSettings({ narrator_enabled: false });

    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBe(JSON.stringify({ narrator_enabled: false }));
  });

  it("forwards partial payloads verbatim (depth-only)", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ applied: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await updateModelReadinessSettings({ pick_history_default_n: 20 });

    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBe(JSON.stringify({ pick_history_default_n: 20 }));
  });
});
