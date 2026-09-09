"use client";

import { usePathname, useRouter } from "next/navigation";
import type { CategoryRef, CountryRef } from "@/lib/api";

/**
 * Country and category navigation for narrow screens.
 *
 * On a phone the link rails wrapped to four or five rows and pushed the first
 * headline below the fold — a reader had to scroll past the navigation to
 * reach the thing they came for. Two selects take one row.
 *
 * Native <select> rather than a custom menu: it opens the platform's own
 * picker, which is already keyboard accessible, screen-reader labelled, and
 * scrollable at any list length. A hand-built dropdown would be more code and
 * worse on every one of those counts.
 *
 * Visibility is CSS-only (see .mobilenav / .nav-rail in globals.css) so both
 * navigations are in the server-rendered HTML and neither depends on
 * JavaScript deciding which to show. The cost is a little duplicate markup;
 * the benefit is that the correct nav is present on first paint.
 */
export function MobileNav({
  countries,
  categories,
}: {
  countries: CountryRef[];
  categories: CategoryRef[];
}) {
  const router = useRouter();
  const pathname = usePathname();

  // Derive the current selection from the URL so the control reflects where
  // you actually are, rather than resetting to "All" on every navigation.
  const countrySlug = pathname.startsWith("/country/")
    ? decodeURIComponent(pathname.slice("/country/".length))
    : "";
  const categorySlug = pathname.startsWith("/category/")
    ? decodeURIComponent(pathname.slice("/category/".length))
    : "";

  if (countries.length === 0 && categories.length === 0) return null;

  return (
    <nav className="mobilenav" aria-label="Browse by country or topic">
      {categories.length > 0 ? (
        <p className="mobilenav__field">
          <label className="mobilenav__label" htmlFor="m-cat">
            Topic
          </label>
          <select
            className="mobilenav__select"
            id="m-cat"
            value={categorySlug}
            onChange={(e) =>
              router.push(e.target.value ? `/category/${e.target.value}` : "/")
            }
          >
            <option value="">All topics</option>
            {categories.map((c) => (
              <option key={c.slug} value={c.slug}>
                {c.parentSlug ? `  ${c.name}` : c.name}
              </option>
            ))}
          </select>
        </p>
      ) : null}

      {countries.length > 0 ? (
        <p className="mobilenav__field">
          <label className="mobilenav__label" htmlFor="m-country">
            Country
          </label>
          <select
            className="mobilenav__select"
            id="m-country"
            value={countrySlug}
            onChange={(e) =>
              router.push(e.target.value ? `/country/${e.target.value}` : "/")
            }
          >
            <option value="">All countries</option>
            {countries.map((c) => (
              <option key={c.slug} value={c.slug}>
                {c.flag ? `${c.flag} ` : ""}
                {c.name}
              </option>
            ))}
          </select>
        </p>
      ) : null}
    </nav>
  );
}
