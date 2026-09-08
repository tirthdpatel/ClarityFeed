# ClarityFeed — roadmap to launch

**Status: 8 Sep 2026.** This is the execution plan from today's *verified*
state to a deployed public website. It supersedes ARCHITECTURE_V3 Part G as
the working plan; V2 and V3 remain the design reference for *how* each piece
should behave.

The difference between this document and Part G is that Part G was written
before Phase 2 landed and describes intended scope. This one starts from what
is actually in the repository and running.

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

## 1. The critical path

Only four things stand between here and a site a stranger can visit:

```
  Phase 3  Read API            ← nothing can render without it
     ↓
  Phase 4  Permission review   ← human/legal, runs in parallel with 3
     ↓
  Phase 5  Frontend            ← the bulk of remaining work
     ↓
  Phase 6  Legal surface       ← must ship WITH the site, not after
     ↓
  Phase 7  Hardening → Phase 8 Launch
```

Phases 4 and 6 are not engineering and will be the ones that slip, because
they are the ones with no compiler telling you they are incomplete. Start
Phase 4 now — it is calendar time waiting on publishers, not work time.

---

## 2. Phase 3 — The read API (3–4 days)

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

## 3. Phase 4 — Source permission review (calendar time, start now)

This is the legal core and it is a human judgement, deliberately not
automated. `source_permissions` is empty, so every source is restrictive:
title, 300-char description, link. **That is the correct and safe posture and
you can launch on it.**

The review is about deciding, per publisher, whether to claim anything more.

**Recommendation: for the launch set, claim nothing more.**

BBC, Al Jazeera, DW, France 24, NPR, Guardian, SCMP, Times of India, Hindu,
NDTV, CBC, ABC Australia, Japan Times, CNA, G1, News24 — these are exactly the
publishers with legal departments and a history of pursuing aggregators. The
value of storing their full text is small; the downside is a letter. Headline
+ short excerpt + prominent link is the posture that aggregators have
successfully defended, and it is what you already have by default.

**Per source, do this and record it:**

1. Read the RSS terms and site ToS. Many explicitly permit personal/non-commercial
   feed use and forbid redistribution — that distinction matters.
2. Write a `source_permissions` row with `reviewed_at` set and a note on what
   the terms said. An unset `reviewed_at` should stay unset rather than being
   filled in optimistically; the empty state is safer than a wrong one.
3. If terms forbid aggregation outright, set `is_active = false` and drop the
   source. Losing one feed is cheaper than defending one.

**One schema addition** (V2 §11 calls for it, it does not exist yet):
`sources.takedown_requested_at`, so a takedown is recorded state rather than a
thing someone remembers doing.

**Tighten the excerpt before launch.** `max_description_chars = 300` predates
the EU question. The DSM Directive Article 15 press publishers' right exempts
"individual words or very short extracts," and 300 characters is not obviously
short in every member state. With EU sources you will have EU readers.
**Reduce the default to ~200 characters, or serve headline + link only for EU
publishers.** This costs nothing now and is expensive to retrofit.

---

## 4. Phase 5 — Frontend (8–12 days, realistically)

Next.js on Vercel, matching the `FRONTEND_URL` CORS configuration already in
`settings.py`. This is the largest remaining block of work.

**Ship-minimum pages:** home (latest), country, category, article card list,
about/methodology, privacy, terms, contact/takedown.

**There is no article *reader* page.** Clicking an article goes to the
publisher. This is a legal decision as much as a product one: the moment you
render article text on your own domain you are reproducing, not referring.

**Carry over from V3 Part F, which is right and cheap now:**

- **Accessibility (F1).** WCAG 2.2 AA. The European Accessibility Act has been
  enforceable since June 2025, and US ADA web claims are a live litigation
  category. Semantic landmarks, keyboard-navigable filters, visible focus,
  real contrast checks, `prefers-reduced-motion`. Free at build time,
  expensive as a retrofit, and a genuine legal exposure — not polish.
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

## 5. Phase 6 — The legal and policy surface

> **I am not a lawyer and this is not legal advice.** This is an engineer's
> checklist of the exposures a news aggregator has, so that you know what to
> ask a real lawyer about. For an aggregator republishing foreign press across
> jurisdictions, one paid hour of a media/IP lawyer's time before launch is the
> best money in this budget.

