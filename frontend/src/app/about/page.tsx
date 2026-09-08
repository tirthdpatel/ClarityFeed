import type { Metadata } from "next";

export const metadata: Metadata = { title: "About" };

/**
 * ROADMAP §6.4. One page doing four jobs: explaining the project, disclosing
 * that summaries are machine-written, carrying the non-affiliation and
 * non-commercial framing, and giving publishers a route to ask for removal.
 *
 * It reads as a real service's About page rather than a disclaimer, because a
 * page that describes itself as a demo invites being treated as one. Every
 * sentence here is true; none of it is apologetic.
 */

// TODO(before deploy): replace with a real address you actually read. This is
// the entire takedown process at this scale — a publisher who emails and gets
// a same-day reply has no reason to escalate. Leaving the placeholder live
// would be worse than having no contact section at all.
const CONTACT_EMAIL = "you@example.com";

export default function AboutPage() {
  return (
    <div className="prose">
      <h1 className="page-title">About ClarityFeed</h1>

      <p>
        ClarityFeed collects headlines from the public RSS feeds of
        international news organisations and shows them in one place, sorted by
        time and filterable by country and topic. It is an independent,
        non-commercial project. It carries no advertising, sells nothing, and
        is not affiliated with, or endorsed by, any of the publishers it links
        to.
      </p>

      <h2>What is shown, and what is not</h2>
      <p>
        Each story appears as its headline, the short excerpt the publisher
        puts in their own feed, and a link. Full article text is never stored
        or displayed here, and neither are publishers’ photographs. Reading an
        article means going to the publisher’s own site, which is the point —
        this is a way of finding their journalism, not a substitute for it.
      </p>
      <p>
        Every story carries the name of the publication that reported it and a
        link back to it. An article that cannot carry both is not shown at all.
      </p>

      <h2>Summaries are machine-generated</h2>
      <p>
        Where a summary appears, it was written by a language model and is
        labelled as such wherever it is shown. These summaries can be wrong.
        They can misstate facts, miss the point of a story, or omit the context
        that makes it make sense. They are a way of deciding whether to click
        through, and nothing more — the original article is the source, and it
        is always one link away.
      </p>
      <p>
        If you find a summary that misrepresents a story, please say so at the
        address below and it will be removed.
      </p>

      <h2>How sources are chosen</h2>
      <p>
        Sources are international news organisations that publish open RSS
        feeds, selected by hand for geographic spread. Feeds are fetched on an
        hourly schedule that respects each site’s <code>robots.txt</code>, uses
        conditional requests so unchanged feeds are not re-downloaded, and is
        rate-limited per domain. The crawler identifies itself honestly as{" "}
        <code>ClarityFeedBot</code>, so any publisher who would rather it did
        not visit can say so in their <code>robots.txt</code> and it will stop.
      </p>
      <p>
        Country and topic labels are assigned automatically. They are wrong
        sometimes, and they err towards leaving a story out of a category
        rather than filing it under the wrong one.
      </p>

      <h2>Publishers: how to be removed</h2>
      <p>
        If you are a publisher and would prefer your feed not appear here,
        email <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. No
        explanation is needed. Your feed will be disabled and your stories
        removed, normally the same day, and the source will not be re-added.
      </p>
      <p>
        The same address works for corrections, for a summary that misrepresents
        your reporting, or for anything else about how your content is used
        here.
      </p>

      <h2>Privacy</h2>
      <p>
        There are no accounts, no comments, no newsletter, and no advertising
        or tracking cookies. Nothing you do here is tied to an identity, because
        there is no identity to tie it to.
      </p>
      <p>
        The infrastructure this runs on keeps ordinary server logs — the sort
        every web server keeps, including IP address and page requested. The
        services involved are Vercel (this website), Render (the API), Supabase
        (the database) and GitHub Actions (the hourly fetch). Article text
        retrieved from public feeds is sent to Groq to generate the summaries
        described above.
      </p>
      <p>
        For anything about this, including a request to delete something, use
        the same address: <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>

      <h2>No warranty</h2>
      <p>
        This is provided as-is, with no guarantee that it is accurate, current,
        or available. Headlines, excerpts, classifications and summaries may all
        be wrong. Do not rely on it as a sole source for anything that matters;
        follow the link and read the reporting.
      </p>
    </div>
  );
}
