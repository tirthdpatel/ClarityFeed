# ClarityFeed — roadmap to launch

**Status: 8 Sep 2026.** This is the execution plan from today's *verified*
state to a deployed site. It supersedes ARCHITECTURE_V3 Part G as the working
plan; V2 and V3 remain the design reference for *how* each piece should behave.

**Target posture: unlisted personal project** — usable by people you send the
link to, and viewable by anyone reading your CV, but not indexed, not
advertised, and not commercial. §1 explains what that changes. §11 covers the
delta if you later decide to go properly public.

The difference between this document and Part G is that Part G was written
before Phase 2 landed and describes intended scope. This one starts from what
is actually in the repository and running.

---

## Progress

| Phase | State |
|---|---|
| 3 — Read API | **Done.** `/articles`, `/articles/{id}`, `/countries`, `/categories`, `/languages`. Cursor pagination, permission gate on every path, rate limiting, `noindex`. |
| 4 — Permissions | **Done.** Restrictive defaults kept, `takedown_requested_at` added and enforced at both the read path and the fetch path. |
| 5 — Frontend | **Done.** Next.js on Vercel. Design arbitrated by `.claude/skills/design-council`. |
| 6 — Legal surface | **Mostly done.** About page carries non-affiliation, non-commercial framing, AI disclosure, takedown route and privacy note. Contact address live. |
| 7 — Hardening | **Partial.** Security headers and a clean `npm audit` (Next 16) shipped. Alerting, error tracking and uptime monitoring still open. |
| 8 — Ship | **Live end to end**, unlisted: https://clarityfeed-tirthdpatels-projects.vercel.app — frontend on Vercel, API on Render (Frankfurt, near the eu-central-1 database), ~9,700 articles served. |

**Open, in rough order of cost if ignored:**

1. **Render does not auto-deploy.** `autoDeploy` is enabled and has never
   fired; every deploy so far was manual. The backend silently sat six commits
   behind while Vercel kept deploying itself, which is how a working frontend
   ended up talking to an API that knew nothing about the filters it was
   sending. Most likely the GitHub webhook was not installed, because the
   service was created around the moment the repo flipped public. Until it is
   fixed, every backend change needs a manual deploy — and the failure mode is
   silent drift, not an error.

2. **Nothing watches ingestion.** It failed for four hours on 8 Sep and the
   only symptom was a site that stopped gaining articles. ROADMAP §7.

3. **Retention has not run yet at 10 days.** It will delete everything older
   than that on its next cycle — a large one-time drop from ~9,700.

4. **Enrichment produces nothing** (`summaries: 0`). Correct rather than
   broken: restrictive permissions mean no full text is stored, so there is
   nothing to summarise. It stays that way until a source is reviewed.

---

## 0. Where this starts

**Verified working:**

- Ingestion runs hourly in GitHub Actions (`ingest.yml`) and has succeeded
  repeatedly. The pipeline fetches, dedups, classifies, embeds, and crosses
  the publish barrier.
- The permission gate (`backend/permissions.py`) exists at both boundaries —
  ingest and serialize — and defaults restrictive.
- Compliance layer (`backend/compliance.py`): robots.txt fail-closed, per-domain
  rate limiting, conditional requests, attribution validation.
- Retention and the storage circuit breaker (`backend/retention.py`).
- Secrets hygiene: gitignored `.env`, pre-commit scanner, `validate_or_die()`
  fail-fast on production misconfiguration.
- The `/internal/collect` webhook and `INTERNAL_SECRET` are **deleted** —
  the shared-secret surface is gone rather than secured.

**Not built, despite what the architecture docs may imply:**

| Thing | State |
|---|---|
| Public read API | **Does not exist.** The API serves `/health`, `/ready`, `/sources`, `/stats/pipeline`. There is no endpoint that returns an article. |
| Frontend | **`frontend/src/` is empty.** Not scaffolded. Zero lines. |
| Clustering / ranking | Not built. No `clusters` table. |
| `source_permissions` rows | **Table is empty.** All 8+ sources resolve to `Permissions.restrictive()`. |
| Translations | Schema designed (V3 Part B), not populated. |
| Legal / policy pages | None. |
| Monitoring, alerting, error tracking | None. Nothing tells you ingestion stopped. |

**The honest summary:** the hard, interesting backend is done and running. The
entire user-facing half of the product — the part that makes it a website —
has not been started. Budget accordingly.

---

## 1. Deployment posture — unlisted, not public

Everything below is scaled to this decision, so make it deliberately.

