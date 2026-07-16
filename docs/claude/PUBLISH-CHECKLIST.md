# Publish checklist (generic, portable)

Run this when the owner says "publish" / "push" / "ship" / "release". This is the clean
template — copy it into each project's vault (`docs/claude/PUBLISH-CHECKLIST.md`) and append
the project's specifics as you go.

> Part of the unified process — see [`PROTOCOL.md`](PROTOCOL.md). Every ship/push also runs
> the **"ship it qa" + "mr robot"** step below.

## "ship it qa" + "mr robot" — every ship gets QA *and* an adversarial pass
When the owner says **"push it live" / "ship it qa" / "ship" / "push" / "merge it" /
"mr robot"**, the ship is **NOT done** until BOTH have run on the change — before it goes
live or right after, owner's call:

1. **QA pass** — run the **active platform's testing regimen** (web / Next.js · Swift / iOS ·
   Apple App Store · Android — see `PROTOCOL.md` → *Platform testing regimens*) and exercise
   the changed surface end-to-end on the real target (golden path + one edge case).
2. **"mr robot" adversarial pass** — hostile-researcher, READ-ONLY security review of exactly
   what changed; threat-model first on cost/abuse (paid APIs) + auth/data exposure. Fix real
   findings, file Low/by-design ones as issues, surface the verdict.

Both are required even when the change already merged. Report the QA result + the mr-robot
verdict before calling it shipped.

## Sync with every user-facing change
- [ ] Copy inventory — any on-screen copy change.
- [ ] README — feature list, limits, what-it-does, setup/env docs.
- [ ] Help / FAQ — for new or changed flows.
- [ ] SEO / AEO surfaces — sitemap, `llms.txt` / `llms-full.txt`, structured data — if the
      project has them.
- [ ] Changelog — an entry for significant copy/feature moves.
- [ ] `STATUS.md` — in-flight state.

## Canonical facts must stay consistent everywhere
Pull these from the project's *Project canonical facts* block (in `PROTOCOL.md`) and verify
they match across copy, README, structured data, and changelog:
- [ ] Names / brand / domain(s).
- [ ] Pricing / plans / limits.
- [ ] Key terms (capitalization, proper nouns).

## Pre-merge gates
- [ ] The active **platform testing regimen** passes (type-check / build / tests / lint).
- [ ] QA the changed surface (golden path + one edge case) on the real target.
- [ ] PR opened as **draft**.
- [ ] Build / deploy confirmed green before merge.
- [ ] Version bumped.

## Project-specific publish steps (append per project)
> Add anything this project needs at publish time — e.g. store submission, cache purges,
> vendor-dashboard steps, announcement emails, external-rebrand cutovers.
