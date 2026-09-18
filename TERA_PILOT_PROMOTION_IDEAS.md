# Tera Pilot: agent self-promotion ideas

> Working document (draft, September 2026). Recorded ideas, not a launch plan.
> A starting point for discussion; none of this is implemented yet.

---

## 1. Context and principles

Tera Pilot's main promo angle is not "yet another IDE add-on" but
**verifiable, signed agent actions** (Ed25519 + hash chain), locality, and
provider neutrality. Self-promotion should use that same machinery of proof.

Rules that must not be broken even for marketing:

1. **Claims discipline** (see `TERA_PILOT_PRODUCT_STRATEGY.md` §11):
   only measured numbers from the eval harness. No "smarter than Claude Code"
   without our own data.
2. **No telemetry / phone-home**: all scenarios are local or go through
   open APIs (GitHub, Mastodon, Telegram). Otherwise we break our own
   privacy narrative.
3. **The "where" rule**: where the API is open and bots are welcome — the
   agent acts on its own; where self-promotion is harshly punished — the
   agent prepares a draft, a human publishes.
4. Scenarios 1–4 can run right now (testing phase); 5–6 — closer to the
   public release.

---

## 2. Scenarios: "how" the agent promotes itself

### 2.1 Dogfooding — the agent develops its own repository

The most honest and cheapest scenario. The agent works on its own code: fixes
its own bugs, repairs failing tests, refactors, adds features. Every such PR
is a live demo, not a staged video.

- A scheduler (cron / `workflow_dispatch`) assigns the agent a task from the backlog.
- The agent solves it in a clean copy of the repo (as in the eval harness),
  runs the tests, opens a PR with a report.
- Every PR includes a signed audit export
  (`tera-pilot audit export`). Competitors can't do this — they have no
  built-in proof.

### 2.2 Automatic provider benchmark tournament

The strategy forbids quality claims without measurements — so let the agent measure itself.

- Periodic runs of `eval/runner.py` (58 tasks) across the 17 providers and
  local models (Ollama / LM Studio).
- Generating comparative reports (precedents: `GROQ_EVAL_REPORT.md`,
  `eval/REPORT_*.md`) and publishing them.
- Demonstrating multi-provider consensus — a feature in itself.
- Example content: "Measured by Tera Pilot on 58 real tasks: 5/5 on
  OpenRouter, 4/5 on the local 2.6B model".

### 2.3 Content factory — auto-generated materials

The agent writes changelogs, release notes, posts, translations — always based
on real data (git history, eval reports, audit logs), never invented.

- After every release the agent analyses the diff and drafts a post:
  "what changed, measured results, link to signed proof".
- A human publishes (or the agent itself, where the API is open — see platforms).
- Self-verifying content: every number links to a verifiable artifact.

### 2.4 Self-review PRs in its own repository

A GitHub Action workflow generator already exists (`github_automation.py`) —
enable it on the repo itself and get a public live demo of CI readiness.

- For every incoming PR the agent in read-only mode (the `reviewer` profile)
  reviews, comments findings, attaches a report.
- A live example for outsiders: "this is how it will work in your repo".
- Dogfooding, real value, and proof of P1.1 from the readiness plan at once.

### 2.5 Issue triage and community answers

The agent works tickets: reproduces the bug in a clean copy (fixtures already
exist), classifies, prepares a minimal repro and a draft answer.

- A fleet of profiles: one digs through bugs, another answers newcomers.
- Every public thread is a capability demo plus load off the author's shoulders.

### 2.6 Playground "try without installing"

A public demo workspace: anyone drops in, gives the agent a task, and watches
in real time how it plans, edits, runs tests, and asks for confirmation.

- Iteration/token budget caps, approvals visible in the interface.
- Trust-first UX can't be explained in words — you have to see it live once.
- Telegram version: the product already has `--inbound telegram` built in — a
  person messages the bot a task, the agent executes it on the demo workspace
  and replies with the result. A unique feature: try the agent right in Telegram.

---

## 3. Platforms: "where" the agent promotes itself

The center of the system is **GitHub** (native integration:
`github_automation.py`, MCP/ACP, audit). Everything else leads back to it.

| Scenario | Platforms | Who publishes |
|---|---|---|
| 2.1 Dogfooding | GitHub (PR, Issues, Releases); YouTube (video script) | agent itself |
| 2.2 Benchmarks | GitHub README, GitHub Pages, dev.to, Mastodon/Fosstodon; HN | agent itself; HN — human |
| 2.3 Content | X, dev.to, Telegram channel; LinkedIn — drafts | agent + moderation |
| 2.4 Self-review | GitHub Actions (own repo) | agent itself |
| 2.5 Triage/answers | GitHub Issues; Reddit, Discord — drafts | GitHub — agent; Reddit/Discord — human |
| 2.6 Playground | Telegram bot, web sandbox | agent itself |

Platform details:

- **GitHub** — Issues/Discussions (triage, answers, repros), PRs (self-review
  and self-fix), Releases (release notes), README + GitHub Pages (benchmarks
  and docs). The agent acts fully on its own via the API.
- **Awesome lists** — small PRs to `awesome-selfhosted`, `awesome-ollama`,
  `awesome-llm-apps`, `awesome-coding-agents` → target audience
  (Segments A, F from the strategy).
- **Hacker News** — "Show HN" once per release, the agent drafts the post, a
  human publishes (no convenient API; spam kills reputation instantly).
- **Mastodon / Fosstodon** — bot account, the agent posts via API.
  Audience: self-hosted/privacy, bots welcome.
- **X (Twitter)** — the agent posts short reports via API. Careful:
  low frequency, human moderation, otherwise a spam ban.
- **Project Telegram channel** — own subscribers without algorithms; the agent
  publishes itself (built-in integration).
- **Reddit** (r/selfhosted, r/LocalLLaMA, r/ollama, r/OpenSource, r/Python) —
  drafts only: the agent prepares, a human posts. Autoposting = ban.
- **Discord servers** (Ollama, LM Studio, LocalLLaMA) — drafts only.
- **dev.to** — cross-posting articles (Markdown, the agent writes natively).
- **YouTube** — screencast "the agent fixes its own bug live": the agent
  generates a step-by-step script and captions, a human/CI script records.
- **PyPI / npm** — package pages: description, keywords, links
  (a package without a clear description is never found).
- **Product Hunt** — one-shot launch closer to v1.
- **Catalogs** — alternativeto.net, libhunt, Sourcegraph (listing via form).
- **LinkedIn** — drafts for a human only.

---

## 4. Priorities: where to start

The favorite is the 2.1 + 2.4 combo (the agent fixes its own bugs + reviews
its own PRs): cheap, genuinely useful, a stream of live demos with no staged videos.

From "where", first to build:

1. **"Try it" Telegram bot** (2.6) — the feature is already built in
   (`--inbound telegram`), needs a public demo mode with limits.
2. **Self-review Action on this repo** (2.4) — fully automatic.

Then, as ready: the benchmark tournament published to README/Pages
(2.2) and the release content factory (2.3).

---

## 5. Open questions

- Public content language: decided — English-first (this document included).
- Where to host the web sandbox (2.6): Codespaces, VPS, GitHub Pages?
- Do we need a public demo repo with "safe" tasks for the playground?
- Who runs the bot accounts (Mastodon, Telegram, X) and who is responsible
  for the publications?
