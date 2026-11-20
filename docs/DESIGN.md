# Design direction

Written before the redesign of 20 Nov 2026; every visual decision in `src/radar/static/app.css`
and the templates traces back to one line here.

## 1. Concept
A *consulente del lavoro* opens a company and in ten seconds knows three things: how much the hires
could be worth, **what is still missing to confirm it**, and which line of which INPS act every
figure comes from. The scale simulation (`docs/simulation.json`) showed that almost every proposal
is "to check", so the company page is a **work queue**, not a showcase of amounts.

## 2. Motif — "the source thread"
Text quoted from INPS is set in a serif (IBM Plex Serif), upright, behind a 2px accent rule, and
ends with a monospace paragraph tag (`§4.p2`). The app's own words are sans-serif. Wherever the
reader sees serif and a `§` tag, they are reading the law, not the software. It appears on the
incentive table, every eligibility check, every Q&A citation and the queue.

Second, smaller motif: the **criteria meter** — one thin segmented bar per proposal
(graphite = met, amber = to check, red = not met). It replaces adjectives with a count.

## 3. Mood
Rigorous, not bureaucratic. Calm, not dull. Dense, not crowded. It is a working register for a
professional who spends hours in it; it is **not** a government portal and **not** a startup
landing page.

## 4. Principles
- Type: Geist (UI, 400/500/600, tabular figures everywhere), IBM Plex Serif (quotes only),
  Geist Mono (anchors, IDs). All OFL, self-hosted (the CSP allows no third-party origin).
- Colour: OKLCH graphite scale with hue 260 and near-zero chroma; **one** accent (ink blue,
  `oklch(0.50 0.19 262)`) reserved for actions, links, focus and the source thread. Status colours
  are semantic only: amber = to check, red = not met. "Met" is graphite, not green: a pass is the
  normal state and should not shout.
- Space: 4px grid; rows 16px, cards 24px, sections 48px. Max reading width 72ch.
- Motion: 160ms, `cubic-bezier(.2,0,0,1)`, only on hover/focus/open; nothing moves on load.
  `prefers-reduced-motion` turns it off.
- Numbers: euro amounts in Italian format, tabular, the euro sign at 70% size.

## 5. Consistency
Radii 10 (cards) / 8 (controls) / 999 (chips). One hairline (`color-mix` of the ink at 10%).
Two shadows only (`--sh-1` resting, `--sh-2` raised). One icon family (Lucide, stroke 2, 16 or 20px).
Every status is said in words as well as colour (CVD-safe, prints in grey).

## 6. Craft detail
The hero figure on a company page is split: the part already confirmed and the part still to
check, on one bar, with the count of open items next to it. The `§` tag is optically lowered to
the serif's x-height. Light and dark themes are designed, not inverted.
