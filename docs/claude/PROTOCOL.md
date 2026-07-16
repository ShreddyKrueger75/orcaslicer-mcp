# PROTOCOL — how every project runs (generic, portable)

This is the **portable control doc**. It is project-agnostic. Keep the clean copy in
your global Claude memory (`~/.claude/`) and **copy it into each project's vault** at the
start of that project (see *Bootstrapping a new project* below). Read it at the start of
**every project**, every time: **Bootstrap → Design → Development → Ship (QA + mr robot) →
Publish**, plus the rules that always apply.

---

## Bootstrapping a new project — DO THIS FIRST on any new project
The global copy in `~/.claude/` is the clean template. Each project gets its **own copy**
it can grow. On starting work in a new project repo:

1. **Create the project's vault** if it doesn't exist: `docs/claude/` (or the project's
   docs equivalent).
2. **Copy the templates in** from `~/.claude/`:
   - `~/.claude/PROTOCOL.md` → `docs/claude/PROTOCOL.md`
   - `~/.claude/PUBLISH-CHECKLIST.md` → `docs/claude/PUBLISH-CHECKLIST.md`
3. **Wire them into the project so Claude auto-loads them every session** — add `@`-imports
   to the project's `CLAUDE.md` (create `CLAUDE.md` at the repo root if missing):
   ```
   @docs/claude/PROTOCOL.md
   @docs/claude/PUBLISH-CHECKLIST.md
   @docs/claude/STATUS.md
   ```
4. **Create `docs/claude/STATUS.md`** — the living "where we left off" for this project.
5. **Fill in the project specifics** at the bottom of the project's *copy* (NOT the global
   template): the *Project canonical facts* block below, the stack, deploy target, and which
   *Platform testing regimen* applies.

**Two layers, on purpose:**
- The **global** `~/.claude/` copies stay **clean and generic** — the reusable template.
- The **per-project** copies are **living docs**: append project decisions, conventions,
  canonical facts, and learnings to them **as the project evolves** (append-only spirit —
  add, don't gut). Keep `STATUS.md` current as you work.

---

## Trigger phrases — what the owner says → what you do
| The owner says… | You run… |
| --- | --- |
| **"start a project"** / first work in a new repo | **Bootstrap** — copy the templates in, wire up `CLAUDE.md`, create `STATUS.md`, fill in specifics. |
| **"let's design …"** / any UI / visual change | **§1 Design** — offer a mockup first, build only after a direction is picked. |
| **"let's build …"** / a new feature, fix, refactor | **§0 Start** + **§2 Development** — branch, scope-confirm, smallest change, diff by diff, draft PR. |
| **"push it live" / "ship it qa" / "ship" / "push" / "merge it" / "mr robot"** | **§3 Ship** — merge/deploy **and run the full test suite: a QA pass AND a "mr robot" adversarial pass.** Not done until both have run and the verdict is reported. |
| **"publish" / "release"** | **§4 Publish** — the publish checklist **plus §3's QA + mr-robot**. |

No reminders needed — when you hear the phrase, you already know the protocol.

---

## 0. Start of every task (always, before anything else)
1. Report `git status` + current branch. **If not on the default branch, or the tree isn't
   clean → STOP and ask.**
2. Pull the default branch. Report any new commits.
3. Branch: `claude/<short-descriptive-topic>` — the topic names the **change**, not the
   action. Confirm the name if scope is ambiguous.
4. **Before writing code:** summarize the task back in 1–2 sentences + list the files you
   expect to touch. **Wait for confirmation** that the scope is right.

> Override: if the owner says "skip the workflow" / "do this on the current branch" / is in
> rapid live-iteration mode, honor that. Otherwise this is the default.

---

## 1. Design process — mockup-first
For any **UI / visual change** (layout, components, styling, new screens): **ask whether the
owner wants a mockup first.** If yes, render it (matching the surface's real aesthetic) and
show it; **build only after a direction is picked.** Code-only changes with no visual surface
are exempt.

---

## 2. Development process — smallest change, diff by diff
5. Make the **smallest change** that does the job. After each meaningful change, **show the
   diff and wait** for confirmation before continuing.
6. When it works + is tested: commit (descriptive message), push the branch (triggers the
   preview build if the platform has one). Open the PR as **draft**.
7. While the owner reviews, **don't start a new task on that branch.** Issues → more commits
   to the same branch.
8. After the owner confirms merge: return to the default branch, pull, delete the topic
   branch, prune. Then stop.

---

## 3. Ship — "ship it qa" + "mr robot" (QA *and* an adversarial pass, every time)
When the owner says any ship trigger (**"push it live" / "ship it qa" / "ship" / "push" /
"merge it" / "mr robot"**), the ship is **NOT done** until BOTH have run on the change —
before it goes live or right after, owner's call:

1. **QA pass** — run the **active platform's testing regimen** (see *Platform testing
   regimens*) AND exercise the changed surface end-to-end on the **real target** (deploy /
   simulator / device, not just a clean compile): golden path + at least one edge case.
2. **"mr robot" adversarial pass** — a hostile-researcher, **READ-ONLY** security review of
   exactly what changed, threat-model first on **cost / abuse** (paid APIs, third-party
   spend) and on **auth / data exposure**. Fix real findings; file Low / by-design ones as
   issues; surface the verdict.

"ship it qa" **requires** both — it never skips them, even when the change already merged.
**Report the QA result + the mr-robot verdict before calling it shipped.**

---

## 4. Publish process — "publish" / "release"
Run the project's [`PUBLISH-CHECKLIST.md`](../PUBLISH-CHECKLIST.md). In short:
- **Bump the version** the project uses.
- **Sync every user-facing change** across the project's surfaces: copy inventory · README ·
  help / FAQ · SEO surfaces (sitemap, llms.txt, structured data) · changelog · `STATUS.md`.
- **Keep the project's canonical facts consistent everywhere** (see *Project canonical
  facts*).
