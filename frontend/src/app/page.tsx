import { Feed } from "@/components/Feed";
import { StaleNotice } from "@/components/StaleNotice";
import { fetchArticlesSafe } from "@/lib/api";

export default async function HomePage({
  searchParams,
}: {
  searchParams: { cursor?: string };
}) {
  const { page, failed } = await fetchArticlesSafe({ cursor: searchParams.cursor });

  return (
    <>
      <h1 className="page-title">Latest</h1>
      <p className="page-sub">
        Newest first, from {new Set(page.articles.map((a) => a.source.name)).size || "several"}{" "}
        publishers. Every headline links to the original.
      </p>

      <StaleNotice latestPublishedAt={page.articles[0]?.publishedAt ?? null} />

      <Feed
        page={page}
        failed={failed}
        emptyMessage="No stories have been published yet. The feed updates hourly."
        moreHref="/?"
      />
    </>
  );
}
