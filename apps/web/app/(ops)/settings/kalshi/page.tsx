"use client";

import { useState } from "react";
import useSWR, { mutate } from "swr";
import { ExternalLink, Link2Off } from "lucide-react";
import { Header } from "@/components/layout/header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  deleteMyKalshiCredentials,
  fetchMe,
  fetchMyKalshiCredentials,
  keys,
  saveMyKalshiCredentials,
} from "@/lib/api";
import type { CurrentUserRead, UserKalshiCredentialsRead } from "@/lib/types";
import { fmtDatetime } from "@/lib/utils";

/**
 * Multi-user batch PR 5 — /settings/kalshi.
 *
 * Per-user Kalshi credential entry. The currently-selected user pastes
 * their API Key ID + the contents of their RSA private key (PEM) and
 * picks demo or prod. Once saved, the per-user account snapshot path
 * (PR 4) uses these creds instead of the env var.
 *
 * The private key is stored plaintext in the DB per the locked
 * planning decision (matches existing .env security profile). The
 * GET endpoint returns metadata only — never the PEM — so re-loading
 * this page doesn't expose the saved key.
 */

const PROD_URL = "https://api.elections.kalshi.com/trade-api/v2";
const DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2";

export default function SettingsKalshiPage() {
  const { data: me } = useSWR<CurrentUserRead>(keys.me, fetchMe);
  const { data: creds } = useSWR<UserKalshiCredentialsRead>(
    me?.user ? keys.myKalshiCredentials : null,
    fetchMyKalshiCredentials,
  );

  const [keyId, setKeyId] = useState("");
  const [privateKeyPem, setPrivateKeyPem] = useState("");
  const [environment, setEnvironment] = useState<"prod" | "demo">("prod");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState(false);

  if (!me?.user) {
    return (
      <>
        <Header title="settings · kalshi" />
        <main className="cosmos-shell">
          <div className="cosmos-shell-inner">
            <p className="text-sm text-muted-foreground">
              Pick a user from the topbar dropdown before configuring Kalshi
              credentials.
            </p>
          </div>
        </main>
      </>
    );
  }

  async function handleSave(event: React.FormEvent) {
    event.preventDefault();
    if (!keyId.trim() || !privateKeyPem.trim()) return;
    setSubmitting(true);
    setError(null);
    setSavedFlash(false);
    try {
      await saveMyKalshiCredentials({
        key_id: keyId.trim(),
        private_key_pem: privateKeyPem,
        base_url: environment === "demo" ? DEMO_URL : PROD_URL,
      });
      await Promise.all([
        mutate(keys.myKalshiCredentials),
        mutate(keys.positions), // /positions panel refetches with new creds
      ]);
      setPrivateKeyPem(""); // Don't keep the PEM in component state after save.
      setSavedFlash(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save credentials.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDisconnect() {
    setError(null);
    try {
      await deleteMyKalshiCredentials();
      await Promise.all([
        mutate(keys.myKalshiCredentials),
        mutate(keys.positions),
      ]);
      setKeyId("");
      setPrivateKeyPem("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to disconnect.");
    }
  }

  return (
    <>
      <Header title="settings · kalshi" />
      <main className="cosmos-shell">
        <div className="cosmos-shell-inner space-y-6">
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">
                  Connect Kalshi as {me.user.display_name || me.user.username}
                </h2>
                <p className="cosmos-panel-desc">
                  Paste your Kalshi API key ID and the contents of your RSA
                  private key file. The credentials are stored in the local sika
                  database and used to fetch your portfolio + submit demo
                  orders. Re-saving overwrites the previous value.{" "}
                  <a
                    className="inline-flex items-center gap-1 text-positive hover:underline"
                    href="https://trading-api.readme.io/reference/api-key-management"
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    Where to get a key
                    <ExternalLink size={11} />
                  </a>
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body">
              <div
                className="mb-4 rounded border border-border bg-surface-hover/40 px-3 py-2 text-sm"
                data-testid="settings-kalshi-status"
              >
                {creds?.configured ? (
                  <span className="flex items-center justify-between gap-3">
                    <span className="flex flex-col gap-px">
                      <span className="text-foreground">
                        Connected as{" "}
                        <span className="font-mono">{creds.key_id}</span>
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {creds.base_url?.includes("demo") ? "Demo" : "Prod"} ·
                        last updated{" "}
                        {creds.updated_at ? fmtDatetime(creds.updated_at) : "—"}
                      </span>
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => void handleDisconnect()}
                      data-testid="settings-kalshi-disconnect"
                    >
                      <Link2Off size={13} className="mr-1.5" />
                      Disconnect
                    </Button>
                  </span>
                ) : (
                  <span className="text-muted-foreground">
                    Not connected. Paste credentials below to enable the Kalshi
                    Account panel + demo orders for this user.
                  </span>
                )}
              </div>

              <form
                onSubmit={handleSave}
                className="grid gap-3 max-w-xl"
                data-testid="settings-kalshi-form"
              >
                <label className="grid gap-1" htmlFor="kalshi-key-id">
                  <span className="text-xs uppercase tracking-[0.08em] text-muted-foreground">
                    API Key ID
                  </span>
                  <Input
                    id="kalshi-key-id"
                    data-testid="settings-kalshi-key-id"
                    value={keyId}
                    onChange={(e) => setKeyId(e.target.value)}
                    placeholder="123e4567-e89b-12d3-a456-426614174000"
                    autoComplete="off"
                  />
                </label>
                <label className="grid gap-1" htmlFor="kalshi-pem">
                  <span className="text-xs uppercase tracking-[0.08em] text-muted-foreground">
                    Private key (PEM contents)
                  </span>
                  <textarea
                    id="kalshi-pem"
                    data-testid="settings-kalshi-pem"
                    value={privateKeyPem}
                    onChange={(e) => setPrivateKeyPem(e.target.value)}
                    placeholder="-----BEGIN PRIVATE KEY-----&#10;...&#10;-----END PRIVATE KEY-----"
                    rows={6}
                    className="min-h-[120px] rounded border border-border bg-surface px-3 py-2 font-mono text-xs"
                    autoComplete="off"
                  />
                </label>
                <label className="grid gap-1" htmlFor="kalshi-env">
                  <span className="text-xs uppercase tracking-[0.08em] text-muted-foreground">
                    Environment
                  </span>
                  <select
                    id="kalshi-env"
                    data-testid="settings-kalshi-env"
                    value={environment}
                    onChange={(e) => setEnvironment(e.target.value as "prod" | "demo")}
                    className="rounded border border-border bg-surface px-3 py-2 text-sm"
                  >
                    <option value="prod">Production (api.elections.kalshi.com)</option>
                    <option value="demo">Demo / sandbox (demo-api.kalshi.co)</option>
                  </select>
                </label>
                {error && (
                  <p
                    role="alert"
                    className="text-xs text-negative"
                    data-testid="settings-kalshi-error"
                  >
                    {error}
                  </p>
                )}
                {savedFlash && (
                  <p
                    role="status"
                    className="text-xs text-positive"
                    data-testid="settings-kalshi-saved"
                  >
                    Saved. The Kalshi panel on Portfolio should now show your account.
                  </p>
                )}
                <Button
                  type="submit"
                  variant="primary"
                  size="sm"
                  disabled={submitting || !keyId.trim() || !privateKeyPem.trim()}
                  data-testid="settings-kalshi-save"
                >
                  {submitting ? "Saving…" : creds?.configured ? "Update credentials" : "Connect Kalshi"}
                </Button>
              </form>
            </div>
          </section>
        </div>
      </main>
    </>
  );
}
