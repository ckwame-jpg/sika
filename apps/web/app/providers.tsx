"use client";

import { ThemeProvider } from "next-themes";
import { PriceDisplayProvider } from "@/lib/price-display";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      disableTransitionOnChange
    >
      <PriceDisplayProvider>{children}</PriceDisplayProvider>
    </ThemeProvider>
  );
}
