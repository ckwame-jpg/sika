"use client";

import { useState } from "react";
import { ChevronDown, LogOut, User as UserIcon } from "lucide-react";
import useSWR, { mutate } from "swr";
import { fetchMe, fetchUsers, keys, signOut, switchUser } from "@/lib/api";
import type { CurrentUserRead, UserRead } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Multi-user batch PR 2 — topbar user switcher.
 *
 * Dropdown showing the configured users (from /users) with the current
 * pick checkmarked. Clicking a name POSTs /users/switch + mutates the
 * /me + /users SWR keys so any consumer (per-user portfolio table in
 * PR 3, KalshiAccountPanel gate in PR 5) re-renders without a manual
 * reload. "Sign out" clears the cookie and lands the operator in the
 * "pick a user" state.
 *
 * No-op when no users are configured (SIKA_USERS empty / single-tenant
 * mode) — the component renders nothing so existing operators don't
 * see a UI change.
 */
export function UserSwitcher() {
  const [open, setOpen] = useState(false);
  const { data: me } = useSWR<CurrentUserRead>(keys.me, fetchMe, {
    revalidateOnFocus: false,
  });
  const { data: users } = useSWR<UserRead[]>(keys.users, fetchUsers, {
    revalidateOnFocus: false,
  });

  // Single-tenant: no users configured → render nothing. Keeps the
  // topbar unchanged for operators who haven't opted into multi-user.
  if (!users || users.length === 0) return null;

  const current = me?.user ?? null;

  async function handleSwitch(username: string) {
    if (current?.username === username) {
      setOpen(false);
      return;
    }
    try {
      await switchUser({ username });
      // Revalidate /me so the topbar updates, and /positions so the
      // portfolio's per-user tables (PR 3) refetch into the new user.
      await Promise.all([mutate(keys.me), mutate(keys.positions)]);
    } finally {
      setOpen(false);
    }
  }

  async function handleSignOut() {
    try {
      await signOut();
      await Promise.all([mutate(keys.me), mutate(keys.positions)]);
    } finally {
      setOpen(false);
    }
  }

  const label = current?.display_name || current?.username || "Pick user";

  return (
    <div className="user-switcher" data-testid="user-switcher">
      <button
        type="button"
        className="user-switcher-trigger focus-visible:ring-focus"
        onClick={() => setOpen((prev) => !prev)}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="user-switcher-trigger"
      >
        <UserIcon size={12} />
        <span className="user-switcher-trigger-label">{label}</span>
        <ChevronDown size={11} aria-hidden />
      </button>
      {open && (
        <div
          role="menu"
          className="user-switcher-menu"
          data-testid="user-switcher-menu"
        >
          <ul className="user-switcher-list">
            {users.map((user) => {
              const isCurrent = current?.username === user.username;
              return (
                <li key={user.id}>
                  <button
                    type="button"
                    role="menuitemradio"
                    aria-checked={isCurrent}
                    onClick={() => void handleSwitch(user.username)}
                    className={cn(
                      "user-switcher-item focus-visible:ring-focus",
                      isCurrent && "is-current",
                    )}
                    data-testid={`user-switcher-item-${user.username}`}
                  >
                    <span className="user-switcher-item-label">
                      {user.display_name || user.username}
                    </span>
                    {user.is_kalshi_owner && (
                      <span className="user-switcher-kalshi-badge">kalshi</span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
          {current && (
            <button
              type="button"
              onClick={() => void handleSignOut()}
              className="user-switcher-signout focus-visible:ring-focus"
              data-testid="user-switcher-signout"
            >
              <LogOut size={11} aria-hidden />
              sign out
            </button>
          )}
        </div>
      )}
    </div>
  );
}
