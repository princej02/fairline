import type { CalibrationBin } from "@/lib/api";

/** Reliability diagram: predicted (x) vs realised (y) with 95% Wilson bars. Points off the
 *  diagonal with bars that miss it are real miscalibration (drawn in the warning colour). */
export function CalibrationPlot({ bins, title, minN = 30 }: { bins: CalibrationBin[]; title: string; minN?: number }) {
  const W = 320, H = 320, P = 36;
  const x = (v: number) => P + v * (W - 2 * P);
  const y = (v: number) => H - P - v * (H - 2 * P);
  const shown = bins.filter((b) => b.n >= minN);
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  return (
    <figure>
      <figcaption className="mb-2 text-sm font-medium">{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-[420px]" role="img" aria-label={`${title} calibration`}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={y(0)} y2={y(1)} stroke="#1b2531" />
            <line x1={x(0)} x2={x(1)} y1={y(t)} y2={y(t)} stroke="#1b2531" />
            <text x={x(t)} y={H - P + 16} fill="#7d8b9a" fontSize="10" textAnchor="middle">{t}</text>
            <text x={P - 8} y={y(t) + 3} fill="#7d8b9a" fontSize="10" textAnchor="end">{t}</text>
          </g>
        ))}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} stroke="#7d8b9a" strokeDasharray="4 4" />
        {shown.map((b) => {
          const off = b.mean_p < b.ci_lo || b.mean_p > b.ci_hi;
          const c = off ? "#fbbf24" : "#60a5fa";
          return (
            <g key={b.lo}>
              <line x1={x(b.mean_p)} x2={x(b.mean_p)} y1={y(b.ci_lo)} y2={y(b.ci_hi)} stroke={c} strokeWidth="2" />
              <circle cx={x(b.mean_p)} cy={y(b.observed)} r={Math.min(7, 2.5 + Math.sqrt(b.n) / 12)} fill={c}>
                <title>{`predicted ${(b.mean_p * 100).toFixed(1)}% → realised ${(b.observed * 100).toFixed(1)}% (n=${b.n}, 95% [${(b.ci_lo * 100).toFixed(1)}, ${(b.ci_hi * 100).toFixed(1)}])`}</title>
              </circle>
            </g>
          );
        })}
        <text x={W / 2} y={H - 4} fill="#7d8b9a" fontSize="11" textAnchor="middle">predicted</text>
        <text x={10} y={H / 2} fill="#7d8b9a" fontSize="11" textAnchor="middle" transform={`rotate(-90 10 ${H / 2})`}>realised</text>
      </svg>
    </figure>
  );
}
