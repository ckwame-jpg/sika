"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { MobileSidebarTrigger } from "@/components/layout/sidebar";
import { OperatorBanner } from "@/components/layout/operator-banner";
import { ProductFreshnessBanner } from "@/components/layout/product-freshness-banner";

interface HeaderProps {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}

function localTzLabel(): string {
  try {
    const offsetMin = -new Date().getTimezoneOffset();
    if (offsetMin === 0) return "UTC";
    const sign = offsetMin > 0 ? "+" : "-";
    const abs = Math.abs(offsetMin);
    const hours = Math.floor(abs / 60);
    const minutes = abs % 60;
    return minutes === 0 ? `UTC${sign}${hours}` : `UTC${sign}${hours}:${String(minutes).padStart(2, "0")}`;
  } catch {
    return "UTC";
  }
}

export function Header({ title, description, actions }: HeaderProps) {
  const [tz, setTz] = useState("UTC");

  useEffect(() => {
    setTz(localTzLabel());
  }, []);

  return (
    <header className="topbar">
      <div className="lg:hidden -ml-1">
        <MobileSidebarTrigger />
      </div>
      <div className="crumbs">
        <span className="crumb-orb" />
        <Link href="/" className="crumb-root">
          sika
        </Link>
        <span className="sep">/</span>
        <span className="crumb-page">{title}</span>
        {description && (
          <>
            <span className="sep">/</span>
            <span className="crumb-sub">{description}</span>
          </>
        )}
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
        <span className="topbar-chip chip-utc">{tz}</span>
      </div>
    </header>
  );
}
