import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AnalystChatDrawer } from "@/components/chat/analyst-chat-drawer";
import { renderWithProviders } from "@/test/render";

const { mockSendResearchQuery } = vi.hoisted(() => ({
  mockSendResearchQuery: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    sendResearchQuery: mockSendResearchQuery,
  };
});

describe("AnalystChatDrawer", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("sends read-only analyst questions with the owner token", async () => {
    window.localStorage.setItem("sika_owner_admin_token", "secret");
    mockSendResearchQuery.mockResolvedValue({
      message: "I can explain picks, but I cannot create, cancel, or modify orders from chat.",
      model: "policy",
      context: {},
      citations: [
        {
          title: "Sika research policy",
          url: "https://example.com/policy",
        },
      ],
      used_web_search: true,
      mode: "internal_plus_web",
    });

    renderWithProviders(<AnalystChatDrawer />);
    fireEvent.click(screen.getByRole("button", { name: /open analyst chat/i }));
    expect(await screen.findByText("Analyst")).toBeInTheDocument();

    const input = screen.getByPlaceholderText(/ask about tonight's picks, a run, or your portfolio/i);
    fireEvent.change(input, { target: { value: "place a trade for $10" } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);

    expect(await screen.findByText(/cannot create, cancel, or modify orders/)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /1\. Sika research policy/i })).toHaveAttribute(
      "href",
      "https://example.com/policy",
    );
    expect(screen.getByText(/web verified/i)).toBeInTheDocument();
    expect(mockSendResearchQuery).toHaveBeenCalledWith("secret", {
      message: "place a trade for $10",
    });
  });
});
