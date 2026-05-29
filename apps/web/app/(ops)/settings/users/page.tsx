"use client";

import { useState } from "react";
import { Trash2, UserPlus } from "lucide-react";
import useSWR, { mutate } from "swr";
import { Header } from "@/components/layout/header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  createUser,
  deleteUser,
  fetchUsers,
  keys,
} from "@/lib/api";
import type { UserRead } from "@/lib/types";

/**
 * Multi-user batch PR 5 — /settings/users.
 *
 * Lets the operator add or remove users without editing .env +
 * restarting the API. Trust model: any logged-in user can add/remove
 * others (Tailscale perimeter, operators trust each other). The
 * Kalshi-owner and the synthetic ``legacy`` bucket are protected
 * server-side; the UI surfaces their badge so the operator can see
 * which rows can't be removed.
 */

export default function SettingsUsersPage() {
  const { data: users } = useSWR<UserRead[]>(keys.users, fetchUsers);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleAdd(event: React.FormEvent) {
    event.preventDefault();
    const cleaned = username.trim();
    if (!cleaned) return;
    setSubmitting(true);
    setError(null);
    try {
      await createUser({
        username: cleaned,
        display_name: displayName.trim() || null,
      });
      setUsername("");
      setDisplayName("");
      await mutate(keys.users);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add user.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRemove(target: string) {
    setError(null);
    try {
      await deleteUser(target);
      await mutate(keys.users);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove user.");
    }
  }

  return (
    <>
      <Header title="settings · users" />
      <main className="cosmos-shell">
        <div className="cosmos-shell-inner space-y-6">
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Add a user</h2>
                <p className="cosmos-panel-desc">
                  Usernames are lowercase identifiers (letters, digits, underscore, hyphen).
                  Display name is optional and shows in the topbar dropdown.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body">
              <form onSubmit={handleAdd} className="grid gap-3 max-w-md">
                <label className="grid gap-1" htmlFor="new-user-username">
                  <span className="text-xs uppercase tracking-[0.08em] text-muted-foreground">Username</span>
                  <Input
                    id="new-user-username"
                    data-testid="settings-users-username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="canaan"
                    autoCapitalize="none"
                    autoComplete="off"
                  />
                </label>
                <label className="grid gap-1" htmlFor="new-user-display-name">
                  <span className="text-xs uppercase tracking-[0.08em] text-muted-foreground">Display name (optional)</span>
                  <Input
                    id="new-user-display-name"
                    data-testid="settings-users-display-name"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="Canaan"
                  />
                </label>
                {error && (
                  <p
                    role="alert"
                    className="text-xs text-negative"
                    data-testid="settings-users-error"
                  >
                    {error}
                  </p>
                )}
                <Button
                  type="submit"
                  variant="primary"
                  size="sm"
                  disabled={submitting || !username.trim()}
                  data-testid="settings-users-add-submit"
                >
                  <UserPlus size={13} className="mr-1.5" />
                  Add user
                </Button>
              </form>
            </div>
          </section>

          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Existing users</h2>
                <p className="cosmos-panel-desc">
                  The Kalshi owner has env-var credentials wired up and cannot be
                  removed here — update SIKA_KALSHI_OWNER in .env first. The
                  legacy bucket is a synthetic identity for historical data and
                  is hidden from this list.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body">
              <ul
                className="grid gap-2"
                data-testid="settings-users-list"
              >
                {users?.map((user) => (
                  <li
                    key={user.id}
                    className="flex items-center justify-between rounded border border-border bg-surface-hover/30 px-3 py-2"
                    data-testid={`settings-users-row-${user.username}`}
                  >
                    <div className="flex flex-col gap-px">
                      <span className="font-medium text-foreground">
                        {user.display_name || user.username}
                      </span>
                      <span className="text-xs text-muted-foreground font-mono">
                        @{user.username}
                      </span>
                    </div>
                    <div className="flex items-center gap-3">
                      {user.is_kalshi_owner && (
                        <span className="rounded-full border border-positive/40 bg-positive/10 px-2 py-px text-[10px] uppercase tracking-[0.12em] text-positive">
                          kalshi owner
                        </span>
                      )}
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={user.is_kalshi_owner}
                        onClick={() => void handleRemove(user.username)}
                        data-testid={`settings-users-remove-${user.username}`}
                      >
                        <Trash2 size={13} />
                      </Button>
                    </div>
                  </li>
                ))}
                {(!users || users.length === 0) && (
                  <li className="text-sm text-muted-foreground italic">
                    No users yet — add one above.
                  </li>
                )}
              </ul>
            </div>
          </section>
        </div>
      </main>
    </>
  );
}
