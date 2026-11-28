"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { api, type BettingRow } from "@/lib/api";
import { pct, signedPct } from "@/lib/format";
import { CalibrationPlot } from "./CalibrationPlot";
import { Badge, Card, ErrorBox, Loading } from "./ui";

const LABELS: Record<string, string> = {
  model: "Raw model",
  model_beats_ref: "Model, must beat Pinnacle",
  model_no_disagree: "Model, no disagreements",
  reference: "Pinnacle reference only",
  blend: "Model/market blend",
};

const parseCI = (s: string) => s.replace(/[[\]+\s]/g, "").split(",").map(Number) as [number, number];

/** Horizontal interval plot: estimate dot, 95% CI bar, zero line. */
function IntervalBar({ est, ci, domain }: { est: number; ci: [number, number]; domain: [number, number] }) {
  const [a, b] = domain;
  const x = (v: number) => `${((Math.min(Math.max(v, a), b) - a) / (b - a)) * 100}%`;
  const tone = ci[0] > 0 ? "bg-pos" : ci[1] < 0 ? "bg-neg" : "bg-muted";
  return (
    <div className="relative h-4 w-full">
      <div className="absolute inset-y-0 w-px bg-line" style={{ left: x(0) }} />
      <div className={`absolute top-1/2 h-1 -translate-y-1/2 rounded ${tone} opacity-60`} style={{ left: x(ci[0]), width: `calc(${x(ci[1])} - ${x(ci[0])})` }} />
      <div className={`absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ${tone}`} style={{ left: x(est) }} />
    </div>
  );
}

export function BacktestView() {
  const { id } = useParams<{ id: string }>();
  const q = useQuery({ queryKey: ["backtest", Number(id)], queryFn: () => api.backtest(Number(id)) });
  if (q.isLoading) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const { config, metrics } = q.data!;
  const rows = metrics.betting.filter((r) => r.bets > 0);
  const clvDomain: [number, number] = [-0.07, 0.05];
  const roiDomain: [number, number] = [-0.2, 0.15];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Backtest #{q.data!.id}</h1>
        <p className="text-sm text-muted">
          {config.pools.join(", ")} · {config.first} to {config.last} · decided at kickoff − {config.offset_hours}h · 95% intervals from a matchday cluster bootstrap
        </p>
      </div>

      <Card title="Closing line value: the headline metric">
        <p className="mb-3 text-xs text-muted">
          CLV = price taken × devigged closing probability − 1. It measures edge with far less noise than ROI. Every ROI
          interval below still spans zero.
        </p>
        <div className="space-y-2">
          {rows.map((r) => {
            const ci = parseCI(r.clv_ci);
            return (
              <div key={r.strategy} className="grid grid-cols-[220px_90px_1fr_150px] items-center gap-3 text-sm">
                <span>{LABELS[r.strategy] ?? r.strategy}</span>
                <span className={`num text-right text-lg font-semibold ${ci[0] > 0 ? "text-pos" : ci[1] < 0 ? "text-neg" : "text-text"}`}>{signedPct(r.clv)}</span>
                <IntervalBar est={r.clv} ci={ci} domain={clvDomain} />
                <span className="num text-xs text-muted">[{signedPct(ci[0])}, {signedPct(ci[1])}] · {pct(r.beat_close, 0)} beat close</span>
              </div>
            );
          })}
        </div>
      </Card>

      <Card title="Return on stake (flat)">
        <div className="space-y-2">
          {rows.map((r) => {
            const ci = parseCI(r.flat_roi_ci);
            return (
              <div key={r.strategy} className="grid grid-cols-[220px_90px_1fr_150px] items-center gap-3 text-sm">
                <span className="text-muted">{LABELS[r.strategy] ?? r.strategy}</span>
                <span className="num text-right">{signedPct(r.flat_roi)}</span>
                <IntervalBar est={r.flat_roi} ci={ci} domain={roiDomain} />
                <span className="num text-xs text-muted">[{signedPct(ci[0])}, {signedPct(ci[1])}]</span>
              </div>
            );
          })}
        </div>
      </Card>

      <Card title="Strategies">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                {["Strategy", "Bets", "Hit", "Avg odds", "Claimed EV", "Kelly ROI", "Bankroll", "Max DD", "Slate cap binds"].map((h) => (
                  <th key={h} className="px-2 py-1.5">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r: BettingRow) => (
                <tr key={r.strategy} className="border-t border-line">
                  <td className="px-2 py-1.5">{LABELS[r.strategy] ?? r.strategy}</td>
                  <td className="num px-2">{r.bets.toLocaleString()}</td>
                  <td className="num px-2">{pct(r.hit)}</td>
                  <td className="num px-2">{r.avg_odds.toFixed(2)}</td>
                  <td className="num px-2 text-muted">{signedPct(r.mean_ev)}</td>
                  <td className="num px-2">{signedPct(r.kelly_roi)}</td>
                  <td className={`num px-2 ${r.bankroll_x < 1 ? "text-neg" : "text-pos"}`}>{r.bankroll_x.toFixed(2)}×</td>
                  <td className="num px-2">{pct(r.max_dd, 0)}</td>
                  <td className="num px-2">{pct(r.slate_cap_rate, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-muted">“Claimed EV” is what each strategy believed when it bet. Compare it with CLV above.</p>
      </Card>

      <Card title="Model calibration">
        {metrics.calibration ? (
          <div className="grid gap-6 md:grid-cols-2">
            <CalibrationPlot title="1X2" bins={metrics.calibration["1X2"] ?? []} />
            <CalibrationPlot title="Over/Under 2.5" bins={metrics.calibration["OU"] ?? []} />
          </div>
        ) : (
          <p className="text-sm text-muted">This run predates stored calibration. Re-run with <code>--persist</code>.</p>
        )}
        <p className="mt-2 text-xs text-muted">
          <Badge tone="warn">amber</Badge> = the 95% Wilson interval misses the diagonal. O/U is overconfident at both tails.
        </p>
      </Card>

      <Card title="Probability accuracy vs the sharp close">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted">
            <tr><th className="px-2 py-1.5">Market</th><th className="px-2">Source</th><th className="px-2">Matches</th><th className="px-2">Log loss</th><th className="px-2">Brier</th><th className="px-2">Minus close [95% CI]</th></tr>
          </thead>
          <tbody>
            {metrics.probability.map((p) => (
              <tr key={p.market + p.source} className="border-t border-line">
                <td className="px-2 py-1.5">{p.market}</td>
                <td className="px-2">{p.source === "ref" ? "Pinnacle at decision" : p.source === "close" ? "Pinnacle close" : p.source}</td>
                <td className="num px-2">{p.matches.toLocaleString()}</td>
                <td className="num px-2">{p.log_loss.toFixed(4)}</td>
                <td className="num px-2">{p.brier.toFixed(4)}</td>
                <td className="num px-2 text-muted">{p.minus_close ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
