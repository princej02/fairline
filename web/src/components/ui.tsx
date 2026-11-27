import type { Band } from "@/lib/api";
import { pct } from "@/lib/format";

export function Badge({ tone = "muted", children, title }: { tone?: "warn" | "ref" | "muted" | "pos" | "neg" | "accent"; children: React.ReactNode; title?: string }) {
  const tones = {
    warn: "border-warn/40 bg-warn/10 text-warn",
    ref: "border-ref/40 bg-ref/10 text-ref",
    muted: "border-line bg-panel-2 text-muted",
    pos: "border-pos/40 bg-pos/10 text-pos",
    neg: "border-neg/40 bg-neg/10 text-neg",
    accent: "border-accent/40 bg-accent/10 text-accent",
  };
  return (
    <span title={title} className={`inline-flex items-center whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px] leading-none ${tones[tone]}`}>
      {children}
    </span>
  );
}

/** A probability with its uncertainty: "54.2% [39–69%]" when a posterior band exists. */
export function ProbCell({ p, band }: { p: number; band?: Band | null }) {
  return (
    <span className="num whitespace-nowrap">
      {pct(p)}
      {band ? (
        <span className="text-muted" title={`${Math.round(band.interval * 100)}% credible interval (PyMC)`}>
          {" "}[{(band.lo * 100).toFixed(0)}–{(band.hi * 100).toFixed(0)}%]
        </span>
      ) : null}
    </span>
  );
}

export function Card({ title, children, className = "" }: { title?: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-line bg-panel p-4 ${className}`}>
      {title && <h2 className="mb-3 text-xs font-medium uppercase tracking-wider text-muted">{title}</h2>}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, tone = "text" }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: "text" | "pos" | "neg" | "muted" }) {
  const tones = { text: "text-text", pos: "text-pos", neg: "text-neg", muted: "text-muted" };
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className={`num mt-1 text-2xl font-semibold ${tones[tone]}`}>{value}</div>
      {sub && <div className="num mt-0.5 text-xs text-muted">{sub}</div>}
    </div>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="py-10 text-center text-sm text-muted">{what}…</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  return (
    <div className="rounded border border-neg/40 bg-neg/10 p-3 text-sm text-neg">
      {error instanceof Error ? error.message : String(error)}
    </div>
  );
}
