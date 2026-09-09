import { FeedSkeleton } from "@/components/FeedSkeleton";

/**
 * Next renders this the moment a navigation begins, before the server has
 * finished. Without it, clicking a category looked like nothing happened — the
 * browser sat on the old page while the request went to a free-tier API that
 * may be cold-starting, which reads as a hang rather than as loading.
 */
export default function Loading() {
  return (
    <>
      <h1 className="page-title">Loading…</h1>
      <p className="page-sub">Fetching the latest stories.</p>
      <FeedSkeleton />
    </>
  );
}
