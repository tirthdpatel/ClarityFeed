"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import type { ArchiveWindow } from "@/lib/api";

/**
 * Pick a day to read.
 *
 * A row of days rather than `<input type="date">`. The native control opens a
 * month calendar, and with a ten-day archive that means roughly twenty of the
 * thirty visible cells are greyed out — the widget spends most of its space
 * showing you what you cannot choose, behind an extra click to open it.
 * Ten days fit on one line, so every option is visible and one tap away.
 *
 * The range still comes from /archive, which derives it from the retention
 * policy, so the control cannot offer a day whose articles have been deleted.
 *
 * Radio semantics via aria-pressed on buttons: exactly one day is selected at
 * a time, and buttons carry the label text, which is what a screen reader
 * announces. Selection paints immediately and navigates in a transition, for
 * the same reason as the filters — the URL is server state and lags a click.
 */
export function DatePicker({
  archive,
  selected,
  basePath,
}: {
  archive: ArchiveWindow | null;
  selected?: string;
  basePath: string;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [draft, setDraft] = useState<string | undefined>(selected);
  useEffect(() => setDraft(selected), [selected]);

  if (!archive) return null;

  const go = (value: string) => {
    setDraft(value || undefined);
    const sep = basePath.includes("?") ? "&" : "?";
    const url = value ? `${basePath}${sep}date=${value}` : basePath;
    startTransition(() => router.push(url));
  };

  // Newest first: "today" is the common case and belongs where the eye lands.
  const days: string[] = [];
  const end = new Date(`${archive.latest}T00:00:00Z`);
  for (let i = 0; i <= archive.days; i += 1) {
    const d = new Date(end);
    d.setUTCDate(d.getUTCDate() - i);
    const iso = d.toISOString().slice(0, 10);
    if (iso < archive.earliest) break;
    days.push(iso);
  }

  const today = archive.latest;

  return (
    <nav
      className="daypick"
      aria-label="Choose a day"
      data-pending={isPending ? "true" : undefined}
    >
      <span className="daypick__label">Day</span>

      <div className="daypick__days">
        <button
          className="daypick__day"
          type="button"
          aria-pressed={!draft}
          onClick={() => go("")}
        >
          Latest
        </button>

        {days.map((iso) => (
          <button
            className="daypick__day"
            key={iso}
            type="button"
            aria-pressed={draft === iso}
            onClick={() => go(iso)}
          >
            {label(iso, today)}
          </button>
        ))}
      </div>
    </nav>
  );
}

/** "Today", then weekday plus day-of-month. Short enough to fit ten across,
 *  and a weekday is easier to place than a bare date when you are looking for
 *  "the day before yesterday". */
function label(iso: string, today: string): string {
  if (iso === today) return "Today";
  const d = new Date(`${iso}T00:00:00Z`);
  return d.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}
