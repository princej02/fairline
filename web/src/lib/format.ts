export const pct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(digits)}%`;

export const signedPct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;

export const odds = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(2));

export const money = (v: number | null | undefined, currency = "GBP") =>
  v === null || v === undefined ? "—" : new Intl.NumberFormat("en-GB", { style: "currency", currency }).format(v);

export const kickoff = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/London" });

export const selectionLabel = (market: string, selection: string, line: number | null) =>
  market === "1X2" ? { home: "Home", draw: "Draw", away: "Away" }[selection] ?? selection : `${selection === "over" ? "Over" : "Under"} ${line ?? ""}`.trim();

export const book = (code: string | null | undefined) =>
  ({ pinnacle: "Pinnacle", pinnacle_derived: "Pinnacle (derived)", betfair_ex_uk: "Betfair Ex", bet365: "Bet365", skybet: "Sky Bet", williamhill: "William Hill", paddypower: "Paddy Power" })[code ?? ""] ?? code ?? "—";

export const evTone = (v: number | null | undefined) =>
  v === null || v === undefined ? "text-muted" : v >= 0 ? "text-pos" : "text-neg";