### The recommendation: `noindex`, not a login wall

Use an unlisted URL (a `*.vercel.app` subdomain is fine) with search indexing
switched off. Do not build authentication.

A login wall is the wrong instrument here, because it works against the reason
you are deploying at all: a recruiter who hits a password prompt closes the
tab. What you want is **undiscoverable**, not **gated**. Those are different
properties and only one of them costs you the CV use case.

Concretely, about twenty minutes of work:

- `robots.txt` with `User-agent: * / Disallow: /`
- `<meta name="robots" content="noindex, nofollow">` on every page, plus the
  `X-Robots-Tag: noindex` response header (the header also covers API
  responses and anything the meta tag misses)
- No sitemap, no `hreflang`, no Open Graph tags that invite crawling
- Do not submit it anywhere: no Search Console, no Show HN, no Reddit, no
  Product Hunt
- Keep `FRONTEND_URL` set to the one real origin, which the CORS config
  already enforces

If you genuinely want a gate later, a shared password in Next.js middleware is
about twenty lines. Do not build accounts — accounts mean storing emails,
which *increases* your privacy surface rather than reducing it, for a demo
with twenty users.

### Why this works, stated honestly

Being unlisted does not make copying lawful. It makes it undiscovered, which
is a different thing, and it is worth being clear-eyed about the distinction.

What actually drives publisher complaints is discovery: their headlines
appearing in search results, or referral traffic they can measure in their own
analytics. Neither happens to a site with no index and no audience. And the
factors that matter if anyone ever *did* look — non-commercial, no ads, no
revenue, transformative, fully attributed, linking out, negligible traffic,
trivially quantified damages — all line up about as favourably as this
category of project can.

So the risk goes from "small but real and worth managing" to "essentially
theoretical," and the correct response is proportionate care rather than the
full public-launch apparatus in §6.

### The one thing that does *not* change

**You are still making requests to other people's servers.** Being unlisted
changes nothing about the crawl. The compliance layer — fail-closed
robots.txt, per-domain rate limiting, conditional requests, the honest
`ClarityFeedBot/1.0` user agent — stays exactly as it is, and it is already
correct.

This is also the one place where a realistic bad outcome exists, and it is not
a lawsuit: it is an abusive crawl pattern getting GitHub Actions IP ranges
blocked by a publisher's CDN. Those ranges are shared with every other project
on the platform. Stay polite because it is right, and because the blast radius
of not being polite is larger than your project.

---

## 2. The critical path

Only four things stand between here and a site a stranger can visit:

```
  Phase 3  Read API            ← nothing can render without it
     ↓
  Phase 5  Frontend            ← the bulk of remaining work
     ↓
  Phase 6  Legal surface       ← ships WITH the site, not after
     ↓
  Phase 7  Hardening → Phase 8 Ship → Phase 9 README
```

Phase 4 is half an hour and no longer gates anything (§4). Phase 6 is the one
with no compiler telling you it is incomplete, so it is the one that quietly
gets dropped — but at half a day, there is no version of "no time for it" that
is true.

---

## 3. Phase 3 — The read API (3–4 days)

The missing middle. Everything downstream is blocked on it.

**Endpoints:**

- `GET /articles` — cursor-paginated. Filters: `country`, `category`,
  `language`, `source`, `since`. Cursor, not offset: offset pagination over a
  feed that gains rows at the head shows duplicates and skips rows.
- `GET /articles/{id}` — single article.
- `GET /countries`, `GET /categories`, `GET /languages` — for the UI's filter
  controls; served from the seeded reference tables.

**Non-negotiables:**

1. **Every article path goes through `apply_serialize_gate()`.** Not "most".
   The gate is only load-bearing if it has no bypass. Add a test that asserts
   an unreviewed source's article never serializes a `content` field.
2. **Only `ingest_status = PUBLISHED`.** The publish barrier means nothing if
   the API reads around it.
3. **Attribution is mandatory in the response shape**, per V2 §11 — publisher
   name and original URL are non-nullable in the serializer, so an article
   physically cannot be returned without a route back to the source.
4. `Cache-Control: public, s-maxage=300, stale-while-revalidate=600` on every
   read (V2 §11). Render's free tier is slow; the CDN should absorb nearly all
   traffic.
5. Rate limiting on public endpoints. Currently there is none, and a single
   scraper can exhaust the free tier's connection pool.

**Deliberately deferred:** clustering, velocity ranking, `is_breaking`
(V3 Part E). Reverse-chronological with filters is a complete product. Build
the cluster model when there is traffic to justify it.

