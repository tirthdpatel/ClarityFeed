/**
 * Shown while a page's data is being fetched.
 *
 * This is not decoration for this project specifically: the API sleeps on
 * Render's free tier and cold-starts in roughly a minute, so a real reader
 * will meet this screen. A spinner would say "something is happening"; a
 * skeleton in the shape of the feed says "articles are coming, here is where
 * they will be", and the page does not jump when they arrive.
 */
export default function Loading() {
  return (
    <>
      <div className="skeleton skeleton--title" />
      <div className="skeleton skeleton--sub" />

      <ul className="feed" aria-busy="true" aria-live="polite" aria-label="Loading stories…">
        {[0, 1, 2, 3].map((i) => (
          <li className="card skeleton-card" key={i}>
            <div className="skeleton skeleton--source" />
            <div className="skeleton skeleton--headline" />
            <div className="skeleton skeleton--line" />
            <div className="skeleton skeleton--line skeleton--line-short" />
          </li>
        ))}
      </ul>
    </>
  );
}
