import type { Metadata } from "next";
import { Feed } from "@/components/Feed";
import { StaleNotice } from "@/components/StaleNotice";
import { fetchArticlesSafe, fetchCountries } from "@/lib/api";

async function countryName(slug: string): Promise<string | null> {
  const countries = await fetchCountries();
  return countries.find((c) => c.slug === slug)?.name ?? null;
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const name = await countryName(slug);
  return { title: name ?? "Country" };
}

export default async function CountryPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ cursor?: string }>;
}) {
  const [{ slug }, { cursor }] = await Promise.all([params, searchParams]);
  const [name, { page, failed }] = await Promise.all([
    countryName(slug),
    fetchArticlesSafe({ country: slug, cursor }),
  ]);

  // An unknown slug is shown as an empty country rather than a 404. The
  // reference list is cached for an hour, so a country enabled ten minutes ago
  // is legitimately absent from it — and a 404 for a page that exists is a
  // worse answer than an empty one.
  const heading = name ?? slug.replace(/-/g, " ");

  return (
    <>
      <h1 className="page-title" style={{ textTransform: name ? "none" : "capitalize" }}>
        {heading}
      </h1>
      <p className="page-sub">Stories filed under this country.</p>

      <StaleNotice latestPublishedAt={page.articles[0]?.publishedAt ?? null} />

      <Feed
        page={page}
        failed={failed}
        emptyMessage={`Nothing filed under ${heading} in the current window. Country tagging is automatic and errs towards leaving a story out rather than filing it wrongly.`}
        moreHref={`/country/${slug}?`}
      />
    </>
  );
}
