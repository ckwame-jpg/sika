"use client";

import { ThemeProvider } from "next-themes";
import { PriceDisplayMode, PriceDisplayProvider } from "@/lib/price-display";

interface ProvidersProps {
  children: React.ReactNode;
  initialPriceMode?: PriceDisplayMode;
}

export function Providers({ children, initialPriceMode }: ProvidersProps) {
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      disableTransitionOnChange
    >
      <PriceDisplayProvider initialMode={initialPriceMode}>{children}</PriceDisplayProvider>
    </ThemeProvider>
  );
}
