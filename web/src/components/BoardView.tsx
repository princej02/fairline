"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { api, type BoardRow, type StrategyName } from "@/lib/api";
import { book, evTone, kickoff, odds, pct, selectionLabel, signedPct } from "@/lib/format";
import { useAccaSlip } from "./AccaSlip";
import { LogBetModal, type LogBetDraft } from "./LogBetModal";
import { Badge, ErrorBox, Loading, ProbCell } from "./ui";

export function BoardView() {
  const params = useSearchParams();
  const router = useRouter();
  const path = usePathname();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const pool = params.get("pool") ?? "ENG";
  const strategy = (params.get("strategy") ?? settings.data?.default_strategy ?? "model_beats_ref") as StrategyName;
  const bands = params.get("bands") === "1";
  const days = useQuery({ queryKey: ["matchdays", pool], queryFn: () => api.matchdays(pool) });
  const date = params.get("date") ?? days.data?.find((d) => d.matches >= 5)?.date ?? days.data?.[0]?.date;
  const board = useQuery({
    queryKey: ["board", pool, date, strategy, bands],
    queryFn: () => api.board(pool, date!, strategy, bands),
    enabled: Boolean(date),
  });
  const [draft, setDraft] = useState<LogBetDraft | null>(null);
  const slip = useAccaSlip();

  const set = (changes: Record<string, string | null>) => {
    const p = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(changes)) {
      if (v === null) p.delete(k);
      else p.set(k, v);
    }
    router.replace(`${path}?${p.toString()}`);
  };

  const strat = settings.data?.strategies.find((s) => s.name === strategy);
  const bankroll = settings.data?.bankroll ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-4">
        <div>
          <h1 className="text-xl font-semibold">Value board</h1>
          <p className="text-sm text-muted">
            Priced 2h before each kickoff. Model EV and Pinnacle EV side by side. Only the sharp
            reference beat the close in the P1.4 backtest.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-3 text-sm">
          <select aria-label="Pool" value={pool} onChange={(e) => set({ pool: e.target.value, date: null })} className="rounded border border-line bg-panel px-2 py-1.5">
            {Object.keys(settings.data?.pools ?? { ENG: [] }).map((p) => <option key={p}>{p}</option>)}
          </select>
          <select aria-label="Matchday" value={date ?? ""} onChange={(e) => set({ date: e.target.value })} className="num rounded border border-line bg-panel px-2 py-1.5">
            {days.data?.map((d) => <option key={d.date} value={d.date}>{d.date} ({d.matches})</option>)}
          </select>
          <label className="flex items-center gap-1.5 text-muted" title="Run PyMC for this slate (about 5-10 s) to show credible intervals">
            <input type="checkbox" checked={bands} onChange={(e) => set({ bands: e.target.checked ? "1" : null })} />
            Uncertainty bands
          </label>
        </div>
      </div>

      <div className="flex flex-wrap gap-1 rounded-lg border border-line bg-panel p-1 text-sm" role="tablist" aria-label="Strategy">
        {settings.data?.strategies.map((s) => (
          <button
            key={s.name}
            role="tab"
            aria-selected={s.name === strategy}
            onClick={() => set({ strategy: s.name })}
            title={s.description}
            className={`rounded px-3 py-1.5 ${s.name === strategy ? "bg-panel-2 text-text" : "text-muted hover:text-text"}`}
          >
            {s.label}
            {s.name === "model" && <span className="ml-1.5 text-xs text-neg">(lost in backtest)</span>}
          </button>
        ))}
      </div>
      {strat && <p className="text-xs text-muted">{strat.description}</p>}

      {board.isLoading || days.isLoading ? (
        <Loading what={bands ? "Sampling PyMC bands" : "Pricing the slate"} />
      ) : board.isError ? (
        <ErrorBox error={board.error} />
      ) : board.data ? (
        <>
          {board.data.notes.map((n) => <div key={n} className="text-xs text-warn">{n}</div>)}
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead className="bg-panel text-left text-xs text-muted">
                <tr>
                  <th className="px-3 py-2">Match</th>
                  <th className="px-3 py-2">Pick</th>
                  <th className="px-3 py-2">Book</th>
                  <th className="px-3 py-2 text-right">Odds</th>
                  <th className="px-3 py-2 text-right">Model p</th>
                  <th className="px-3 py-2 text-right">Pinnacle p</th>
                  <th className="px-3 py-2 text-right">Model EV</th>
                  <th className="px-3 py-2 text-right">Pinnacle EV</th>
                  <th className="px-3 py-2">Signals</th>
                  <th className="px-3 py-2 text-right">Stake</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {board.data.rows.length === 0 && (
                  <tr><td colSpan={11} className="px-3 py-8 text-center text-muted">No picks pass this strategy&apos;s filters on this day.</td></tr>
                )}
                {board.data.rows.map((r) => (
                  <Row
                    key={`${r.event_id}-${r.market}-${r.selection}`}
                    r={r}
                    currency={settings.data?.currency ?? "GBP"}
                    inSlip={slip.has(leg(r))}
                    onAcca={() => slip.add(leg(r))}
                    onLog={() =>
                      setDraft({
                        event_id: r.event_id, label: r.label, market: r.market as "1X2" | "OU", line: r.line,
                        selection: r.selection, bookmaker: r.bookmaker, price: r.price,
                        stake: r.stake_amount || Math.round(bankroll * 0.005 * 100) / 100,
                        placed_at: r.decision_ts, p_model: r.p_model, strategy, suggested_stake_pct: r.stake_pct,
                      })
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-muted">
            {board.data.evaluated_prices} prices evaluated. Stakes are {settings.data?.kelly_fraction}× Kelly, capped at{" "}
            {pct(settings.data?.max_stake_pct)} per bet, {pct(settings.data?.max_match_exposure_pct)} per match and{" "}
            {pct(settings.data?.max_slate_exposure_pct)} per slate, of a {bankroll} {settings.data?.currency} bankroll.
          </p>
        </>
      ) : null}
      {draft && settings.data && (
        <LogBetModal draft={draft} books={settings.data.bettable_books} currency={settings.data.currency} onClose={() => setDraft(null)} />
      )}
    </div>
  );
}

const leg = (r: BoardRow) => ({
  event_id: r.event_id, market: r.market as "1X2" | "OU", selection: r.selection, price: r.price, line: r.line,
  bookmaker: r.bookmaker, label: r.label,
});

function Row({ r, currency, onLog, onAcca, inSlip }: { r: BoardRow; currency: string; onLog: () => void; onAcca: () => void; inSlip: boolean }) {
  const disagree = r.flags.includes("model_disagrees_with_market");
  const eff = [r.home_effective_matches, r.away_effective_matches].filter((x): x is number => x !== null);
  const minEff = eff.length ? Math.min(...eff) : null;
  return (
    <tr className={`border-t border-line ${disagree ? "bg-warn/5" : ""}`}>
      <td className="px-3 py-2">
        <Link href={`/events/${r.event_id}`} className="hover:text-ref">{r.label}</Link>
        <div className="text-xs text-muted">{r.competition} · {kickoff(r.starts_at)}</div>
      </td>
      <td className="px-3 py-2">{selectionLabel(r.market, r.selection, r.line)}</td>
      <td className="px-3 py-2">{book(r.bookmaker)}</td>
      <td className="num px-3 py-2 text-right">
        {odds(r.price)}
        {r.effective_price !== r.price && <div className="text-xs text-muted" title="after exchange commission">{odds(r.effective_price)} net</div>}
      </td>
      <td className="px-3 py-2 text-right"><ProbCell p={r.p_model} band={r.band} /></td>
      <td className="px-3 py-2 text-right">
        <span className="num text-ref">{pct(r.p_ref)}</span>
        {r.ref_book && r.ref_book !== "pinnacle" && <div className="text-xs text-muted">{book(r.ref_book)}</div>}
      </td>
      <td className={`num px-3 py-2 text-right ${evTone(r.ev_model)}`}>{signedPct(r.ev_model)}</td>
      <td className={`num px-3 py-2 text-right font-medium ${evTone(r.ev_vs_ref)}`}>{signedPct(r.ev_vs_ref)}</td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-1">
          {disagree && <Badge tone="warn" title="Model and devigged Pinnacle disagree by more than the warning threshold. In P1.4 these picks lost 19% flat.">Model ≠ Market</Badge>}
          {minEff !== null && (
            <Badge tone={minEff < 15 ? "warn" : "muted"} title="Effective matches behind the weaker side's rating (time-decay weighted)">
              n≈{minEff.toFixed(0)}
            </Badge>
          )}
          {r.cold_start && <Badge tone="warn" title="A team is rated from the cold-start prior">cold start</Badge>}
          {r.flags.includes("edge_not_robust") && <Badge tone="neg" title="EV turns negative at the conservative posterior quantile">not robust</Badge>}
          {r.capped_by.length > 0 && <Badge title={`Stake scaled by: ${r.capped_by.join(", ")} cap`}>capped</Badge>}
        </div>
      </td>
      <td className="num px-3 py-2 text-right">
        {r.stake_amount.toFixed(2)}
        <div className="text-xs text-muted">{pct(r.stake_pct, 2)} {currency === "GBP" ? "" : currency}</div>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        <button onClick={onAcca} disabled={inSlip} className="mr-1 rounded border border-line px-2 py-1 text-xs text-muted hover:text-text disabled:opacity-40">
          {inSlip ? "In acca" : "+ Acca"}
        </button>
        <button onClick={onLog} className="rounded bg-ref/15 px-2 py-1 text-xs font-medium text-ref hover:bg-ref/25">Log bet</button>
      </td>
    </tr>
  );
}
