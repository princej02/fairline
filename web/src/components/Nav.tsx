"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAccaSlip } from "./AccaSlip";

const LINKS = [
  { href: "/", label: "Value board" },
  { href: "/tracker", label: "Tracker" },
  { href: "/acca", label: "Acca builder" },
  { href: "/backtests", label: "Backtests" },
];

export function Nav() {
  const path = usePathname();
  const { legs } = useAccaSlip();
  return <NavBar path={path} slipCount={legs.length} />;
}

/** Static shell, used as the Suspense fallback while the pathname streams in. */
export function NavBar({ path = "", slipCount = 0 }: { path?: string; slipCount?: number }) {
  return (
    <header className="border-b border-line bg-panel">
      <div className="mx-auto flex max-w-[1400px] items-center gap-4 px-4 py-3">
        <Link href="/" className="text-lg font-semibold tracking-tight">
          Fair<span className="text-ref">line</span>
        </Link>
        <nav className="-mx-1 flex min-w-0 gap-1 overflow-x-auto px-1 text-sm">
          {LINKS.map((l) => {
            const active = l.href === "/" ? path === "/" : Boolean(path) && path.startsWith(l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                className={`whitespace-nowrap rounded px-3 py-1.5 ${active ? "bg-panel-2 text-text" : "text-muted hover:text-text"}`}
              >
                {l.label}
                {l.href === "/acca" && slipCount > 0 && (
                  <span className="num ml-1.5 rounded bg-accent/20 px-1.5 text-xs text-accent">{slipCount}</span>
                )}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