---

## 4. Phase 4 — Source permissions (30 minutes, not 2 days)

`source_permissions` is empty, so every source resolves to
`Permissions.restrictive()`: title, a 300-char description, a link,
attribution required. **Leave it that way and ship.**

For a public commercial launch this phase was a per-publisher legal review.
For an unlisted personal project it is one decision, already made correctly by
the schema defaults. The restrictive posture costs you nothing you actually
want — you were never going to render article bodies on your own domain anyway
(§5) — and it keeps the option of going public open without a data migration.

Two small things worth doing anyway:

1. **Add `sources.takedown_requested_at`** (V2 §11 calls for it; it does not
   exist yet). Five minutes, and it means a takedown is recorded state rather
   than something someone remembers doing.
2. **Write down that the default is deliberate**, in the README or the About
   page. "Headlines and short excerpts only, always linked to the original" is
   a sentence that reads as judgement to an engineer evaluating the project,
   and as good faith to a publisher who ever asks.

Skipped versus the public plan: the per-publisher ToS review, the EU Article 15
excerpt tightening, and dropping sources whose terms forbid aggregation. All
three come back in §11 if you go public.

---

## 5. Phase 5 — Frontend (7–10 days, realistically)

Next.js on Vercel, matching the `FRONTEND_URL` CORS configuration already in
`settings.py`. This is the largest remaining block of work.

**Ship-minimum pages:** home (latest), country, category, article card list,
about/methodology (which absorbs privacy and contact, per §6). No separate
terms page at this scale.

**There is no article *reader* page.** Clicking an article goes to the
publisher. This is a legal decision as much as a product one: the moment you
render article text on your own domain you are reproducing, not referring.

**Carry over from V3 Part F, which is right and cheap now:**

- **Accessibility (F1).** WCAG 2.2 AA. The *legal* driver — the European
  Accessibility Act, US ADA web claims — mostly falls away for an unlisted
  non-commercial site. Do it anyway: semantic landmarks, keyboard-navigable
  filters, visible focus, real contrast checks, `prefers-reduced-motion`. It is
  nearly free at build time, brutal as a retrofit, and it is one of the few
  things a senior engineer looking at your project will check in ten seconds.
- **Empty and error states (F5).** A country with no articles today, and a
  stale-data banner for when ingestion has not run. This is the most likely
  thing a real visitor sees in month one.
- **Time handling (F8).** Relative times rendered client-side from UTC.
- **Dark mode (F2).**

**AI labelling is mandatory, not optional** — see §5.5 below. Every generated
summary and every translated headline carries a visible machine-generated
notice and a link to the original.

**Defer:** i18n routing, RTL, the language switcher, search, admin panel.
Launch English-only. RTL is genuinely cheaper to build in from the start (V3
B6 is correct), but "cheaper later" beats "not launched." Ship, then add
Arabic as the first i18n proof.

---

## 6. Phase 6 — The legal surface, scaled to an unlisted site (half a day)

> Still not legal advice — but at this scale you do not need a lawyer, which
> was the single most expensive item on the public-launch plan.

This section was 2–3 days and a paid lawyer hour. Unlisted and non-commercial,
it collapses to about half a day. What survives, and why.

### Drops entirely

| Dropped | Why it was there |
|---|---|
| DMCA designated-agent registration | §512 safe harbour matters when you have volume and a public presence to protect |
| India IT Rules grievance officer | Intermediary obligations attach to a service offered to the public |
| Company formation | A liability shield protects against claims a non-commercial demo does not attract |
| GDPR Art. 27 EU representative | Requires *targeting* EU residents; an unlisted link targets nobody |
| Cookie consent banner | Moot with cookieless analytics, or none |
| Trademark clearance, WHOIS privacy | Brand protection for a brand you are not yet building |
| Formal ToS with venue and indemnity | Governs a commercial relationship with users you do not have |

### Keeps — all cheap, and most of it reads as judgement on a CV

**1. Attribution and outbound links.** Already enforced in the serializer and
non-negotiable regardless of audience. Publisher name and original URL on every
article. This is the whole ethical basis of an aggregator.

**2. AI labelling.** Keep in full. It was §5.5 of the public plan and it is the
item I would least want cut, for two independent reasons.

The legal one: an LLM summary that states a false fact about a named living
person is defamation, and unlisted is a weaker shield here than it is for
copyright — the subject of a false statement can be shown it by a single person
who read it. Small exposure, but not the same shape as the copyright one.

