import type { ArticlePage } from "@/lib/api";
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
  failed: boolean;
  emptyMessage: string;
  moreHref?: string;
}) {
  if (failed) {
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
