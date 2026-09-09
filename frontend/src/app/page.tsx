import { DatePicker } from "@/components/DatePicker";
import { Feed } from "@/components/Feed";
import { SourcePicker } from "@/components/SourcePicker";
import { StaleNotice } from "@/components/StaleNotice";
import { fetchArchive, fetchArticlesSafe, fetchSources } from "@/lib/api";

export default async function HomePage({
  searchParams,
}: {
  // Promises since Next 15: the framework no longer resolves route inputs
  // before the component runs, so they are awaited like any other async data.
  searchParams: Promise<{ cursor?: string; date?: string; source?: string }>;
}) {
  const { cursor, date, source } = await searchParams;
  const [{ page, failed }, archive, sources] = await Promise.all([
    fetchArticlesSafe({ cursor, date, source }),
    fetchArchive(),
    fetchSources(),
  ]);

  const selectedSources = (source ?? "")
    .split(",")
    .map((s) => Number(s))
    .filter((n) => Number.isFinite(n) && n > 0);

  // Filters compose, so every link out of this page has to carry the ones
  // already applied. Dropping `source` when paginating would silently widen
  // the feed on page two.
  const qs = new URLSearchParams();
  if (date) qs.set("date", date);
  if (source) qs.set("source", source);
  const base = qs.toString() ? `/?${qs.toString()}&` : "/?";

  return (
    <>
      <h1 className="page-title">{date ? formatDay(date) : "Latest"}</h1>
      <p className="page-sub">
        Newest first, from {new Set(page.articles.map((a) => a.source.name)).size || "several"}{" "}
        publishers. Every headline links to the original.
      </p>

      <SourcePicker
        sources={sources}
        selected={selectedSources}
        basePath={date ? `/?date=${date}` : "/"}
      />

      <DatePicker
        archive={archive}
        selected={date}
        basePath={source ? `/?source=${source}` : "/"}
      />

      {/* A chosen day is a fixed window, so "the feed may be stale" is
          meaningless there — of course yesterday's page shows yesterday. */}
      {date ? null : (
        <StaleNotice latestPublishedAt={page.articles[0]?.publishedAt ?? null} />
      )}

      <Feed
        page={page}
        failed={failed}
        emptyMessage={
          date
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
