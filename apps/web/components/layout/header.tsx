"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { OperatorBanner } from "@/components/layout/operator-banner";
import { ProductFreshnessBanner } from "@/components/layout/product-freshness-banner";
import { UserSwitcher } from "@/components/layout/user-switcher";

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
        <span className="topbar-chip chip-live">
          <span className="dot" />
          Live
        </span>
      </div>
    </header>
  );
}
