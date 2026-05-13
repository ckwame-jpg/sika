import type { Metadata } from "next";
import { cookies } from "next/headers";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { Providers } from "./providers";
import { Shell } from "@/components/layout/shell";
import { PRICE_DISPLAY_COOKIE, isPriceDisplayMode } from "@/lib/price-display";
import "./globals.css";

export const metadata: Metadata = {
  title: "sika",
  description: "Sports trading copilot for live events, market diagnostics, watchlists, demo trading, and parlays",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const cookieStore = await cookies();
  const storedPriceMode = cookieStore.get(PRICE_DISPLAY_COOKIE)?.value;
  const initialPriceMode = isPriceDisplayMode(storedPriceMode) ? storedPriceMode : undefined;

  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${GeistSans.variable} ${GeistMono.variable}`}
    >
      <body className="variant-E lg-orbit">
        <Providers initialPriceMode={initialPriceMode}>
          <Shell>{children}</Shell>
        </Providers>
      </body>
    </html>
  );
}
