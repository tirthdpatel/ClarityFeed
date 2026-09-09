import { permanentRedirect } from "next/navigation";

/**
 * Kept as a redirect, not deleted.
 *
 * Browsing by topic used to be its own route, and those URLs are in browser
 * history, in anything anyone has shared, and in the tags on every article
 * card rendered before this change. Deleting the route would 404 all of them;
 * forwarding costs one file and keeps them working.
 *
 * 308 rather than 307: the move is permanent, so a client may cache it and
 * stop asking. Filtering lives on the home page now — one place to browse,
 * instead of two that could not be combined with each other.
 *
 * Other search params are carried through, so a link that already carried a
 * date or a publisher does not silently lose it on the way.
 */
export default async function CategoryRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const [{ slug }, rest] = await Promise.all([params, searchParams]);

  const qs = new URLSearchParams();
  qs.set("category", slug);
  for (const [key, value] of Object.entries(rest)) {
    if (key === "category" || value === undefined) continue;
    qs.set(key, Array.isArray(value) ? value.join(",") : value);
  }

  permanentRedirect(`/?${qs.toString()}`);
}
