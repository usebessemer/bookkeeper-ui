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

**Slice 3 (close + sign) additions — same package, read before building Slice 3:**

- `bookkeeper/skills/close_period.py` — `close_period(reconciliation, tax_summary, categorization, config, period) -> CloseReport` (**sync, pure, port-free, writes nothing** — the caller runs the other skills and hands the reports in). `CloseStatus` is `READY | BLOCKED` **only** (no CLOSED/SIGNED — the signed state is entirely the app's artifact). Five preconditions, fixed order: `period_closeable` (vs `config.prior_period_state`, parses `YYYY-Qn`/`YYYY-MM`, fail-safe BLOCK on unparseable/mixed), `period_coherent` (every input report's `.period` == the closing period), `reconciliation_clean` (no gaps/to_confirm), `categorization_complete` (no `flagged`; **proposals do NOT block**), `tax_clean`. **Anomalies are not a precondition** — that gate is the app's.
- `bookkeeper/skills/track_tax.py` — `track_tax(ledger_source, config, period) -> TaxSummary` (**async**, read-only). `select_regime` **fail-fasts** `UnknownTaxRegime` on any regime but `HST` (case-insensitive) — surface it, never swallow.
- `bookkeeper/skills/flag_anomaly.py` — `flag_anomaly(ledger_source, config, period) -> AnomalyReport` (**async**, advisory, writes nothing, never gates a skill). Flags carry **no id** (the app derives one). `over_materiality` is inert when `materiality_floor` is unset.
- **The v1 pin only** applies (`v0.1.0` == `v0.2.0` on all bookkeeper source except `py.typed`). **Zero `agent-classes` changes** — construct only framework-public dataclasses for the effective inputs; the signed/close state, anomaly ids, and reopen are all the app's, never the framework's.

## Build slices

**Slice 1 — categorize-and-confirm: SHIPPED** (released `v0.3.0` to `main`, 2026-07-06). The smallest end-to-end proof — import · confirm-queue (the full trust trail: proposed account + confidence + the rule that fired) · categorized-ledger — over a file store implementing the ports. Issues `#1`/`#2`/`#3` merged; do not re-open.

**Slice 2 — reconcile: SHIPPED** (released `v0.4.0` to `main`, 2026-07-10). Statement import → `reconcile_account` as-is → the reconcile queue (confirm/reject/acknowledge) with every resolution persisted append-only; the app never adjusts a ledger entry. Issues `#21`/`#22`/`#23`/`#24` merged; do not re-open.

**Slice 3 — close review + sign: ACTIVE.** The integrator. Build a **close-review screen** that renders the framework's real close checklist (`close_period`'s `CloseReport`) over **effective reports** (the raw skill output with each persisted human resolution applied), plus the period's anomalies (`flag_anomaly`) and tax summary (`track_tax`), and a **SIGN action** (the section 5.7 human sign-off) that writes a durable, append-only, **self-contained** close record — after which the period renders closed everywhere and its books are write-guarded. **Five `dev-ready` issues (`#35`–`#39`), build in order, one PR each:**

1. **A** (`#35`) — foundation: the three stores (`FileCloseStore` / `FileAnomalyReviewStore` / `FileWaiverStore`), the `examples/config.json` `tax_regime` fix (`standard` → `HST`, or `track_tax` fail-fasts), and the **closed-period write guards** on the existing write paths + the `create_app`/`register_ui`/`build_app_from_env` wiring.
2. **B** — the composition: the effective-`CategorizationReport` + effective-`ReconciliationReport` constructors, `views.build_close_review` (the one shared close projection, incl. the effective-prior-state D4 substitution + the app gates), the `build_ledger`/`LedgerOut` `closed` extension, and the read-only `GET /close`.
3. **C** — the thin write endpoints: `POST /anomalies/review` + `POST /reconciliation/waive`.
4. **D** — the **SIGN action**: `POST /sign` (in-handler re-verification + the period precondition + closed-guard) and the durable self-contained close record. The correctness core (the #14 immutability lesson) — the review-heaviest issue.
5. **E** — the UI: the close-review screen, the htmx acknowledge/waive/sign twins, and closed banners on the existing screens.

Take the **lowest-numbered open `dev-ready` issue** first; each issue body is a self-contained brief with the grounded `v0.1.0` contract + acceptance criteria + out-of-scope inline. The dependency chain is A → B → {C, D} → E (C and D both depend on A+B; neither on the other). **The effective reports are Slice 3's own constructors** — Slice 2 ships only the status-annotated `ReconciliationViewOut`, never an effective `ReconciliationReport`.

**Slice boundaries (all slices):** the app implements the framework's ports and calls its skills **as-is**; **no `agent-classes` changes**; single-user, local, file-based; nothing leaves the machine. Money is exact `Decimal` — strings on the wire and in files, never `float`. Later slices (not now): package preview / receipt-ingest.

## Tests

- **pytest**, green before every commit. `pip install -e .` (with the framework available) then `pytest`.
- The app writes to a ledger / system of record **only through its own stores** — `categorize` writes nothing; confirmations are a separate resolution layer.
- The categorize/confirm paths ship with tests. A change that can't be covered is a flag, not a merge.

## Task intake — substrate, not the human

On launch you are a **dev leaf** for this repo. Your brief lives on the work substrate, not in the human's chat. The human launches you with a bare command + the fixed trigger **"begin"**; they never paste a brief, and you never report progress to them directly — it goes on the PR/issue.

1. **Sync first — this repo AND the framework.** `git fetch origin && git checkout develop && git pull` before branching. Then the same in **`../agent-classes`** (the framework you `pip install -e` — it must be on `develop`, pulled; a stale or feature-branch checkout there silently changes the contract you build against).
2. **Fetch your task.** `gh issue list --label dev-ready --state open` → the issue body is your self-contained brief (scope, acceptance criteria, out-of-scope). Take the **lowest-numbered open `dev-ready` issue** unless the human names one, and build the slice **in issue order** (Slice 3's issues are titled `A` → `B` → `C` → `D` → `E`).
3. **Work it** on a `feature/<issue-slug>` branch — one change at a time, tests green before each commit.
4. **Bubble up on the substrate.** Open a PR against `develop` and mirror the issue's acceptance criteria as a checklist in the body. The lead reviews **on the PR**; the human observes, does not relay. Coordinate via PR / issue comments — never by pasting into the human's chat.
5. **Never self-merge.** The lead (Consulting stream) merges `feature → develop` on a green, AC-passing review. The release boundary (`develop → main`) is the human's gate.

## Conventions

- Branches: `feature/<short-name>` off `develop`; PRs target `develop`. `develop → main` is the release boundary (human-gated).
- **No Claude attribution in commits.** Never add a `Co-Authored-By: Claude` trailer (or any AI-attribution).
- **Some steps need a human** — flag exactly what's needed rather than fabricating or silently stubbing past it.
- Match surrounding style; stay in the issue's scope.