The other: **it is a hiring signal.** A visible "AI-generated summary — may
contain errors, read the original" beside every summary, with provenance logged
(model, prompt version, timestamp), tells anyone evaluating this project that
you understood you were building a publishing system and not just a pipeline.
That is rarer to demonstrate than the pipeline itself.

Keep: the visible label, the source link always beside the summary, short
factual summaries, provenance logging, suppression on low confidence. The
category-based suppression for crime and health is optional at this scale.

**3. Crawl politeness.** Unchanged, per §1.

**4. A short About / methodology page.** One page doing quadruple duty: it
explains the project to friends, it is legal cover, it is the takedown route,
and it is the first thing a technical reader looks for. State plainly what the
site does, that it is a personal non-commercial project, that summaries and
translations are machine-generated, how sources are selected, and how to ask
for something to be removed.

**5. A contact address you actually monitor.** This is the entire takedown
process at this scale. A publisher who emails and gets a same-day "removed,
sorry" has no reason to escalate, and that has always been what prevents
disputes — not the policy text.

**6. A privacy note, one or two paragraphs.** Short, because you collect almost
nothing: what is collected (server logs, analytics if any), who processes it
(Vercel, Render, Supabase, GitHub, Groq), that article text is sent to Groq for
summarisation, and how to contact you. **Use cookieless analytics or none** —
Plausible, Umami, or Vercel Analytics. Still the highest-leverage decision in
the section: it is the difference between two paragraphs and a consent
management platform.

**7. `noindex` everywhere**, per §1. This is the load-bearing control that makes
every other reduction here reasonable.

### The one addition

**State on the About page that the site is independent and non-commercial.**

This matters because "non-commercial" is the framing under which every
reduction in this section is defensible — it is doing real work, not
decorating.

But it should read as a real product's About page, not as a disclaimer that
this is a student demo. Do not use the words "portfolio", "personal project",
"demo", or anything about a CV, anywhere on the site. Real news services carry
this exact language for exactly these reasons:

> ClarityFeed is an independent news aggregator. It is not affiliated with, or
> endorsed by, any of the publishers it links to, and it is operated on a
> non-commercial basis. Headlines and short excerpts are shown under
> attribution; full articles remain on the publisher's own site. Publishers
> who would like their content removed can write to <address> and it will be
> taken down.

That is four sentences, it is entirely true, it reads as confidence rather than
apology, and it carries the non-affiliation disclaimer, the non-commercial
framing, the attribution posture and the takedown route at once. The footer
needs nothing beyond a link to it.

---

## 7. Phase 7 — Hardening (1–2 days)

Currently, if ingestion stops, nothing tells you. That is the gap that matters
most.

- **Alerting on ingestion failure.** The `ingest` workflow should notify on
  failure, and something should notice "no successful run in 3 hours" — a
  workflow that stops being scheduled fails silently, which is exactly the
  failure `ingest.yml`'s keep-alive job exists to prevent.
- **Error tracking** (Sentry free tier) on the API and the frontend.
- **Uptime monitoring** on `/ready`.
- **Verify the backup story.** Confirm what Supabase's tier actually retains
  and how a restore works — test it once, on purpose.
- **Security headers**: HSTS, CSP, `X-Content-Type-Options`, referrer policy.
  Enable Dependabot.
- **`robots.txt` disallow-all and `noindex`**, per §1. No sitemap — a sitemap
  is an invitation to crawl and is the opposite of what you want here.
- **Then** the deferred database work from HANDOFF §3.5: move dedup into the
  database, build the HNSW index in the same change, drop `vector_json` after
  verification. Not before — an index no query uses is pure write cost.

---

## 8. Phase 8 — Ship it

Deploy, check it over a few days of real ingestion, then send the link around.
There is no soft-launch/publicise distinction any more — unlisted *is* the
launch.

Pre-flight, all no-go items:

- [ ] `noindex` meta tag, `X-Robots-Tag` header, and `robots.txt` disallow-all
      are live — verify by fetching the deployed URL, not by trusting the config
- [ ] AI summaries are visibly labelled and always shown beside a source link
- [ ] Every article carries publisher name and original URL
- [ ] About page live, linked from every page: independent, non-affiliated,
      non-commercial, with a contact address for removals (§6). No "portfolio",
      "demo" or CV framing anywhere on the site.
- [ ] That address is one you actually read
- [ ] Cookieless analytics, or none
- [ ] `APP_ENV=production`, `validate_or_die()` passes on the real deploy,
      `FRONTEND_URL` set to the one real origin
