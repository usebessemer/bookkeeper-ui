# bookkeeper-ui — project guide

The local, open-source **thin UI** for the Bessemer Bookkeeper agent — the first app on an open-source agent OS. A user runs it locally: **import transactions → the agent proposes a chart-of-accounts category per transaction → the human confirms/corrects → confirmations persist.** Local-first, single-user, file-based. Open-source (`usebessemer/bookkeeper-ui`).

The app is built by consulting; the framework and the ports/contracts it implements are OSS (`agent-classes`). This repo depends on the framework — it never modifies it.

## Stack

- **Backend:** **FastAPI** (async — matches the framework's async contract; serves the UI too).
- **UI:** **Jinja templates + htmx — no Node build step.** Deliberate: the whole Bookkeeper interaction surface (this slice + the future reconcile / close-and-sign / package / anomalies slices) is review-queue and confirm/sign/export shaped, which htmx serves directly. The one thing htmx is weak at — a rich client-side analytics dashboard — is excluded from the Bookkeeper by design. The Tier-2 API stays the durable seam if a richer per-view client is ever wanted later — not a one-way door.
- **Store:** file-based, local (the generic "local store" — the default `booksLocation` when a business has no external system of record).

## The framework you depend on — read it, never modify it

The `bookkeeper` package in **`usebessemer/agent-classes`** (headless, dependency-free, research-grade — no web stack goes into it). Its source is the sibling clone at **`../agent-classes/bookkeeper/`** (install it as an editable dependency for dev: `pip install -e ../agent-classes`). Read these for the exact contract:

- `bookkeeper/ports.py` — `LedgerSink.store(transaction)` (async, write) and `LedgerSource.fetch_for_period(period)` (async, read; deterministic order). **You implement both over a file store.**
- `bookkeeper/model.py` — the `Transaction` model (what import maps onto).
- `bookkeeper/config.py` — `BookkeeperConfig` (`chart_of_accounts`, `categorize_threshold()`); you load one from a file.
- `bookkeeper/skills/categorize.py` — `categorize(ledger_source, config, period) -> CategorizationReport` (async). Returns `proposals` (transaction + `proposed_account` + `confidence` + `source`/rule that fired) and `flagged` (transaction + `reason`). **Called as-is; never auto-assigns — the human confirm/correct step is the point of this app.**

## Slice 1 — standalone categorize-and-confirm

The smallest thing that proves the interaction surface end to end. **Build the issues in order, one issue per PR:**

1. **#1 Foundation** — file store implementing the ports · CSV/JSON import · config loading.
2. **#2 API** — run `categorize` · submit confirm/correct resolutions · read the categorized ledger.
3. **#3 Thin UI** — import · the confirm-queue rendering the full **trust trail** (proposed account + confidence + the rule that fired) · the categorized-ledger view.

**Slice boundaries:** categorize-only; **no `agent-classes` changes**; single-user, local, file-based. Later slices (not now): reconcile / close-and-sign / package preview / anomalies queue.

## Tests

- **pytest**, green before every commit. `pip install -e .` (with the framework available) then `pytest`.
- The app writes to a ledger / system of record **only through its own stores** — `categorize` writes nothing; confirmations are a separate resolution layer.
- The categorize/confirm paths ship with tests. A change that can't be covered is a flag, not a merge.

## Task intake — substrate, not the human

On launch you are a **dev leaf** for this repo. Your brief lives on the work substrate, not in the human's chat. The human launches you with a bare command + the fixed trigger **"begin"**; they never paste a brief, and you never report progress to them directly — it goes on the PR/issue.

1. **Sync first — this repo AND the framework.** `git fetch origin && git checkout develop && git pull` before branching. Then the same in **`../agent-classes`** (the framework you `pip install -e` — it must be on `develop`, pulled; a stale or feature-branch checkout there silently changes the contract you build against).
2. **Fetch your task.** `gh issue list --label dev-ready --state open` → the issue body is your self-contained brief (scope, acceptance criteria, out-of-scope). Build **in issue order** (#1 → #2 → #3); take the lowest-numbered open `dev-ready` issue unless the human names one.
3. **Work it** on a `feature/<issue-slug>` branch — one change at a time, tests green before each commit.
4. **Bubble up on the substrate.** Open a PR against `develop` and mirror the issue's acceptance criteria as a checklist in the body. The lead reviews **on the PR**; the human observes, does not relay. Coordinate via PR / issue comments — never by pasting into the human's chat.
5. **Never self-merge.** The lead (Consulting stream) merges `feature → develop` on a green, AC-passing review. The release boundary (`develop → main`) is the human's gate.

## Conventions

- Branches: `feature/<short-name>` off `develop`; PRs target `develop`. `develop → main` is the release boundary (human-gated).
- **No Claude attribution in commits.** Never add a `Co-Authored-By: Claude` trailer (or any AI-attribution).
- **Some steps need a human** — flag exactly what's needed rather than fabricating or silently stubbing past it.
- Match surrounding style; stay in the issue's scope.
