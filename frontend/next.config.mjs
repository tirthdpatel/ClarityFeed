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
