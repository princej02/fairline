"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { useAccaSlip } from "@/components/AccaSlip";
import { Badge, Card, ErrorBox, Loading, Stat } from "@/components/ui";
import { api } from "@/lib/api";
import { book, evTone, odds, pct, selectionLabel, signedPct } from "@/lib/format";

export default function AccaPage() {
  const { legs, remove, clear } = useAccaSlip();
  const [builder, setBuilder] = useState<Record<number, string>>({});
  const builderPrices = Object.fromEntries(Object.entries(builder).filter(([, v]) => Number(v) > 1).map(([k, v]) => [Number(k), Number(v)]));
  const q = useQuery({
    queryKey: ["acca", legs, builderPrices],
    queryFn: () => api.acca(legs, builderPrices),
    enabled: legs.length > 0,
  });
  const d = q.data;

  if (legs.length === 0)
    return (
      <div className="space-y-2">
        <h1 className="text-xl font-semibold">Acca builder</h1>
        <p className="text-sm text-muted">Add legs with “+ Acca” on the <Link href="/" className="text-ref">value board</Link>.</p>
      </div>
    );

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold">Acca builder</h1>
          <p className="text-sm text-muted">Legs on the same match use the exact joint probability from the model&apos;s score matrix, never a product of marginals.</p>
        </div>
        <button onClick={clear} className="text-sm text-muted hover:text-neg">Clear slip</button>
      </div>

      <Card>
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted">
            <tr><th className="py-1.5">Leg</th><th>Book</th><th className="text-right">Odds</th><th className="text-right">Model p</th><th className="text-right">Leg EV</th><th /></tr>
          </thead>
          <tbody>
            {legs.map((l, i) => {
              const v = d?.legs[i];
              return (
                <tr key={i} className="border-t border-line">
                  <td className="py-2">{l.label} · <span className="text-muted">{selectionLabel(l.market, l.selection, l.line)}</span></td>
                  <td>{book(l.bookmaker)}</td>
                  <td className="num text-right">{odds(l.price)}</td>
                  <td className="num text-right">{v ? pct(v.p_model) : "—"}</td>
                  <td className={`num text-right ${evTone(v?.ev)}`}>{v ? signedPct(v.ev) : "—"}{v?.drags && <div><Badge tone="neg">drags ticket</Badge></div>}</td>
                  <td className="text-right"><button onClick={() => remove(i)} className="text-xs text-muted hover:text-neg">remove</button></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      {q.isLoading ? <Loading what="Pricing acca" /> : q.isError ? <ErrorBox error={q.error} /> : d && (
        <>
          {d.blocked.map((b) => <div key={b} className="rounded border border-neg/40 bg-neg/10 p-2 text-sm text-neg">{b}</div>)}
          {d.warnings.map((w) => <div key={w} className="rounded border border-warn/40 bg-warn/10 p-2 text-sm text-warn">{w}</div>)}
          {Object.keys(d.same_match_fair_odds).length > 0 && (
            <Card title="Same-match groups: enter the bet-builder price">
              {Object.entries(d.same_match_fair_odds).map(([ev, fair]) => (
                <label key={ev} className="flex items-center gap-3 text-sm">
                  <span>{legs.find((l) => l.event_id === Number(ev))?.label}</span>
                  <span className="num text-muted">fair odds {fair.toFixed(2)}</span>
                  <input aria-label="Bet-builder price" placeholder="builder price" value={builder[Number(ev)] ?? ""} onChange={(e) => setBuilder((b) => ({ ...b, [Number(ev)]: e.target.value }))} className="num w-28 rounded border border-line bg-panel-2 px-2 py-1" />
                </label>
              ))}
            </Card>
          )}
          <div className="grid gap-4 md:grid-cols-4">
            <Card><Stat label="Joint probability (exact)" value={pct(d.p_joint, 2)} sub={`naive product ${pct(d.p_naive, 2)}`} /></Card>
            <Card><Stat label="Combined price" value={odds(d.combined_price)} sub={d.p_joint ? `fair ${odds(1 / d.p_joint)}` : undefined} /></Card>
            <Card><Stat label="Ticket EV (model)" value={signedPct(d.ev)} tone={d.ev === null ? "muted" : d.ev >= 0 ? "pos" : "neg"} sub={`suggested stake ${pct(d.suggested_stake_pct, 2)}`} /></Card>
            <Card><Stat label="Compounded margin" value={pct(d.compounded_margin)} sub="bookmaker overround across legs" tone="neg" /></Card>
          </div>
          <p className="text-xs text-muted">Ticket EV uses model probabilities, which did not beat the sharp market in backtests. Treat it as the model&apos;s opinion, not an edge.</p>
        </>
      )}
    </div>
  );
}
