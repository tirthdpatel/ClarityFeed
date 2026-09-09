import type { Article } from "@/lib/api";
import { sourceHue } from "@/lib/sourceColor";
import { AiSummary } from "./AiSummary";
import { Timestamp } from "./Timestamp";

/**
 * One article. The headline links out to the publisher, never inward.
 *
 * There is no article *reader* page on this site, and that is a legal decision
 * as much as a product one: the moment article text renders on our own domain
 * we are reproducing rather than referring (ROADMAP §5). The card shows the
 * headline, the publisher's own excerpt, and — where one exists — a clearly
 * labelled machine summary. Everything else is a link.
 */
export function ArticleCard({ article }: { article: Article }) {
  const href = article.attribution.readOriginalUrl;
  const hue = sourceHue(article.source.name);

  return (
    // The spine is the publisher. Colour is set per-card as a custom property
    // so one CSS rule can tint the rail, the name and the summary's own marker
    // together, and stay in step with the theme.
    <li className="card" style={{ "--source-hue": hue } as React.CSSProperties}>
      <p className="card__meta">
        <span className="card__source">{article.source.name}</span>
        <Timestamp iso={article.publishedAt} />
      </p>

      <h2 className="card__title">
        {/*
          `noopener` because target=_blank without it hands the opened page a
          handle on this one. `nofollow` because we are not conferring ranking
          on hundreds of outbound links a day, and this site is noindex anyway.
        */}
        <a href={href} rel="noopener noreferrer nofollow" target="_blank">
          {article.title}
          <span className="visually-hidden"> (opens {article.source.name} in a new tab)</span>
        </a>
      </h2>

      {article.description ? <p className="card__desc">{article.description}</p> : null}

      <AiSummary summary={article.summary} originalUrl={href} />

      {article.categories.length > 0 || article.countries.length > 0 ? (
        <p className="card__tags">
          {article.countries.slice(0, 3).map((c) => (
            /* No flag emoji: Windows ships no flag glyphs at all and renders
               a bare two-letter pair instead, so the "decorative" version of
               this is broken text for a large share of readers. Decided on
               correctness, not taste (design-council tie-break 3). */
            <a className="tag" key={c.slug} href={`/?country=${c.slug}`}>
              {c.name}
            </a>
          ))}
          {article.categories.slice(0, 3).map((c) => (
            <a className="tag" key={c.slug} href={`/?category=${c.slug}`}>
              {c.name}
            </a>
          ))}
        </p>
      ) : null}
    </li>
  );
}
