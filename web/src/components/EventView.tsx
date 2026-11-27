"use client";

import { useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { book, kickoff, odds, pct, signedPct } from "@/lib/format";
import { BOOK_COLORS } from "./OddsChart";
import { Badge, Card, ErrorBox, Loading } from "./ui";

const OddsChart = dynamic(() => import("./OddsChart"), { ssr: false, loading: () => <Loading what="Loading chart" /> });

const SELECTIONS: Record<string, string[]> = { "1X2": ["home", "draw", "away"], OU: ["over", "under"] };

export function EventView() {
  const { id } = useParams<{ id: string }>();
  const eventId = Number(id);
  const [market, setMarket] = useState<"1X2" | "OU">("1X2");
  const [selection, setSelection] = useState("home");
  const ev = useQuery({ queryKey: ["event", eventId], queryFn: () => api.event(eventId) });
  const series = useQuery({ queryKey: ["odds", eventId, market], queryFn: () => api.odds(eventId, market) });

  if (ev.isLoading) return <Loading what="Loading match" />;
  if (ev.isError) return <ErrorBox error={ev.error} />;
  const e = ev.data!;
  const shown = (series.data ?? []).filter((s) => s.selection === selection);
  const anyEstimated = shown.some((s) => s.points.some((p) => p.ts_estimated));
  const line = market === "OU" ? " 2.5" : "";

  return (
    <div className="space-y-4">
      <div>
        <div className="text-xs text-muted">{e.competition} · {e.season} · {kickoff(e.starts_at)}</div>
        <h1 className="mt-1 text-xl font-semibold">
          {e.home} <span className="text-muted">v</span> {e.away}
          {e.home_goals !== null && <span className="num ml-3 text-muted">{e.home_goals}–{e.away_goals}</span>}
        </h1>
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <Card>
          <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
            {(["1X2", "OU"] as const).map((m) => (
              <button key={m} onClick={() => { setMarket(m); setSelection(SELECTIONS[m][0]); }}
                className={`rounded px-3 py-1 ${m === market ? "bg-panel-2" : "text-muted hover:text-text"}`}>
                {m === "OU" ? "O/U 2.5" : m}
              </button>
            ))}
            <span className="mx-2 h-4 w-px bg-line" />
            {SELECTIONS[market].map((s) => (
              <button key={s} onClick={() => setSelection(s)}
                className={`rounded px-3 py-1 capitalize ${s === selection ? "bg-panel-2" : "text-muted hover:text-text"}`}>
                {s}
              </button>
            ))}
          </div>
          {series.isLoading ? <Loading what="Loading prices" /> : shown.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted">No prices stored for this market.</div>
          ) : (
            <OddsChart series={shown} />
          )}
          <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-muted">
            {shown.map((s) => (
              <span key={s.bookmaker} className="flex items-center gap-1.5">
                <span className="inline-block h-0.5 w-5" style={{ background: BOOK_COLORS[s.bookmaker] ?? "#7d8b9a", height: s.bookmaker === "pinnacle" ? 3 : 1 }} />
                {book(s.bookmaker)}{s.bookmaker === "pinnacle" && " (reference)"}
              </span>
            ))}
            {anyEstimated && <Badge tone="warn" title="football-data.co.uk does not publish collection times: pre-match prices are placed at kickoff − 2h (an upper bound), closing prices at kickoff">dashed = estimated timestamp</Badge>}
          </div>
        </Card>

        <div className="space-y-4">
          <Card title="Model vs sharp close">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted"><tr><th className="text-left">Pick</th><th className="text-right">Model</th><th className="text-right">Close (fair)</th><th className="text-right">Diff</th></tr></thead>
              <tbody>
                {SELECTIONS[market].map((s) => {
                  const k = `${market}${line}:${s}`;
                  const m = e.model[k];
                  const c = e.close_fair[k];
                  return (
                    <tr key={s} className="border-t border-line">
                      <td className="py-1.5 capitalize">{s}</td>
                      <td className="num text-right">{pct(m)}</td>
                      <td className="num text-right text-ref">{pct(c)}</td>
                      <td className="num text-right text-muted">{m !== undefined && c !== undefined ? signedPct(m - c) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-muted">
              Model fitted {e.model_as_of ? new Date(e.model_as_of).toLocaleDateString("en-GB") : "—"}; close devigged from Pinnacle (Betfair fallback).
            </p>
          </Card>
          {e.rates && (
            <Card title="Expected goals (model)">
              <div className="num flex justify-between text-sm">
                <span>{e.home} {e.rates.lambda_home.toFixed(2)}</span>
                <span>{e.away} {e.rates.lambda_away.toFixed(2)}</span>
              </div>
              <div className="mt-1 text-xs text-muted">Dixon-Coles ρ = {e.rates.rho.toFixed(3)}</div>
            </Card>
          )}
          <Card title="Latest prices">
            <table className="w-full text-sm">
              <tbody>
                {shown.map((s) => {
                  const last = s.points[s.points.length - 1];
                  return (
                    <tr key={s.bookmaker} className="border-t border-line first:border-0">
                      <td className="py-1.5">{book(s.bookmaker)}</td>
                      <td className="num text-right">{odds(last?.price)}</td>
                      <td className="text-right text-xs text-muted">{last?.is_closing ? "close" : "pre"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
        </div>
      </div>
    </div>
  );
}
