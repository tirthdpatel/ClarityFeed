import type { MetadataRoute } from "next";

/**
 * ROADMAP §1. Unlisted means unlisted: no crawling, and deliberately no
 * sitemap — a sitemap is an invitation, which is the opposite of the intent.
 *
 * This is one of three places the decision is expressed (see layout.tsx and
 * next.config.mjs). Going public means removing all three, and removing this
 * one should be the *last* step rather than the first, because it is the one
 * that actually makes the site discoverable.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [{ userAgent: "*", disallow: "/" }],
  };
}
