---
name: design-council
description: Run installed design skills as opposing critics against a UI, then arbitrate their conflicts into one coherent decision. Use when designing or redesigning a frontend, when a design "looks generic/AI-made", or when several design skills give contradictory advice.
---

# Design Council

Multiple design skills are installed and **they contradict each other**. Loading
all of them at once produces mush: every rule is followed weakly, the conflicts
are never named, and the result is the average of several opinions — which is
the definition of a generic design.

This skill runs them as adversaries instead, then arbitrates.

## The council

Read only what is installed; skip silently what is not.

| Seat | Source | Brief |
|---|---|---|
| **Taste** | `.agents/skills/design-taste-frontend*` | Hunt AI tells and cliché. Opinionated, maximalist. |
| **Studio** | `~/.claude/skills/frontend-design` (Anthropic) | Distinctiveness and restraint. Is this specific to *this* brief? |
| **Guidelines** | `.agents/skills/web-design-guidelines` (Vercel) | Accessibility, performance, correctness. |
| **Patterns** | `~/.claude/skills/frontend-patterns` | Implementation convention. |

Seats not installed do not vote. Never invent a seat's opinion — quote the rule
you are actually applying, from the file.

## Procedure

**1. Each seat critiques independently.** Before reconciling anything, produce
each seat's findings in its own voice, against the *actual current code*. A seat
that finds nothing wrong has not looked hard enough — the first pass on any
generated UI should find real hits, because the tells are statistical and you
are the statistic.

**2. Name the conflicts explicitly.** This is the part that has value; do not
smooth it over. Known live conflicts between the installed seats:

- **Serif type.** Taste bans serif on software UI, permits it for editorial.
  Studio flags "broadsheet with hairline rules" as a generated-page tell. Both
  can be right; the resolution is never "use serif timidly".
- **All-caps labels.** Taste's structured-label habit produces them. Studio
  bans them outright as template chrome. Studio wins — see tie-breaks.
- **Motion.** Taste's default `MOTION_INTENSITY: 6` wants perpetual
  micro-animation. Studio says non-user-triggered motion is a tell, and
  per-card hover transitions read as generated. Guidelines adds
  `prefers-reduced-motion`.
- **Emoji.** Taste bans emoji outright. Product need may want them (flags for
  countries). Note the *technical* argument separately from the taste one:
  flag emoji do not render as flags on Windows at all, which decides it on
  correctness rather than opinion.
- **Framework.** Taste assumes Tailwind and Framer Motion. If the project uses
  neither, its concrete class names are inapplicable — take the principle,
  discard the syntax. Never add a dependency to satisfy a style rule.

**3. Arbitrate with fixed tie-breaks.** In descending order, non-negotiable:

1. **Legal and safety constraints outrank all aesthetics.** In this project
   that means: AI-generated content stays visibly labelled, attribution and the
   outbound link stay present and prominent. No seat may vote these away, and
   "the label is visually noisy" is not a finding — it is the point.
2. **Accessibility outranks taste.** Contrast, focus visibility, target size,
   semantics, reduced motion. A design that fails these is not a bolder
   design, it is a broken one.
3. **Correctness outranks taste.** Cross-platform rendering, layout stability,
   performance.
4. **Specificity outranks polish.** Between two accessible options, take the
   one that could only belong to *this* product. A safe design that could be
   any product has already failed.
5. **Subtraction outranks addition.** When the council deadlocks, remove the
   element rather than compromise on it.

**4. Decide, and record the reasoning.** Output a short decision log: the
conflict, who said what, what was chosen, why. Then implement. A decision
nobody wrote down gets re-litigated by the next pass.

**5. Look at it.** Take a screenshot and critique the render, not the code.
Check both colour schemes and a narrow viewport. Most tells are visible and
invisible in source.

## Anti-averaging rule

If the output of this process is "apply every rule a bit", the process failed.
Spend boldness in exactly one place, make that one thing specific to the
subject, and let the council's restraint rules govern everything around it.
