import type { ArticleSummary } from "@/lib/api";

/**
 * The label is not decoration and is not optional (ROADMAP §6.2).
 *
 * An LLM summary that states a false fact about a named living person is
 * defamation, and the mitigation that costs nothing is telling the reader a
 * machine wrote it and giving them the original in the same breath. So the
 * label is rendered from the same object as the text: there is no prop that
 * turns it off, and a summary with no text renders nothing at all rather than
 * an empty labelled box.
 */
export function AiSummary({
  summary,
  originalUrl,
}: {
  summary: ArticleSummary | null;
  originalUrl: string;
}) {
  if (!summary) return null;

  const hasText = Boolean(summary.tldr) || summary.bulletPoints.length > 0;
  if (!hasText) return null;

  return (
    <aside className="ai-summary" aria-label="Machine-generated summary">
      <p className="ai-summary__label">Machine summary</p>

      {summary.tldr ? <p className="ai-summary__text">{summary.tldr}</p> : null}

      {summary.bulletPoints.length > 0 ? (
        <ul className="ai-summary__bullets">
          {summary.bulletPoints.map((point, i) => (
            <li key={i}>{point}</li>
          ))}
        </ul>
      ) : null}

      <p className="ai-summary__note">
        {summary.disclaimer}{" "}
        <a href={originalUrl} rel="noopener noreferrer nofollow" target="_blank">
          Read the original
        </a>
        .
      </p>
    </aside>
  );
}
