"use client";

// Browser-only (canvas). Always load through next/dynamic with { ssr: false }.
import { ColorType, createChart, LineSeries, LineStyle, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { OddsSeries } from "@/lib/api";

const BOOK_COLORS: Record<string, string> = {
  pinnacle: "#60a5fa",
  betfair_ex_uk: "#a78bfa",
  bet365: "#34d399",
  williamhill: "#fbbf24",
  skybet: "#f472b6",
  paddypower: "#22d3ee",
};

/** Pinnacle is the bold reference line; bettable books are thinner. Points with an estimated
 *  timestamp are drawn dashed, so historical football-data.co.uk prices are never mistaken
 *  for exactly timed ticks. */
export default function OddsChart({ series, height = 360 }: { series: OddsSeries[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      height,
      layout: { background: { type: ColorType.Solid, color: "#111821" }, textColor: "#7d8b9a", attributionLogo: false },
      grid: { vertLines: { color: "#1b2531" }, horzLines: { color: "#1b2531" } },
      rightPriceScale: { borderColor: "#233040" },
      timeScale: { borderColor: "#233040", timeVisible: true, secondsVisible: false },
      autoSize: true,
    });
    for (const s of series) {
      const isPin = s.bookmaker === "pinnacle";
      const color = BOOK_COLORS[s.bookmaker] ?? "#7d8b9a";
      const byTime = new Map<number, { time: UTCTimestamp; value: number }>();
      for (const p of s.points) {
        const t = Math.floor(new Date(p.ts).getTime() / 1000) as UTCTimestamp;
        byTime.set(t, { time: t, value: p.price });
      }
      const data = [...byTime.values()].sort((a, b) => a.time - b.time);
      const estimated = s.points.some((p) => p.ts_estimated);
      const line = chart.addSeries(LineSeries, {
        color,
        lineWidth: isPin ? 3 : 1,
        lineStyle: estimated ? LineStyle.Dashed : LineStyle.Solid,
        pointMarkersVisible: true,
        pointMarkersRadius: isPin ? 4 : 2,
        title: s.bookmaker,
        priceLineVisible: false,
        lastValueVisible: isPin,
      });
      line.setData(data);
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [series, height]);

  return <div ref={ref} style={{ height }} className="w-full" />;
}

export { BOOK_COLORS };
