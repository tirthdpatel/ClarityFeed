import { DatePicker } from "@/components/DatePicker";
import { Feed } from "@/components/Feed";
import { Filters } from "@/components/Filters";
import { StaleNotice } from "@/components/StaleNotice";
import {
  fetchArchive,
  fetchArticlesSafe,
  fetchCategories,
  fetchCountries,
  fetchSources,
} from "@/lib/api";

export default async function HomePage({
  searchParams,
}: {
  // Promises since Next 15: the framework no longer resolves route inputs
  // before the component runs, so they are awaited like any other async data.
  searchParams: Promise<{
    cursor?: string;
    date?: string;
    source?: string;
    country?: string;
    category?: string;
  }>;
}) {
  const { cursor, date, source, country, category } = await searchParams;

  const [{ page, failed }, archive, sources, countries, categories] =
    await Promise.all([
      fetchArticlesSafe({ cursor, date, source, country, category }),
      fetchArchive(),
      fetchSources(),
      fetchCountries(),
      fetchCategories(),
    ]);

  const list = (v?: string) =>
    (v ?? "").split(",").map((x) => x.trim()).filter(Boolean);

  const selected = {
    country: list(country),
    category: list(category),
    source: list(source),
  };

  // Filters compose, so every link out of this page has to carry the ones
  // already applied. Dropping one when paginating would silently widen the
  // feed on page two.
  const qs = new URLSearchParams();
  if (date) qs.set("date", date);
  if (source) qs.set("source", source);
  if (country) qs.set("country", country);
  if (category) qs.set("category", category);
  const base = qs.toString() ? `/?${qs.toString()}&` : "/?";

  // The date picker has to preserve the other filters too, and vice versa.
  const withoutDate = new URLSearchParams(qs);
  withoutDate.delete("date");
  const datePickerBase = withoutDate.toString()
    ? `/?${withoutDate.toString()}`
    : "/";

  // Counts the publishers actually on this page, which changes as filters are
  // applied — and says "1 publisher", not "1 publishers". A stray plural is
  // the kind of thing that quietly signals nobody read the page.
  const shown = new Set(page.articles.map((a) => a.source.name)).size;
  const subtitle =
    shown === 0
      ? "Every headline links to the original."
      : `Newest first, from ${shown} publisher${shown === 1 ? "" : "s"}. ` +
        "Every headline links to the original.";

  return (
    <>
      <h1 className="page-title">{date ? formatDay(date) : "Latest"}</h1>
      <p className="page-sub">{subtitle}</p>

      <Filters
        countries={countries}
        categories={categories}
        sources={sources}
        selected={selected}
      />

      <DatePicker archive={archive} selected={date} basePath={datePickerBase} />

      {/* A chosen day is a fixed window, so "the feed may be stale" is
          meaningless there — of course yesterday's page shows yesterday. */}
      {date ? null : (
        <StaleNotice latestPublishedAt={page.articles[0]?.publishedAt ?? null} />
      )}

      <Feed
        page={page}
        failed={failed}
        emptyMessage={
          selected.country.length || selected.category.length || selected.source.length
            ? "Nothing matches this combination of filters. Try removing one."
            : date
              ? "Nothing was published on this day. Try another date."
              : "No stories have been published yet. The feed updates hourly."
        }
        moreHref={base}
      />
    </>
  );
}

/** "9 September 2026" in the server's locale, from a YYYY-MM-DD string.
 *  Parsed as UTC to match the API's day boundaries — `new Date("2026-09-09")`
 *  is already UTC midnight, but being explicit stops a later refactor from
 *  quietly shifting the label a day in a negative-offset timezone. */
function formatDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, {
        dateStyle: "long",
        timeZone: "UTC",
      });
}
