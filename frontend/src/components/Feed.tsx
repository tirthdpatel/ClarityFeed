import type { ArticlePage, FeedFailure } from "@/lib/api";
import { ArticleCard } from "./ArticleCard";

/**
 * A page of articles, or an honest account of why there are none.
 *
 * V3 F5: the empty state is not polish. A country page with no articles today
 * is the single most likely thing a real visitor sees in month one, and a
 * blank page is indistinguishable from a broken one. So there are three
 * distinct states here and they say different things — nothing yet, nothing
 * matching this filter, and we could not reach the API — because the reader's
 * next action differs in each case.
 */
export function Feed({
  page,
  failed,
  emptyMessage,
  moreHref,
}: {
  page: ArticlePage;
  failed: FeedFailure | null;
  emptyMessage: string;
  moreHref?: string;
}) {
  if (failed === "bad-request") {
    // The service answered, and said the request was wrong. Refreshing will
    // not help; starting again from the top will.
    return (
      <div className="notice" role="status">
        <h2 className="notice__title">That link doesn’t work</h2>
        <p>
          Part of the address is out of date or mistyped — most likely a
          pagination link that has since expired.{" "}
          <a href="/">Start again from the latest stories</a>.
        </p>
      </div>
    );
  }

  if (failed === "unavailable") {
    return (
      <div className="notice" role="status">
        <h2 className="notice__title">Can’t reach the news service</h2>
        <p>
          This is usually the backend waking up after a quiet spell — it starts
          on demand and takes about a minute. Refreshing shortly should work.
        </p>
      </div>
    );
  }

  if (page.articles.length === 0) {
    return (
      <div className="notice" role="status">
        <h2 className="notice__title">Nothing here yet</h2>
        <p>{emptyMessage}</p>
      </div>
    );
  }

  return (
    <>
      <ul className="feed">
        {page.articles.map((article) => (
          <ArticleCard key={article.id} article={article} />
        ))}
      </ul>

      {page.hasMore && page.nextCursor && moreHref ? (
        <a className="more" href={`${moreHref}cursor=${encodeURIComponent(page.nextCursor)}`}>
          Older stories
        </a>
      ) : null}
    </>
  );
}
