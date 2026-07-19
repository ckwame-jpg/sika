"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import useSWR from "swr";
import { OperatorBanner } from "@/components/layout/operator-banner";
import { ProductFreshnessBanner } from "@/components/layout/product-freshness-banner";
import { UserSwitcher } from "@/components/layout/user-switcher";
import { fetchMyKalshiCredentials, keys } from "@/lib/api";
import { kalshiEnvLabel } from "@/lib/kalshi-env";

interface HeaderProps {
  title: string;
  actions?: React.ReactNode;
}

const OPS_PREFIXES = ["/runs", "/mappings", "/settings"];

/* Per-screen status pill (glass-instrument spec §Main Header). */
function modePill(pathname: string): React.ReactNode {
  if (
    pathname.startsWith("/trade") ||
    pathname.startsWith("/predictions") ||
    OPS_PREFIXES.some((p) => pathname.startsWith(p))
  ) {
    return <span className="topbar-chip chip-info">instrument mode</span>;
  }
  if (pathname.startsWith("/positions")) {
    return <span className="topbar-chip chip-info">paper mode</span>;
  }
  return null;
}

/** Kalshi trading-environment chip — replaces the old hardcoded "Live"
 * pill. Driven by the user's stored credentials: nothing until an
 * account is connected, then "kalshi demo" (sandbox) or "live · kalshi"
 * so real-money mode is visible on every screen. */
function KalshiEnvChip() {
  const { data: creds } = useSWR(keys.myKalshiCredentials, fetchMyKalshiCredentials, {
    // Header mounts on every screen — one fetch per session is plenty.
    revalidateOnFocus: false,
  });
  if (!creds?.configured) return null;
  const env = kalshiEnvLabel(creds.base_url);
  if (env === "demo") {
    return (
      <span className="topbar-chip chip-info" data-testid="header-kalshi-env">
        kalshi demo
      </span>
    );
  }
  return (
    <span className="topbar-chip chip-live" data-testid="header-kalshi-env">
      <span className="dot" />
      live · kalshi
    </span>
  );
}

export function Header({ title, actions }: HeaderProps) {
  const pathname = usePathname();
  const inOps = OPS_PREFIXES.some((p) => pathname.startsWith(p));

  return (
    <header className="topbar">
      <div className="crumbs">
        <span className="crumb-orb" aria-hidden />
        <Link href="/" className="crumb-root">
          sika
        </Link>
        {inOps && (
          <>
            <span className="sep">/</span>
            <span className="crumb-sub">operator</span>
          </>
        )}
        <span className="sep">/</span>
        <span className="crumb-page">{title}</span>
      </div>
      <span className="spacer" />
      <div className="topbar-right">
        <div className="topbar-chips">
          <OperatorBanner />
          <ProductFreshnessBanner />
          {actions}
          {modePill(pathname)}
        </div>
        {/* Multi-user batch PR 2 — renders nothing in single-tenant
            mode (SIKA_USERS empty), a clickable dropdown once users
            are configured. Placed between the chips and the Live
            indicator so it's the rightmost interactive element. */}
        <UserSwitcher />
        <KalshiEnvChip />
      </div>
    </header>
  );
}