You are in **India**, so the DPDP Act 2023 is your home privacy regime. Your
sources — and therefore your readers — are EU, US, UK, CA, AU, IN, BR, ZA, SG,
JP. Assume GDPR applies to EU readers.

### 5.1 Copyright — the largest exposure

The whole product is other people's journalism. Everything else on this list is
secondary.

- **Keep the restrictive default.** Headline, short excerpt, link, attribution.
  This is the defensible line.
- **Never store or serve images.** `can_store_image = false` is already the
  default — keep it. Press photos are separately licensed (AP/Reuters/Getty)
  and are the single most commonly enforced asset in this space. Hotlinking is
  not a workaround; it is the same reproduction plus a bandwidth complaint.
- **Summaries must summarise facts, not track expression.** Facts are not
  copyrightable; a close paraphrase that follows the original's structure and
  phrasing is a derivative work. This is a prompt-design constraint, and it
  should be written into the prompt explicitly and version-controlled.
- **Translation is a derivative work** (V3 B8). The posture there —
  transformative, attributed, linked, one flag to opt out — is sound. Keep the
  visible machine-translation notice.
- **EU: DSM Article 15.** Press publishers hold a neighbouring right in
  extracts. See §3 on shortening the excerpt.
- **Keep respecting robots.txt** and keep the honest user agent
  (`ClarityFeedBot/1.0`). A crawler that identifies itself can be blocked by a
  publisher who wants to block it — which is exactly what makes the robots
  check meaningful rather than theatre. This is already correct.

### 5.2 Takedown — the thing that actually prevents lawsuits

Most publisher disputes end at the first email if the answer is fast and the
content disappears. They escalate when nobody responds.

- A `/contact` page with a **monitored** address, findable from every page
  footer, that explicitly names publisher takedown as a purpose.
- A documented 24-hour process: disable the source, purge its articles, record
  `takedown_requested_at`. Write the runbook now; you will not compose it well
  under a legal threat.
- **US DMCA:** register a designated agent with the US Copyright Office
  (dmca.copyright.gov, nominal fee, renewed every 3 years) and publish a
  notice-and-takedown policy with a repeat-infringer policy. Caveat: §512 safe
  harbour is designed for user-uploaded content, and an aggregator that selects
  its own sources is a weaker fit. Register anyway — it is cheap and signals
  good faith.
- **India:** IT Rules 2021 intermediary obligations, including a published
  grievance officer with a name, address, and a 24-hour acknowledgement /
  15-day resolution window. If you incorporate in India, this is a
  requirement, not an option.

### 5.3 Privacy

- **Publish a privacy policy before launch**, covering: who the controller is
  (a real name and contact — the DPDP Act and GDPR both require identity),
  what is collected, the legal basis, processors (Vercel, Render, Supabase,
  Groq, GitHub, and any analytics), retention, international transfers, and
  how to exercise rights.
- **Use cookieless analytics.** Plausible, Umami, or Vercel Analytics. This is
  the single highest-leverage compliance decision available to you: no
  non-essential cookies means no consent banner, no CMP, no consent logging,
  and a dramatically smaller surface. Google Analytics reverses all of that
  and has been found unlawful by several EU DPAs on transfer grounds.
- **Do not add accounts, comments, or a newsletter for launch.** Each adds a
  category of personal data and a body of law. A newsletter alone brings
  CAN-SPAM, GDPR consent and double opt-in, and a suppression-list obligation.
- **Note what already leaves your systems:** article text is sent to Groq for
  summarisation. That is third-party processing and belongs in the policy and
  in a processor list, even though no personal data of *readers* is involved.
- **EU representative (GDPR Art. 27)** is required if you *target* EU
  residents. An English-language global news site probably does not target
  them; the moment you ship country pages for EU countries and EU-language
  editions, that argument weakens considerably. Revisit at i18n, not before.
- **Breach plan:** GDPR gives you 72 hours to notify. Write the plan while
  calm.

### 5.4 Terms of service

