"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ErrorBox, Loading } from "@/components/ui";
import { api } from "@/lib/api";

export default function Page() {
  const q = useQuery({ queryKey: ["backtests"], queryFn: api.backtests });
  if (q.isLoading) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Backtests</h1>
      <div className="rounded-lg border border-line">
        {q.data!.length === 0 && <div className="p-6 text-sm text-muted">No runs yet. Run <code>fairline backtest run --persist</code>.</div>}
        {q.data!.map((b) => (
          <Link key={b.id} href={`/backtests/${b.id}`} className="flex items-center justify-between border-b border-line px-4 py-3 last:border-0 hover:bg-panel">
            <span>Run #{b.id} · {b.pools.join(", ")} · {b.first} to {b.last}</span>
            <span className="num text-xs text-muted">{new Date(b.started_at).toLocaleString("en-GB")}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}
