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

**Slice 2 (reconcile) additions — same package, read before building Slice 2:**

- `bookkeeper/ports.py` — `StatementSource.fetch_statement(period) -> list[StatementLine]` (async, read-only). No statement *writer* port exists; do not invent one.
- `bookkeeper/model.py` — `StatementLine(statement_ref, date, amount, description)`.
- `bookkeeper/skills/reconcile.py` — `reconcile_account(ledger_source, statement_source, config, period) -> ReconciliationReport` (async, detection-only, writes nothing): `matched` (no score/reason) · `to_confirm` (carries `vendor_similarity` + `reason`) · `gaps` (three `GapKind`s; signed `delta` on `amount_mismatch`). Deterministic order — preserve it in every surface. **Called as-is.**
- `bookkeeper/config.py` — `reconcile_date_window()` (default 3, not a section-5 boundary) and `reconcile_vendor_threshold() -> float | None` (**a section-5 boundary: `None` = inert**; `DEFAULT_RECONCILE_VENDOR_FLOOR = 0.7`). Display-only; never re-implement the matching.

The `v0.1.0` pin and the `develop` clone are byte-identical on the reconcile files, so build to what these say.

## Build slices

**Slice 1 — categorize-and-confirm: SHIPPED** (released `v0.3.0` to `main`, 2026-07-06). The smallest end-to-end proof — import · confirm-queue (the full trust trail: proposed account + confidence + the rule that fired) · categorized-ledger — over a file store implementing the ports. Issues `#1`/`#2`/`#3` merged; do not re-open.

**Slice 2 — reconcile: ACTIVE.** Import the authoritative bank/card statement for a period → run `reconcile_account` **as-is** against the books the app already holds → work a **reconcile queue** (confirm/reject the pairs the skill surfaced, acknowledge the gaps) with every resolution persisted append-only. The app never adjusts a ledger entry — a discrepancy is surfaced, never auto-fixed (section 5.5). Three `dev-ready` issues, build **in order**, one PR each:

1. **A** — statement store + statement import (`StatementSource` over JSONL).
2. **B** — reconcile API + resolution store (`reconcile_account`, persisted resolutions, the one overlaid projection).
3. **C** — reconcile queue UI + ledger fold.

**One decided quick fix leads the queue:** issue **#21** (N1: strict **404** on `/resolve` for an unknown transaction id, plus its UI twin) is the lowest-numbered `dev-ready`, so you take it **first**. It establishes the strict-404 resolve rule that Slice 2's `/reconcile/resolve` then mirrors (see issue B). Issue **#5** (identity/dedupe, normalize+count) is deliberately **after** Slice 2 — it sweeps the transaction and statement keys together.

**Slice boundaries (all slices):** the app implements the framework's ports and calls its skills **as-is**; **no `agent-classes` changes**; single-user, local, file-based; nothing leaves the machine. Money is exact `Decimal` — strings on the wire and in files, never `float`. Later slices (not now): close-and-sign / package preview / anomalies.

## Tests

- **pytest**, green before every commit. `pip install -e .` (with the framework available) then `pytest`.
- The app writes to a ledger / system of record **only through its own stores** — `categorize` writes nothing; confirmations are a separate resolution layer.
- The categorize/confirm paths ship with tests. A change that can't be covered is a flag, not a merge.

## Task intake — substrate, not the human

On launch you are a **dev leaf** for this repo. Your brief lives on the work substrate, not in the human's chat. The human launches you with a bare command + the fixed trigger **"begin"**; they never paste a brief, and you never report progress to them directly — it goes on the PR/issue.

1. **Sync first — this repo AND the framework.** `git fetch origin && git checkout develop && git pull` before branching. Then the same in **`../agent-classes`** (the framework you `pip install -e` — it must be on `develop`, pulled; a stale or feature-branch checkout there silently changes the contract you build against).
2. **Fetch your task.** `gh issue list --label dev-ready --state open` → the issue body is your self-contained brief (scope, acceptance criteria, out-of-scope). Take the **lowest-numbered open `dev-ready` issue** unless the human names one, and build the slice **in issue order** (its issues are titled `A` → `B` → `C`).
3. **Work it** on a `feature/<issue-slug>` branch — one change at a time, tests green before each commit.
4. **Bubble up on the substrate.** Open a PR against `develop` and mirror the issue's acceptance criteria as a checklist in the body. The lead reviews **on the PR**; the human observes, does not relay. Coordinate via PR / issue comments — never by pasting into the human's chat.
5. **Never self-merge.** The lead (Consulting stream) merges `feature → develop` on a green, AC-passing review. The release boundary (`develop → main`) is the human's gate.

## Conventions

- Branches: `feature/<short-name>` off `develop`; PRs target `develop`. `develop → main` is the release boundary (human-gated).
- **No Claude attribution in commits.** Never add a `Co-Authored-By: Claude` trailer (or any AI-attribution).
- **Some steps need a human** — flag exactly what's needed rather than fabricating or silently stubbing past it.
- Match surrounding style; stay in the issue's scope.
