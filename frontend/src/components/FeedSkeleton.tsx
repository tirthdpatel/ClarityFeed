/**
 * Placeholder shown while a page's articles are being fetched.
 *
 * `aria-busy` plus a polite live region so a screen reader is told the page is
 * loading rather than being read an empty list. Purely decorative bars are
 * hidden from the accessibility tree.
 */
export function FeedSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-live="polite">
      <span className="visually-hidden">Loading stories…</span>
      <ul className="skeleton" aria-hidden="true">
        {Array.from({ length: rows }, (_, i) => (
          <li className="skeleton__item" key={i}>
            <div className="skeleton__line skeleton__line--meta" />
            <div className="skeleton__line skeleton__line--title" />
            <div className="skeleton__line skeleton__line--title2" />
            <div className="skeleton__line skeleton__line--body" />
            <div className="skeleton__line skeleton__line--body2" />
          </li>
        ))}
      </ul>
    </div>
  );
}