Straightforward, but it must exist and must be linked from the footer:
"as is" with no warranty, an explicit accuracy disclaimer covering
machine-generated summaries and classifications, limitation of liability,
acceptable use (no scraping of *your* site), the right to modify or
discontinue, and governing law and venue in your own jurisdiction. That last
clause is why a template is worth adapting rather than copying.

### 5.5 AI output — the sharpest under-rated risk

An LLM summary that states a false fact about a named living person is
defamation, and "the model produced it" is not a defence anyone has
successfully run. This is the risk in this product most likely to be
underestimated, because the pipeline treats summaries as a quality feature
rather than a publishing act.

Mitigations, in order of value:

1. **Label every generated summary visibly**, next to the text, not in a
   footer. "AI-generated summary — may contain errors. Read the original."
   This is honest, it is legally useful, and EU AI Act Article 50 transparency
   obligations for AI-generated content phase in from August 2026 anyway.
2. **Always show the source link beside the summary**, never in place of it —
   V2 §11 already specifies this. It gives the reader a correction path and
   makes the summary plainly secondary to the original.
3. **Keep summaries short and factual.** Long summaries invent; short ones have
   less room to.
4. **Suppress summaries where confidence is low**, and consider suppressing
   them entirely for crime, legal proceedings, and health. Getting a crime
   story's subject wrong is the canonical defamation scenario.
5. **Log provenance for every summary**: model, prompt version, timestamp,
   source article. `summaries.model_name` exists; add prompt version. When
   someone complains, you need to be able to say exactly what produced it.
6. **A visible corrections mechanism**, per V3 F3. Fixing it fast is most of
   the defence.

### 5.6 Entity, brand, and the boring protections

- **Form a company before public launch.** A liability shield is a larger risk
  reduction than every policy page combined, and none of the above protects
  personal assets without it.
- **Clear the name.** Search for conflicting "ClarityFeed" marks before
  investing in the brand.
- **Use plain-text publisher names, never their logos.** Naming a source is
  nominative use; reproducing a masthead suggests endorsement and adds a
  trademark claim to a copyright one.
- **Domain privacy on WHOIS**, so a complaint arrives as an email rather than
  at your home address.

---

## 6. Phase 7 — Pre-launch hardening (2–3 days)

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
  A `security.txt`. Enable Dependabot.
- **Own `robots.txt` and a sitemap** for your site.
- **Then** the deferred database work from HANDOFF §3.5: move dedup into the
  database, build the HNSW index in the same change, drop `vector_json` after
  verification. Not before — an index no query uses is pure write cost.

---

## 7. Phase 8 — Launch

Soft launch first: deploy, leave it unlisted, watch a week of real ingestion
and real cache behaviour. Then publicise.

The pre-flight checklist is short and every item is a no-go:

- [ ] Every source has a reviewed `source_permissions` row, or is inactive
- [ ] Privacy policy, terms, about/methodology, and contact/takedown are live
      and linked from every page
- [ ] AI summaries are visibly labelled and always shown beside a source link
- [ ] Takedown address is monitored and the runbook is written
- [ ] Cookieless analytics, or a consent banner that actually blocks
- [ ] Accessibility pass done
- [ ] Alerting fires when ingestion stops (test it by breaking it on purpose)
- [ ] Entity formed
- [ ] `APP_ENV=production` and `validate_or_die()` passes on the real deploy

---

## 8. Estimate

| Phase | Work |
|---|---|
| 3 — Read API | 3–4 days |
| 4 — Permission review | 2 days work, longer in calendar |
| 5 — Frontend | 8–12 days |
| 6 — Legal surface | 2–3 days, plus a lawyer hour |
| 7 — Hardening | 2–3 days |
| 8 — Launch | 1 day + a soft-launch week |

**≈ 18–25 working days**, which for one developer is 6–9 calendar weeks. V3's
Part G said 18–23 days for a comparable remainder and did not include the legal
surface; that estimate was optimistic in the way all such estimates are.

## 9. What to cut if it slips

In this order: search, i18n and RTL, clustering and breaking-news ranking,
trending, the admin panel, dark mode. All of them are additions to a working
site. Nothing on the critical path or in §5 is a candidate — a site that
launches without a privacy policy or without AI labelling is not a smaller
launch, it is a different and worse kind of risk.
