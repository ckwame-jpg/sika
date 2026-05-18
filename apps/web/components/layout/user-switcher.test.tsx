import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { renderWithProviders } from "@/test/render";
import { UserSwitcher } from "./user-switcher";
import type { CurrentUserRead, UserRead } from "@/lib/types";

const { mockFetchMe, mockFetchUsers, mockSwitchUser, mockSignOut, mockMutate } = vi.hoisted(() => ({
  mockFetchMe: vi.fn(),
  mockFetchUsers: vi.fn(),
  mockSwitchUser: vi.fn(),
  mockSignOut: vi.fn(),
  mockMutate: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchMe: mockFetchMe,
    fetchUsers: mockFetchUsers,
    switchUser: mockSwitchUser,
    signOut: mockSignOut,
  };
});

vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, mutate: mockMutate };
});

function userRow(overrides: Partial<UserRead> = {}): UserRead {
  return { id: 1, username: "chris", display_name: "chris", is_kalshi_owner: true, ...overrides };
}

function me(user: UserRead | null): CurrentUserRead {
  return { user };
}

beforeEach(() => {
  mockFetchMe.mockReset();
  mockFetchUsers.mockReset();
  mockSwitchUser.mockReset();
  mockSignOut.mockReset();
  mockMutate.mockReset();
});

describe("UserSwitcher", () => {
  it("renders nothing in single-tenant mode (no users configured)", async () => {
    mockFetchUsers.mockResolvedValue([]);
    mockFetchMe.mockResolvedValue(me(null));
    const { container } = renderWithProviders(<UserSwitcher />);
    // Wait a tick for SWR to settle so we don't false-positive on
    // first render.
    await waitFor(() => expect(mockFetchUsers).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders 'pick user' when no current user is set", async () => {
    mockFetchUsers.mockResolvedValue([
      userRow({ id: 1, username: "chris", is_kalshi_owner: true }),
      userRow({ id: 2, username: "canaan", is_kalshi_owner: false }),
    ]);
    mockFetchMe.mockResolvedValue(me(null));
    renderWithProviders(<UserSwitcher />);
    await waitFor(() => expect(screen.getByTestId("user-switcher-trigger")).toBeInTheDocument());
    expect(screen.getByTestId("user-switcher-trigger")).toHaveTextContent(/pick user/i);
  });

  it("renders the current user's display_name once selected", async () => {
    mockFetchUsers.mockResolvedValue([userRow()]);
    mockFetchMe.mockResolvedValue(me(userRow({ display_name: "Chris" })));
    renderWithProviders(<UserSwitcher />);
    await waitFor(() =>
      expect(screen.getByTestId("user-switcher-trigger")).toHaveTextContent(/Chris/),
    );
  });

  it("opens the dropdown on trigger click and shows all users + kalshi badge", async () => {
    mockFetchUsers.mockResolvedValue([
      userRow({ id: 1, username: "chris", is_kalshi_owner: true }),
      userRow({ id: 2, username: "canaan", is_kalshi_owner: false }),
    ]);
    mockFetchMe.mockResolvedValue(me(userRow()));
    renderWithProviders(<UserSwitcher />);
    await waitFor(() => expect(screen.getByTestId("user-switcher-trigger")).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByTestId("user-switcher-trigger"));
    expect(screen.getByTestId("user-switcher-menu")).toBeInTheDocument();
    expect(screen.getByTestId("user-switcher-item-chris")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("user-switcher-item-canaan")).toHaveAttribute("aria-checked", "false");
    // Kalshi badge appears on chris (the kalshi owner).
    const chrisItem = screen.getByTestId("user-switcher-item-chris");
    expect(chrisItem).toHaveTextContent(/kalshi/i);
    const canaanItem = screen.getByTestId("user-switcher-item-canaan");
    expect(canaanItem).not.toHaveTextContent(/kalshi/i);
  });

  it("clicking a different user POSTs /users/switch and mutates /me + /positions", async () => {
    mockFetchUsers.mockResolvedValue([
      userRow({ id: 1, username: "chris" }),
      userRow({ id: 2, username: "canaan", is_kalshi_owner: false }),
    ]);
    mockFetchMe.mockResolvedValue(me(userRow()));
    mockSwitchUser.mockResolvedValue(me(userRow({ id: 2, username: "canaan" })));
    renderWithProviders(<UserSwitcher />);
    await waitFor(() => expect(screen.getByTestId("user-switcher-trigger")).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByTestId("user-switcher-trigger"));
    await user.click(screen.getByTestId("user-switcher-item-canaan"));

    await waitFor(() => expect(mockSwitchUser).toHaveBeenCalledWith({ username: "canaan" }));
    // /me + /positions both re-validated so any per-user UI refetches.
    expect(mockMutate).toHaveBeenCalledWith("/me");
    expect(mockMutate).toHaveBeenCalledWith("/positions");
  });

  it("clicking the already-current user is a no-op (no API call)", async () => {
    mockFetchUsers.mockResolvedValue([userRow()]);
    mockFetchMe.mockResolvedValue(me(userRow()));
    renderWithProviders(<UserSwitcher />);
    await waitFor(() => expect(screen.getByTestId("user-switcher-trigger")).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByTestId("user-switcher-trigger"));
    await user.click(screen.getByTestId("user-switcher-item-chris"));

    expect(mockSwitchUser).not.toHaveBeenCalled();
  });

  it("sign out clears the cookie and mutates /me + /positions", async () => {
    mockFetchUsers.mockResolvedValue([userRow()]);
    mockFetchMe.mockResolvedValue(me(userRow()));
    mockSignOut.mockResolvedValue(me(null));
    renderWithProviders(<UserSwitcher />);
    await waitFor(() => expect(screen.getByTestId("user-switcher-trigger")).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByTestId("user-switcher-trigger"));
    await user.click(screen.getByTestId("user-switcher-signout"));

    await waitFor(() => expect(mockSignOut).toHaveBeenCalled());
    expect(mockMutate).toHaveBeenCalledWith("/me");
    expect(mockMutate).toHaveBeenCalledWith("/positions");
  });

  it("sign out button is hidden when no user is currently selected", async () => {
    mockFetchUsers.mockResolvedValue([userRow()]);
    mockFetchMe.mockResolvedValue(me(null));
    renderWithProviders(<UserSwitcher />);
    await waitFor(() => expect(screen.getByTestId("user-switcher-trigger")).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByTestId("user-switcher-trigger"));
    expect(screen.queryByTestId("user-switcher-signout")).toBeNull();
  });
});
