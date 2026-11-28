import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";
import { Nav, NavBar } from "@/components/Nav";
import { Providers } from "./providers";
import "./globals.css";

const sans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const mono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Fairline",
  description: "Sports betting analytics and decision support. Fairline never places bets.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${sans.variable} ${mono.variable} min-h-screen antialiased`}>
        <Providers>
          <Suspense fallback={<NavBar />}>
            <Nav />
          </Suspense>
          <main className="mx-auto max-w-[1400px] px-4 py-6">{children}</main>
          <footer className="mx-auto max-w-[1400px] px-4 pb-8 text-xs text-muted">
            Decision support only. Every edge here is an estimate with real uncertainty; nothing is guaranteed and
            Fairline never places bets.
          </footer>
        </Providers>
      </body>
    </html>
  );
}
