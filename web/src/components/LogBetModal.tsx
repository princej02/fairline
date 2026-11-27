"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Bet, type BetIn } from "@/lib/api";
import { book, odds, pct, selectionLabel, signedPct } from "@/lib/format";
import { Badge } from "./ui";

export interface LogBetDraft {
  event_id: number;
  label: string;
  market: "1X2" | "OU";
  line: number | null;
  selection: string;
  bookmaker: string;
  price: number;
  stake: number;
  placed_at: string; // ISO
  p_model: number | null;
  strategy: string | null;
  suggested_stake_pct: number | null;
}

const toLocalInput = (iso: string) => {
  const d = new Date(iso);
  const off = d.getTimezoneOffset() * 60_000;
  return new Date(d.getTime() - off).toISOString().slice(0, 16);
};

export function LogBetModal({ draft, books, currency, onClose }: { draft: LogBetDraft; books: string[]; currency: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [bookmaker, setBookmaker] = useState(draft.bookmaker);
  const [price, setPrice] = useState(String(draft.price));
  const [stake, setStake] = useState(String(draft.stake));
  const [placedAt, setPlacedAt] = useState(toLocalInput(draft.placed_at));
  const [notes, setNotes] = useState("");
  const [saved, setSaved] = useState<Bet | null>(null);

  const m = useMutation({
    mutationFn: (body: BetIn) => api.createBet(body),
    onSuccess: (bet) => {
      setSaved(bet);
      qc.invalidateQueries({ queryKey: ["bets"] });
      qc.invalidateQueries({ queryKey: ["tracker-summary"] });
    },
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    m.mutate({
      legs: [{ event_id: draft.event_id, market: draft.market, selection: draft.selection, line: draft.line, price: Number(price), p_model: draft.p_model }],
      bookmaker,
      stake: Number(stake),
      placed_at: new Date(placedAt).toISOString(),
      strategy: draft.strategy,
      source: "board",
      suggested_stake_pct: draft.suggested_stake_pct,
      notes: notes || null,
    });
  };

  const leg = saved?.legs[0];
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-lg border border-line bg-panel p-5" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Log bet">
        <h2 className="text-base font-semibold">Log a bet you placed</h2>
        <p className="mt-1 text-sm text-muted">
          {draft.label} · {selectionLabel(draft.market, draft.selection, draft.line)}
        </p>
        {saved && leg ? (
          <div className="mt-4 space-y-2 text-sm">
            <div className="text-pos">Logged as bet #{saved.id} ({saved.status}).</div>
            <div>
              vs {book(leg.ref_book)} at placement:{" "}
              <span className={`num ${leg.execution_edge !== null && leg.execution_edge < 0 ? "text-neg" : "text-pos"}`}>
                {signedPct(leg.execution_edge)}
              </span>{" "}
              <span className="text-muted">(fair p {pct(leg.ref_fair_prob)})</span>
            </div>
            {leg.execution_edge !== null && leg.execution_edge < 0 && (
              <Badge tone="warn">Price was already below the sharp line when placed</Badge>
            )}
            <div>
              CLV vs devigged close: <span className="num">{leg.clv === null ? "pending (filled after the close)" : signedPct(leg.clv)}</span>
            </div>
            <button onClick={onClose} className="mt-3 rounded bg-panel-2 px-3 py-1.5 text-sm">Close</button>
          </div>
        ) : (
          <form onSubmit={submit} className="mt-4 grid grid-cols-2 gap-3 text-sm">
            <label className="col-span-2 flex flex-col gap-1">
              <span className="text-muted">Bookmaker</span>
              <select value={bookmaker} onChange={(e) => setBookmaker(e.target.value)} className="rounded border border-line bg-panel-2 px-2 py-1.5">
                {books.map((b) => <option key={b} value={b}>{book(b)}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">Odds taken</span>
              <input value={price} onChange={(e) => setPrice(e.target.value)} inputMode="decimal" className="num rounded border border-line bg-panel-2 px-2 py-1.5" />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted">Stake ({currency})</span>
              <input value={stake} onChange={(e) => setStake(e.target.value)} inputMode="decimal" className="num rounded border border-line bg-panel-2 px-2 py-1.5" />
            </label>
            <label className="col-span-2 flex flex-col gap-1">
              <span className="text-muted">Placed at (your local time)</span>
              <input type="datetime-local" value={placedAt} onChange={(e) => setPlacedAt(e.target.value)} className="num rounded border border-line bg-panel-2 px-2 py-1.5" />
              <span className="text-xs text-muted">Pre-filled with the board&apos;s decision time. Used to look up the sharp price you were up against.</span>
            </label>
            <label className="col-span-2 flex flex-col gap-1">
              <span className="text-muted">Notes</span>
              <input value={notes} onChange={(e) => setNotes(e.target.value)} className="rounded border border-line bg-panel-2 px-2 py-1.5" />
            </label>
            <div className="col-span-2 text-xs text-muted">
              Suggested: <span className="num">{odds(draft.price)}</span> at {book(draft.bookmaker)}, stake{" "}
              <span className="num">{draft.stake}</span> ({pct(draft.suggested_stake_pct, 2)} of bankroll, after caps)
            </div>
            {m.isError && <div className="col-span-2 text-neg">{(m.error as Error).message}</div>}
            <div className="col-span-2 mt-1 flex justify-end gap-2">
              <button type="button" onClick={onClose} className="rounded px-3 py-1.5 text-muted hover:text-text">Cancel</button>
              <button type="submit" disabled={m.isPending} className="rounded bg-ref px-3 py-1.5 font-medium text-bg disabled:opacity-50">
                {m.isPending ? "Logging…" : "Log bet"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
