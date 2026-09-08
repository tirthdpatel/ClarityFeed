import type { Metadata, Viewport } from "next";
import { fetchCategories, fetchCountries } from "@/lib/api";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "ClarityFeed", template: "%s · ClarityFeed" },
  description: "International news headlines from public feeds, in one place.",
  // ROADMAP §1 — this deployment is unlisted. This tag, the X-Robots-Tag
  // header in next.config.mjs, and the disallow in robots.ts are three
  // expressions of one decision; change them together or not at all.
  robots: { index: false, follow: false, nocache: true },
};

/**
 * The browser paints its own chrome (address bar, notch area) with this, so it
 * has to track the *page* background rather than a single brand colour —
 * otherwise the seam above the masthead is visibly the wrong shade in one of
 * the two themes.
 */
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f4f6f7" },
    { media: "(prefers-color-scheme: dark)", color: "#101618" },
  ],
};

const NAV = [
  { href: "/", label: "Latest" },
  { href: "/about", label: "About" },
];

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Fetched in the layout so every page gets the same nav. Both calls fall
  // back to an empty list rather than throwing: a missing filter bar is a
  // degraded page, and an error screen is a broken site.
  const [countries, categories] = await Promise.all([fetchCountries(), fetchCategories()]);

  return (
    <html lang="en">
      <body>
        {/* First tab stop on every page. Without it, reaching the articles by
            keyboard means tabbing through the whole nav on each navigation. */}
        <a className="skip-link" href="#main">
          Skip to content
        </a>

        <header className="site-header">
          <div className="shell site-header__inner">
            <a className="site-header__brand" href="/">
              ClarityFeed
            </a>
            <p className="site-header__tagline">
              International headlines from public feeds
            </p>
          </div>

          <nav className="shell nav" aria-label="Sections">
            {NAV.map((item) => (
              <a className="nav__link" key={item.href} href={item.href}>
                {item.label}
              </a>
            ))}
            {categories.slice(0, 7).map((c) => (
              <a className="nav__link" key={c.slug} href={`/category/${c.slug}`}>
                {c.name}
              </a>
            ))}
          </nav>

          {countries.length > 0 ? (
            <nav className="shell nav" aria-label="Countries">
              {countries.slice(0, 14).map((c) => (
                <a className="nav__link" key={c.slug} href={`/country/${c.slug}`}>
                  {c.name}
                </a>
              ))}
            </nav>
          ) : null}
        </header>

        <main className="shell" id="main">
          {children}
        </main>

        <footer className="site-footer">
          <div className="shell">
            <p>
              ClarityFeed is an independent, non-commercial news aggregator. It
              is not affiliated with, or endorsed by, any of the publishers it
              links to.
            </p>
            <p>
              Headlines and short excerpts are shown under attribution; full
              articles stay on the publisher’s own site.{" "}
              <a href="/about">How this works, and how to request removal</a>.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
