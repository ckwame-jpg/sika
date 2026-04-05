"use client";

import { ThemeProvider } from "next-themes";
import { SWRConfig } from "swr";
import { PriceDisplayProvider } from "@/lib/price-display";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      disableTransitionOnChange
    >
      <SWRConfig
        value={{
          errorRetryCount: 3,
          errorRetryInterval: 2000,
          shouldRetryOnError: (err: unknown) => {
            const status = (err as { status?: number })?.status;
            return !status || status >= 500;
          },
        }}
      >
        <PriceDisplayProvider>{children}</PriceDisplayProvider>
      </SWRConfig>
    </ThemeProvider>
  );
}
