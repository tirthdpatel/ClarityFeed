"use client";

import { useRouter } from "next/navigation";
import type { CategoryRef, CountryRef, SourceRef } from "@/lib/api";

/**
 * Country, topic and publisher filters in one place.
 *
 * Each group is OR within itself and AND across groups: picking India and the
 * UK asks for news from either, while adding Sports narrows that to sport
 * from either. That is what a reader means by "India and UK sport", and it is
 * the only combination where adding a second country widens rather than
 * empties the result.
 *
 * Only top-level topics are offered. The taxonomy has 57 entries and the
 * classifier tags leaves, so listing all of them would be a wall of checkboxes
 * where most are near-empty; the ten roots each match their own descendants,
 * which is the same coverage in a usable control.
 *
 * State lives in the URL, so a filtered view is shareable and bookmarkable,
 * the back button works, and the server renders the right articles on first
 * paint instead of flashing the unfiltered feed.
 *
 * Nothing selected in a group means no filter for that group, spelled as the
 * absence of the parameter. That keeps the default URL clean and means a
 * country or publisher added later is included by default rather than being
 * silently excluded by a stale link.
 */
type Group = { key: string; legend: string; options: { value: string; label: string }[] };

export function Filters({
  countries,
  categories,
  sources,
  selected,
}: {
  countries: CountryRef[];
  categories: CategoryRef[];
  sources: SourceRef[];
  selected: { country: string[]; category: string[]; source: string[] };
}) {
  const router = useRouter();

  const groups: Group[] = [
    {
      key: "category",
      legend: "Topic",
      // Roots only — each already matches everything beneath it.
      options: categories
        .filter((c) => !c.parentSlug)
        .map((c) => ({ value: c.slug, label: c.name })),
    },
    {
      key: "country",
      legend: "Country",
      options: countries.map((c) => ({
        value: c.slug,
        label: c.flag ? `${c.flag} ${c.name}` : c.name,
      })),
    },
    {
      key: "source",
      legend: "Publisher",
      options: sources.map((s) => ({ value: String(s.id), label: s.name })),
    },
  ].filter((g) => g.options.length > 0);

  if (groups.length === 0) return null;

  const current: Record<string, string[]> = {
    country: selected.country,
    category: selected.category,
    source: selected.source,
  };

  const apply = (next: Record<string, string[]>) => {
    const qs = new URLSearchParams();
    for (const [key, values] of Object.entries(next)) {
      if (values.length) qs.set(key, values.join(","));
    }
    const query = qs.toString();
    router.push(query ? `/?${query}` : "/");
  };

  const toggle = (key: string, value: string) => {
    const active = current[key] ?? [];
    apply({
      ...current,
      [key]: active.includes(value)
        ? active.filter((v) => v !== value)
        : [...active, value],
    });
  };

  const activeCount =
    selected.country.length + selected.category.length + selected.source.length;

  return (
    <section className="filters" aria-label="Filter the feed">
      <div className="filters__head">
        <h2 className="filters__title">Filter</h2>
        {activeCount > 0 ? (
          <button
            className="filters__clear"
            type="button"
            onClick={() => apply({ country: [], category: [], source: [] })}
          >
            Clear {activeCount} filter{activeCount === 1 ? "" : "s"}
          </button>
        ) : (
          <span className="filters__hint">Showing everything</span>
        )}
      </div>

      <div className="filters__groups">
        {groups.map((group) => (
          <fieldset className="filters__group" key={group.key}>
            <legend className="filters__legend">{group.legend}</legend>
            <div className="filters__opts">
              {group.options.map((opt) => {
                const on = (current[group.key] ?? []).includes(opt.value);
                return (
                  <label className="filters__opt" key={opt.value}>
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => toggle(group.key, opt.value)}
                    />
                    <span>{opt.label}</span>
                  </label>
                );
              })}
            </div>
          </fieldset>
        ))}
      </div>
    </section>
  );
}
