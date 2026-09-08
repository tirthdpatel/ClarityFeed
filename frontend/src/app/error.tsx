"use client";

/**
 * The last line of defence. Individual data fetches already degrade to empty
 * states rather than throwing (see lib/api.ts), so reaching this means
 * something genuinely unexpected happened — and the reader still gets a page
 * with a way out rather than a stack trace.
 */
export default function Error({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="notice" role="alert" style={{ marginTop: "2rem" }}>
      <h1 className="notice__title">Something went wrong</h1>
      <p>
        This one is on us.{" "}
        <button
          onClick={reset}
          style={{
            background: "none",
            border: "none",
            padding: 0,
            font: "inherit",
            color: "var(--accent)",
            textDecoration: "underline",
            cursor: "pointer",
          }}
        >
          Try again
        </button>
        , or <a href="/">go back to the latest stories</a>.
      </p>
    </div>
  );
}
