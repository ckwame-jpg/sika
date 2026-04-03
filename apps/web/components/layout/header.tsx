"use client";

import { ThemeToggle } from "@/components/theme-toggle";
import { TooltipProvider } from "@/components/ui/tooltip";

interface HeaderProps {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}

export function Header({ title, description, actions }: HeaderProps) {
  return (
    <TooltipProvider>
      <header className="flex items-center justify-between h-12 px-5 border-b border-border bg-surface shrink-0">
        <div className="flex items-baseline gap-2.5">
          <h1 className="text-sm font-semibold text-foreground">{title}</h1>
          {description && (
            <span className="text-xs text-muted-foreground hidden sm:block">
              {description}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {actions}
          <ThemeToggle />
        </div>
      </header>
    </TooltipProvider>
  );
}