- [ ] Alerting fires when ingestion stops (test it by breaking it on purpose)

Dropped from the public checklist: per-source permission review, privacy policy
and terms as separate documents, entity formation, monitored takedown *process*
(the address is enough at this scale).

**Cost: $0.** Vercel, Render, Supabase and Actions free tiers all cover this,
and Actions minutes are unlimited because the repo is public.

**One thing to check:** Render's free tier sleeps after 15 minutes, and a cold
start is ~50 seconds. That is a terrible first impression for someone clicking a
link on your CV. With Next.js ISR and the `s-maxage=300` headers from §3, cold
starts land on revalidation rather than on readers — but verify that in practice
before you send the link out, because getting it wrong is invisible to you and
fatal to the reader.

---

## 9. Using it on your CV

Worth being deliberate about, because the deployed site is probably **not** the
strongest artifact here.

**What is actually impressive in this project**, roughly in order:

1. **The publish barrier.** Splitting ingestion into "must succeed to be
   visible" and "allowed to fail silently" is a real distributed-systems
   instinct, and it was a fix for an actual outage where an LLM failure emptied
   the site. That story — symptom, diagnosis, architectural fix — is the best
   thing you have to talk about in an interview.
2. **The permission gate at two boundaries.** Enforcing at ingest *and*
   serialize, with restrictive defaults, because permissions change and a
   publisher who revokes consent must stop being reproduced immediately. That is
   a judgement most engineers do not make until someone forces them to.
3. **KEDA autoscaling driven by a real queue-depth metric**, with
   `/stats/pipeline` exposing the same number the scaler queries.
4. **Fail-closed robots.txt and an honest user agent**, with the reasoning
   written down: a crawler hiding behind a browser UA cannot be blocked by a
   publisher who wants to block it, which makes the robots check theatre.
5. **The architecture docs themselves.** V2 and V3, with numbered findings and
   amendments that supersede earlier decisions, are genuinely unusual for a
   personal project. Most reviewers will read those before they read code.

**Therefore:**

- **The README is the highest-leverage thing you can write**, and it does not
  exist in a form that carries any of the above. Most people will never click
  the link. Lead with the architecture diagram, the publish barrier, and the
  permission model. Screenshots inline, so it works for someone who will not
  deploy anything.
- **Consider a public status/architecture page** on the site — the live
  `/stats/pipeline` numbers, the pipeline diagram, ingestion run history. Unlike
  the article feed, that page reproduces nobody's content, so it can be indexed
  and linked freely. It is also more impressive than the feed: the feed looks
  like every other news site, and the pipeline is where the work is.
- **Write up one incident.** The `NameError` on every `/internal/collect`
  success (HANDOFF §68) or the false-positive history scan are both good: a
  short post on how it was found and what changed is worth more than another
  feature.
- **Keep the repo public.** It is load-bearing for free Actions minutes anyway,
  and the commit history — which shows hardening, reversals, and deleting a
  shared-secret surface rather than securing it — is itself the argument.

---

## 10. Estimate

| Phase | Work |
|---|---|
| 3 — Read API | 3–4 days |
| 4 — Permissions | 30 minutes |
| 5 — Frontend | 7–10 days |
| 6 — Legal surface | half a day |
| 7 — Hardening | 1–2 days |
| 8 — Ship | 1 day |
| 9 — README and writeup | 1 day, and do not skip it |

**≈ 14–19 working days**, or 4–6 calendar weeks solo. The public-launch version
of this plan was 18–25 days; nearly all of the saving is legal and
organisational work that an unlisted non-commercial project does not need, not
engineering.

The frontend is still the bulk and still the thing most likely to overrun,
because it is the part that has not been started at all.

---

## 11. If you later go public

Nothing here is a dead end — the restrictive permission defaults, attribution,
AI labelling and crawl politeness are all the public posture already. Going
public means adding back, in roughly this order:

1. Remove `noindex`, add the sitemap and `hreflang` — **last**, not first
2. Per-publisher `source_permissions` review with `reviewed_at` set, and drop
   sources whose terms forbid aggregation
3. Shorten the excerpt default from 300 to ~200 chars for EU Article 15
4. Privacy policy and terms as real documents; a documented 24-hour takedown
   process with a runbook
5. DMCA designated agent; India IT Rules grievance officer if incorporated there
6. Form the entity **before** any of the above, not after
7. One hour with a media/IP lawyer

The order matters: (1) is what makes you discoverable, and it should be the last
thing you do rather than the first.
