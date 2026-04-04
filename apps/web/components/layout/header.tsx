"use client";

import { ThemeToggle } from "@/components/theme-toggle";
import { TooltipProvider } from "@/components/ui/tooltip";
import { MobileSidebarTrigger } from "@/components/layout/sidebar";

interface HeaderProps {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}

export function Header({ title, description, actions }: HeaderProps) {
  return (
    <TooltipProvider>
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-3 sm:px-5">
        <div className="flex min-w-0 items-center gap-2.5">
          <MobileSidebarTrigger />
          <div className="flex min-w-0 items-baseline gap-2.5">
            <h1 className="truncate text-sm font-semibold text-foreground">{title}</h1>
            {description && (
              <span className="hidden truncate text-xs text-muted-foreground sm:block">
                {description}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {actions}
          <ThemeToggle />
        </div>
      </header>
    </TooltipProvider>
  );
}
