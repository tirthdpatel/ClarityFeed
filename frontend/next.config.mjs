/**
 * ClarityFeed frontend configuration.
 *
 * The security headers below are deliberately set here rather than in Vercel's
 * dashboard: a header that only exists in a hosting console is invisible in
 * review, untested, and gone the day the project moves.
 */

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // Where the frontend finds the read API.
  //
  // This is not a secret. NEXT_PUBLIC_* values are compiled into the client
  // bundle by definition, so this URL is visible to anyone who opens the site;
  // the API is read-only and unauthenticated. It lives here rather than in the
  // Vercel dashboard so a fresh clone builds correctly without anyone having to
  // remember a setting that exists only in a web console.
  //
  // It is not in .env.production because the root .gitignore excludes `.env.*`,
  // and carving an exception into the rule that keeps credentials out of the
  // repo is a bad trade for one public hostname.
  //
  // An explicit NEXT_PUBLIC_API_URL still wins in both environments, which is
  // what .env.local does for local development.
  env: {
    NEXT_PUBLIC_API_URL:
      process.env.NEXT_PUBLIC_API_URL ??
      (process.env.NODE_ENV === "production"
        ? "https://clarityfeed-api.onrender.com"
        : "http://localhost:8000"),
  },

  // No `images.remotePatterns`, because no publisher images are ever rendered
  // (ROADMAP §6.1) — press photos are separately licensed and are the most
  // commonly enforced asset in this space. If that ever changes it must be a
  // deliberate edit here, not a component quietly pointing <img> at a CDN.
  images: { disableStaticImages: false },

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          // ROADMAP §1. This deployment is unlisted. The layout also emits a
          // robots meta tag; both exist because the header covers responses
          // the meta tag cannot, and neither should be the single point of
          // failure for the decision.
          { key: "X-Robots-Tag", value: "noindex, nofollow" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), interest-cohort=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
