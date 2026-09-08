"use client";

import { useEffect, useState } from "react";

/**
 * Relative time, rendered on the client from a UTC instant (V3 F8).
 *
 * Server-rendering "2 hours ago" would bake the build time into a cached page
 * and show a stale figure for as long as the page lives. The absolute time
 * stays in `dateTime` and `title` so it is available to screen readers and on
 * hover, and the first paint shows the absolute date rather than nothing —
 * which keeps the page useful if JavaScript never arrives.
 */
export function Timestamp({ iso }: { iso: string | null }) {
  const [relative, setRelative] = useState<string | null>(null);

  useEffect(() => {
    if (!iso) return;
    const update = () => setRelative(toRelative(iso));
    update();
    // Re-render on a slow tick so a page left open does not sit at "just now"
    // for an hour.
    const timer = setInterval(update, 60_000);
    return () => clearInterval(timer);
  }, [iso]);

  if (!iso) return null;

  const absolute = new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });

  return (
    <time dateTime={iso} title={absolute}>
      {relative ?? absolute}
    </time>
  );
}

function toRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";

  const seconds = Math.round((Date.now() - then) / 1000);
  // A feed's published_at is occasionally slightly in the future — a
  // publisher's clock, or a scheduled post. "in 3 minutes" on a news site
  // reads as a bug, so anything near-future is just "just now".
  if (seconds < 60) return "just now";

  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31_536_000],
    ["month", 2_592_000],
    ["week", 604_800],
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
  ];
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) {
      return formatter.format(-Math.round(seconds / size), unit);
    }
  }
  return "just now";
}
