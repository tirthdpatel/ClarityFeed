"use client";

import { useRouter } from "next/navigation";
import type { ArchiveWindow } from "@/lib/api";

/**
 * Pick a day to read.
 *
 * The range comes from the API's /archive endpoint, which derives it from the
 * retention policy — so the picker cannot offer a date whose articles have
 * already been deleted. Hardcoding "10 days" here would drift the first time
 * the policy changed, and the failure would look like a broken page rather
 * than a stale constant.
 *
 * A plain <input type="date"> rather than a calendar component: it is
 * keyboard-accessible and screen-reader-labelled for free, it uses the
 * viewer's own locale for display, and it costs no JavaScript beyond this.
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
  if (!archive) return null;

  const go = (value: string) => {
    const sep = basePath.includes("?") ? "&" : "?";
    router.push(value ? `${basePath}${sep}date=${value}` : basePath);
  };

  return (
    <div className="datepick">
      <label className="datepick__label" htmlFor="date">
        Read the news from
      </label>
      <input
        className="datepick__input"
        id="date"
        type="date"
        value={selected ?? ""}
        min={archive.earliest}
        max={archive.latest}
        onChange={(e) => go(e.target.value)}
      />
      {selected ? (
        <button className="datepick__clear" type="button" onClick={() => go("")}>
          Back to latest
        </button>
      ) : null}
      <span className="datepick__note">
        Archive covers the last {archive.days} days.
      </span>
    </div>
  );
}