- **Pre-merge gates:** the platform regimen passes · PR opened as **draft** · confirm the
  build / deploy is green before merge.
- Then run **§3 (QA + mr robot)**.

---

## Platform testing regimens — QA adapts to the stack
The QA pass in §3 and the gates in §4 run **the regimen for the platform being worked on**.
Use whichever applies; add a new platform's regimen as projects appear.

**Web / Next.js / TypeScript:**
- Type-check clean (`tsc --noEmit`).
- Lint clean.
- Production build exits 0 (`next build`).
- Any boundary / project gate scripts (e.g. server/client boundary).
- Unit tests present (`npm test`).
- QA on the **deployed preview**, not just locally, when the app needs services a local run
  can't reach.

**Swift / iOS / macOS (native):**
- `xcodebuild build` (or `swift build`) clean.
- `xcodebuild test` (XCTest / Swift Testing) green on a simulator.
- `swiftlint` / `swift-format` clean.
- Run on at least one simulator; ideally smoke-test on a real device.

**Apple App Store app (release):** everything in Swift/iOS, **plus**:
- `xcodebuild archive` + Organizer **validate** the archive.
- **TestFlight** internal build + on-device smoke test.
- App Review prep: privacy nutrition labels, ATT + permission-usage strings, required
  screenshots, a demo account / video where a feature needs one, no private-API usage.
- Bump marketing version + build number.

**Android / Play Store (release):** unit tests (`./gradlew test`) + instrumented tests green;
lint / ktlint clean; signed release build + internal testing track; Play Console data-safety
form current; versionCode / versionName bumped.

---

## Cross-cutting rules (always apply)
- **Minimum change.** No unasked "improvements," no refactor-while-fixing. Anything in shared
  code (shared libs, constants, multi-use components) = treat as **not small**; confirm scope
  first.
- **Rules / process files are append-only.** `CLAUDE.md` and everything in the project's
  vault (including this file's per-project copy) are additive unless the owner says
  "rewrite / remove / replace." Show proposed additions as a diff before writing.
- **Test on the real target, not just a clean compile** — exercise the deployed preview /
  simulator / device, especially when local can't reach the app's services.
- **Respect the framework's boundaries** (e.g. server/client split, platform lifecycle).
  Read the framework's own docs before writing against it if it's customized or unfamiliar.
- **Write user-facing copy in the project's established language/voice.**
- **Don't assert identity you don't have** — when copy might infer a person (a creator,
  account, user) from a handle, stay neutral; describing an audience or category is fine.

---

## Project canonical facts (fill in per project — append below in the project copy)
> Replace this block in the project's copy with the real facts, and keep them consistent
> everywhere they appear (copy, README, structured data, changelog).
- **Product / app name:**
- **Domain(s) / bundle id:**
- **Plans / pricing / limits:**
- **Key terms (capitalized proper nouns):**
- **Brand voice notes:**
- **Stack + deploy target:**
- **Active platform testing regimen:**

---

## Where the canon lives (per project)
- This protocol (project copy): `docs/claude/PROTOCOL.md`.
- Publish checklist: `docs/claude/PUBLISH-CHECKLIST.md`.
- Living "where we left off": `docs/claude/STATUS.md`.
- Project conventions / rules: `CLAUDE.md` (+ anything it `@`-imports).
- Global clean templates: `~/.claude/PROTOCOL.md`, `~/.claude/PUBLISH-CHECKLIST.md`.
