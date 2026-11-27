"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Badge, Card, ErrorBox, Loading, Stat } from "@/components/ui";
import { api, type Bet, type CI } from "@/lib/api";
import { book, money, odds, pct, selectionLabel, signedPct } from "@/lib/format";

function ciLine(c: CI | null) {
  if (!c) return "no data yet";
  return `95% [${signedPct(c.lo)}, ${signedPct(c.hi)}] · n=${c.n} · ${pct(c.positive_share, 0)} positive`;
}
const ciTone = (c: CI | null) => (!c ? "muted" : c.lo > 0 ? "pos" : c.hi < 0 ? "neg" : "text");

export default function TrackerPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const summary = useQuery({ queryKey: ["tracker-summary"], queryFn: api.trackerSummary });
  const bets = useQuery({ queryKey: ["bets"], queryFn: api.bets });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["bets"] });
    qc.invalidateQueries({ queryKey: ["tracker-summary"] });
  };
  const refresh = useMutation({ mutationFn: api.refreshBets, onSuccess: invalidate });
  const voidBet = useMutation({ mutationFn: api.voidBet, onSuccess: invalidate });
  const cur = settings.data?.currency ?? "GBP";
  const s = summary.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="max-w-3xl">
          <h1 className="text-xl font-semibold">Tracker</h1>
          <p className="text-sm text-muted">
            Does the backtest&apos;s +2.3% soft-book CLV survive live execution? Each bet is checked against the sharp price at
            placement (was the edge real, or a stale line?) and against the devigged close (true CLV).
          </p>
        </div>
        <button onClick={() => refresh.mutate()} disabled={refresh.isPending} className="rounded border border-line px-3 py-1.5 text-sm hover:bg-panel">
          {refresh.isPending ? "Refreshing…" : "Refresh results & CLV"}
        </button>
      </div>
      {refresh.data && <div className="text-xs text-muted">Filled {refresh.data.clv_filled} closing prices, settled {refresh.data.settled} bets.</div>}

      {summary.isError ? <ErrorBox error={summary.error} /> : (
        <div className="grid gap-4 md:grid-cols-4">
          <Card className="md:col-span-2">
            <Stat label="CLV vs devigged close (the test)" value={s?.clv ? signedPct(s.clv.mean, 2) : "—"} sub={ciLine(s?.clv ?? null)} tone={ciTone(s?.clv ?? null) as "pos" | "neg" | "text" | "muted"} />
          </Card>
          <Card>
            <Stat label="Execution edge vs sharp at placement" value={s?.execution_edge ? signedPct(s.execution_edge.mean, 2) : "—"} sub={ciLine(s?.execution_edge ?? null)} tone={ciTone(s?.execution_edge ?? null) as "pos" | "neg" | "text" | "muted"} />
          </Card>
          <Card>
            <Stat label="P&L (settled)" value={money(s?.pnl ?? 0, cur)} sub={`ROI ${signedPct(s?.roi)} on ${money(s?.staked ?? 0, cur)} · ${s?.settled ?? 0} settled, ${s?.open ?? 0} open`} tone={(s?.pnl ?? 0) >= 0 ? "pos" : "neg"} />
          </Card>
        </div>
      )}
      <p className="text-xs text-muted">
        ROI needs hundreds of bets to mean anything at these odds; CLV converges far faster. Judge the strategy on CLV first.
      </p>

      {s && Object.keys(s.clv_by_book).length > 0 && (
        <Card title="CLV by bookmaker">
          <div className="flex flex-wrap gap-6 text-sm">
            {Object.entries(s.clv_by_book).map(([b, v]) => (
              <div key={b}><span className="text-muted">{book(b)}</span> <span className="num">{signedPct(v.mean)}</span> <span className="text-xs text-muted">n={v.n}</span></div>
            ))}
          </div>
        </Card>
      )}

      <Card title="Bets">
        {bets.isLoading ? <Loading /> : bets.isError ? <ErrorBox error={bets.error} /> : bets.data!.length === 0 ? (
          <p className="text-sm text-muted">No bets yet. Use <Link href="/" className="text-ref">Log bet</Link> on the value board after placing one.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted">
                <tr>
                  {["Placed", "Bet", "Book", "Odds", "Stake", "Sharp p at placement", "Execution edge", "Close p", "CLV", "Result", ""].map((h) => (
                    <th key={h} className="px-2 py-1.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>{bets.data!.map((b) => <BetRow key={b.id} b={b} cur={cur} onVoid={() => voidBet.mutate(b.id)} />)}</tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

/** Whole-bet numbers. For an acca, multiply the legs exactly as /api/tracker/summary does:
 * p = Π p_leg, edge = Π effective_price × p − 1. Null unless every leg has the price. */
function combined(b: Bet) {
  const prod = (xs: (number | null)[]) => (xs.some((x) => x === null) ? null : xs.reduce<number>((a, x) => a * (x as number), 1));
  const price = prod(b.legs.map((l) => l.effective_price ?? l.price));
  const ref = prod(b.legs.map((l) => l.ref_fair_prob));
  const close = prod(b.legs.map((l) => l.close_fair_prob));
  const refBooks = [...new Set(b.legs.map((l) => l.ref_book).filter(Boolean))] as string[];
  const derivedFrom = b.legs.length === 1 ? b.legs[0].ref_source_line : null;
  const clvNote = b.legs.map((l) => l.clv_note).find(Boolean) ?? null;
  return {
    ref,
    close,
    refBook: refBooks.length === 1 ? refBooks[0] : null,
    edge: price !== null && ref !== null ? price * ref - 1 : null,
    clv: price !== null && close !== null ? price * close - 1 : null,
    derivedFrom,
    clvNote,
  };
}

const signTone = (v: number | null) => (v === null ? "text-muted" : v < 0 ? "text-neg" : "text-pos");

function BetRow({ b, cur, onVoid }: { b: Bet; cur: string; onVoid: () => void }) {
  const leg = b.legs[0];
  const multi = b.legs.length > 1;
  const c = combined(b);
  const tone = b.status === "won" ? "pos" : b.status === "lost" ? "neg" : "muted";
  const meta = [b.strategy, b.source === "manual" ? "manual" : null].filter(Boolean).join(" · ");
  return (
    <tr className="border-t border-line align-top">
      <td className="num whitespace-nowrap px-2 py-2 text-xs text-muted">{new Date(b.placed_at).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" })}</td>
      <td className="min-w-56 px-2 py-2">
        {b.legs.map((l) => (
          <div key={l.leg_no}>
            <Link href={`/events/${l.event_id}`} className="hover:text-ref">{l.label}</Link>{" "}
            <span className="text-muted">· {selectionLabel(l.market, l.selection, l.line)}</span>
          </div>
        ))}
        {multi && <Badge tone="accent">{b.legs.length}-leg acca</Badge>}
        {meta && <div className="text-xs text-muted">{meta}</div>}
        {b.notes && <div className="max-w-72 truncate text-xs text-muted" title={b.notes}>{b.notes}</div>}
      </td>
      <td className="whitespace-nowrap px-2 py-2">{book(b.bookmaker)}</td>
      <td className="num px-2 py-2">{odds(b.price)}{!multi && leg.effective_price && leg.effective_price !== leg.price && <div className="text-xs text-muted">{odds(leg.effective_price)} net</div>}</td>
      <td className="num px-2 py-2">{money(b.stake, cur)}</td>
      <td className="num px-2 py-2 text-ref">
        {c.ref === null ? (
          <span className="text-xs text-muted" title="No sharp price was recorded before this bet was placed (no lookahead), so execution edge can't be measured.">
            none before placement
          </span>
        ) : (
          <>
            {pct(c.ref)}
            <div className="text-xs text-muted">{book(c.refBook)}</div>
            {c.derivedFrom !== null && <div className="text-xs text-warn">derived from {c.derivedFrom}</div>}
          </>
        )}
      </td>
      <td className={`num px-2 py-2 ${signTone(c.edge)}`}>
        {c.edge === null ? "—" : signedPct(c.edge)}
        {c.edge !== null && c.edge < 0 && <div><Badge tone="warn">stale / no edge</Badge></div>}
      </td>
      <td className="num px-2 py-2 text-ref">{c.close === null ? <span className="text-muted">—</span> : pct(c.close)}</td>
      <td className={`num px-2 py-2 font-medium ${signTone(c.clv)}`}>{c.clv !== null ? signedPct(c.clv) : c.clvNote ? <span className="text-xs text-muted" title={c.clvNote}>n/a: no like-for-like close</span> : <span className="text-muted">pending</span>}</td>
      <td className="px-2 py-2"><Badge tone={tone}>{b.status}</Badge>{b.pnl !== null && <div className="num text-xs text-muted">{money(b.pnl, cur)}</div>}</td>
      <td className="px-2 py-2 text-right">{b.status === "open" && <button onClick={onVoid} className="text-xs text-muted hover:text-neg">void</button>}</td>
    </tr>
  );
}
