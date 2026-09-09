/**
 * The one place that talks to the backend.
 *
 * Every fetch here is a server-side fetch with an explicit `revalidate`, which
 * is what keeps Render's free tier out of the reader's path: the API is hit on
 * revalidation, not per visitor. That matters more than it looks — a free-tier
 * instance cold-starts in roughly a minute, and without ISR every first
 * visitor after a quiet spell would wait it out.
 */

// next.config.mjs always supplies this — production falls back to the Render
// service, development to localhost — so there is no fallback here. A second
// default in this file would be dead code that still ships its string to the
// browser, which makes "which URL is this build actually using?" ambiguous to
// answer by inspecting the bundle.
const API_BASE = process.env.NEXT_PUBLIC_API_URL!.replace(/\/$/, "");

export interface Attribution {
  required: boolean;
  readOriginalUrl: string;
}

export interface ArticleSummary {
  tldr: string | null;
  bulletPoints: string[];
  /** Always true when present. The API refuses to serialize a summary
   *  without it, so the UI cannot render machine text as human by accident. */
  aiGenerated: boolean;
  disclaimer: string;
  model: string | null;
  generatedAt: string | null;
}

export interface Article {
  id: number;
  title: string;
  description: string | null;
  /** Always null. No source licences full text, and rendering a publisher's
   *  body on our own domain would be reproduction rather than referral. */
  content: null;
  imageUrl: null;
  url: string;
  publishedAt: string | null;
  source: { id: number; name: string; url: string | null; language: string | null };
  categories: { slug: string; name: string; isPrimary: boolean }[];
  countries: { iso2: string; name: string; slug: string; flag: string | null; relevance: string }[];
  summary: ArticleSummary | null;
  attribution: Attribution;
}

export interface ArticlePage {
  articles: Article[];
  nextCursor: string | null;
  hasMore: boolean;
}

export interface CountryRef {
  iso2: string;
  name: string;
  slug: string;
  flag: string | null;
  regionCode: string | null;
  regionName: string | null;
  articleCount: number;
}

export interface CategoryRef {
  slug: string;
  name: string;
  parentSlug: string | null;
  colorToken: string | null;
}

/** Seconds. Ingestion is hourly, so anything under that is wasted work. */
const FEED_REVALIDATE = 300;
const REFERENCE_REVALIDATE = 3600;

class ApiError extends Error {}

async function get<T>(path: string, revalidate: number): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    next: { revalidate },
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new ApiError(`GET ${path} failed: ${res.status}`);
  }
  return (await res.json()) as T;
}

/**
 * Reference data is a *nav bar*. If it cannot be fetched the page should still
 * render its articles rather than 500 — a missing filter control is a
 * degraded page, and an error screen is a broken site.
 */
async function getOrEmpty<T>(path: string, revalidate: number, fallback: T): Promise<T> {
  try {
    return await get<T>(path, revalidate);
  } catch {
    return fallback;
  }
}

export async function fetchArticles(
  params: Record<string, string | number | undefined> = {},
): Promise<ArticlePage> {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") qs.set(key, String(value));
  }
  const query = qs.toString();
  return get<ArticlePage>(`/articles${query ? `?${query}` : ""}`, FEED_REVALIDATE);
}

/** Feed failures are shown as an empty state, not an error page: the most
 *  likely cause is the free tier waking up, and that resolves itself. */
export async function fetchArticlesSafe(
  params: Record<string, string | number | undefined> = {},
): Promise<{ page: ArticlePage; failed: boolean }> {
  try {
    return { page: await fetchArticles(params), failed: false };
  } catch {
    return { page: { articles: [], nextCursor: null, hasMore: false }, failed: true };
  }
}

export const fetchCountries = () =>
  getOrEmpty<CountryRef[]>("/countries", REFERENCE_REVALIDATE, []);

export const fetchCategories = () =>
  getOrEmpty<CategoryRef[]>("/categories", REFERENCE_REVALIDATE, []);

export async function fetchArticle(id: string): Promise<Article | null> {
  try {
    return await get<Article>(`/articles/${id}`, FEED_REVALIDATE);
  } catch {
    return null;
  }
}
