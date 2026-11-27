"use client";

import { useSyncExternalStore } from "react";
import type { AccaLegIn } from "@/lib/api";

// A tiny external store backed by sessionStorage (per tab, survives reloads). Reads and writes
// are wrapped because storage can be unavailable (private mode, blocked site data).
const KEY = "fairline.accaSlip";
const EMPTY: AccaLegIn[] = [];
let legs: AccaLegIn[] | null = null;
const listeners = new Set<() => void>();

function read(): AccaLegIn[] {
  if (legs === null) {
    try {
      legs = JSON.parse(sessionStorage.getItem(KEY) ?? "[]");
    } catch {
      legs = [];
    }
  }
  return legs!;
}

function write(next: AccaLegIn[]) {
  legs = next;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(next));
  } catch {}
  listeners.forEach((l) => l());
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

const same = (a: AccaLegIn, b: AccaLegIn) =>
  a.event_id === b.event_id && a.market === b.market && a.selection === b.selection && a.line === b.line;

interface Slip {
  legs: AccaLegIn[];
  add: (leg: AccaLegIn) => void;
  remove: (i: number) => void;
  clear: () => void;
  has: (leg: AccaLegIn) => boolean;
}

const actions = {
  add: (leg: AccaLegIn) => {
    if (!read().some((l) => same(l, leg))) write([...read(), leg]);
  },
  remove: (i: number) => write(read().filter((_, j) => j !== i)),
  clear: () => write([]),
};

/** Each consumer subscribes to the store directly (no context), so every component hydrates
 *  with the empty server snapshot and only then picks up the stored slip. A context update
 *  flowing into a not-yet-hydrated Suspense boundary would cause a hydration mismatch. */
export function useAccaSlip(): Slip {
  const legs = useSyncExternalStore(subscribe, read, () => EMPTY);
  return { legs, ...actions, has: (leg) => legs.some((l) => same(l, leg)) };
}
