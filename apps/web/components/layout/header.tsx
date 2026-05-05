"use client";

import Link from "next/link";
import { OperatorBanner } from "@/components/layout/operator-banner";
import { ProductFreshnessBanner } from "@/components/layout/product-freshness-banner";

interface HeaderProps {
  title: string;
  actions?: React.ReactNode;
}

export function Header({ title, actions }: HeaderProps) {
  return (
    <header className="topbar">
      <div className="crumbs">
        <span className="crumb-orb" />
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
        <span className="topbar-chip chip-live">
          <span className="dot" />
          Live
        </span>
      </div>
    </header>
  );
}
