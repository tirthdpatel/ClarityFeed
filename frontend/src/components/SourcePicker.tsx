"use client";

import { useRouter } from "next/navigation";
import type { SourceRef } from "@/lib/api";

/**
 * Choose which publishers to read.
 *
 * Checkboxes rather than a custom multi-select widget: they are keyboard
 * operable and announced correctly by screen readers with no work, and the
 * whole list is eight items — a dropdown would hide the options behind an
 * interaction for no gain.
 *
 * Selection lives in the URL, not in component state, so a filtered view can
 * be bookmarked and shared, the back button behaves, and the server renders
 * the right articles on first paint instead of flashing the unfiltered feed.
 *
 * "All" is the absence of the parameter rather than every id listed, so the
 * default URL stays clean and a publisher added later is included by default
 * instead of being silently excluded by a stale link.
 */
export function SourcePicker({
  sources,
  selected,
  basePath,
}: {
  sources: SourceRef[];
  selected: number[];
  basePath: string;
}) {
  const router = useRouter();
  if (sources.length < 2) return null;

  const push = (ids: number[]) => {
    const sep = basePath.includes("?") ? "&" : "?";
    const all = ids.length === 0 || ids.length === sources.length;
    router.push(all ? basePath : `${basePath}${sep}source=${ids.join(",")}`);
  };

  const toggle = (id: number) => {
    const active = selected.length ? selected : sources.map((s) => s.id);
    push(active.includes(id) ? active.filter((x) => x !== id) : [...active, id]);
  };

  const showingAll = selected.length === 0;

  return (
    <fieldset className="srcpick">
      <legend className="srcpick__legend">Publishers</legend>

      <div className="srcpick__options">
        {sources.map((s) => {
          const on = showingAll || selected.includes(s.id);
          return (
            <label className="srcpick__opt" key={s.id}>
              <input
                type="checkbox"
                checked={on}
                onChange={() => toggle(s.id)}
              />
              <span>{s.name}</span>
            </label>
          );
        })}
      </div>

      {!showingAll ? (
        <button className="srcpick__clear" type="button" onClick={() => push([])}>
          All publishers
        </button>
      ) : null}
    </fieldset>
  );
}
