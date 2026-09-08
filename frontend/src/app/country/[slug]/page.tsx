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
  params: { slug: string };
}): Promise<Metadata> {
  const name = await countryName(params.slug);
  return { title: name ?? "Country" };
}

export default async function CountryPage({
  params,
  searchParams,
}: {
  params: { slug: string };
  searchParams: { cursor?: string };
}) {
  const [name, { page, failed }] = await Promise.all([
    countryName(params.slug),
    fetchArticlesSafe({ country: params.slug, cursor: searchParams.cursor }),
  ]);

  // An unknown slug is shown as an empty country rather than a 404. The
  // reference list is cached for an hour, so a country enabled ten minutes ago
  // is legitimately absent from it — and a 404 for a page that exists is a
  // worse answer than an empty one.
  const heading = name ?? params.slug.replace(/-/g, " ");

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
        moreHref={`/country/${params.slug}?`}
      />
    </>
  );
}
