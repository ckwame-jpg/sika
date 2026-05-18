"use client";

import Link from "next/link";
import { OperatorBanner } from "@/components/layout/operator-banner";
import { ProductFreshnessBanner } from "@/components/layout/product-freshness-banner";
import { UserSwitcher } from "@/components/layout/user-switcher";

interface HeaderProps {
  title: string;
  actions?: React.ReactNode;
}

export function Header({ title, actions }: HeaderProps) {
  return (
    <header className="topbar">
      <div className="crumbs">
        <span className="crumb-orb" aria-hidden />
        <Link href="/" className="crumb-root">
          sika
        </Link>
        <span className="sep">/</span>
        <span className="crumb-page">{title}</span>
      </div>
      <span className="spacer" />
      <div className="topbar-right">
        <div className="topbar-chips">
          <OperatorBanner />
          <ProductFreshnessBanner />
          {actions}
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
