# The intake port

The intake port is how any extractor submits **candidate transactions** to
bookkeeper-ui — the machine-facing half of receipt capture. An extractor reads a
source artifact (a receipt image, a PDF invoice, a plain-text memo), pulls out the
transaction fields, and POSTs them here **together with the raw artifact**. The app
holds each submission as a *candidate*.

A **candidate is a proposal that can never touch the ledger.** It sits on the
proposal side of the review boundary. Only a human **confirm** constructs a ledger
transaction; a human **reject** discards the proposal. Everything an extractor
sends is a suggestion the person reviewing the books gets to accept or correct.

This document is the generic port contract an extractor author builds against. It
knows nothing about any specific extractor: the examples use neutral placeholders
(`acme-extractor`, `job-001`). How you *produce* candidates — OCR, a vision model,
an inbox poller — is entirely your side of the wire and out of scope here.

The port only ever **receives**. It never sends anything back out, never polls an
inbox, never syncs to an external system. Transmission is a separate concern behind
a separate gate.

---

## The candidate document

`POST /intake/candidates` accepts one candidate as JSON:

```json
{
  "source": "acme-extractor",
  "submission_id": "acme-18c9f2ab44e01",
  "vendor": "Home Depot",
  "amount": "82.50",
  "tax": "10.73",
  "date": "2026-06-14",
  "description": "Lumber and fasteners",
  "attribution_target_id": "job-001",
  "source_hint": "Receipt - site materials",
  "received_at": "2026-06-14T15:02:11+00:00",
  "artifact": "<base64 of the source artifact bytes>",
  "artifact_media_type": "image/jpeg"
}
```

### Field rules

Every field is validated server-side. A violation is a **422 naming the field**;
nothing is written on a validation failure (never a partial write).

| field | required | rule |
|---|---|---|
| `source` | yes | non-blank string — the extractor's identity; it namespaces `submission_id` |
| `submission_id` | yes | non-blank string — the extractor's **stable** id for this artifact (the idempotency key) |
| `vendor` | yes | non-blank string |
| `amount` | yes | a JSON **string** parsing to a finite `Decimal` (`"82.50"`). A JSON **number** is a 422. `"NaN"` / `"Infinity"` and **exponent notation** (`"1E+2"`) are rejected — send a plain decimal string |
| `tax` | no | same string-`Decimal` rule; absent or blank → `"0"` (the books never hold null money) |
| `date` | yes | ISO 8601 (`2026-06-14` or `2026-06-14T15:02:11+00:00`) |
| `description` | no | string; absent → `""` |
| `attribution_target_id` | no | string or null — an extractor that resolved attribution sends it; one that didn't sends null, and the human assigns it at review |
| `source_hint` | no | free-text string (a filename, a memo, a subject line); absent → `""` |
| `received_at` | no | ISO 8601, or absent |
| `artifact` | yes | base64; the decoded bytes must be non-empty and within the size cap (default 10 MiB, env `BOOKKEEPER_UI_MAX_ARTIFACT_BYTES`) |
| `artifact_media_type` | yes | one of `image/jpeg`, `image/png`, `image/webp`, `image/gif`, `application/pdf`, `text/plain` |

**Money is always a string on the wire.** `amount` and `tax` carry the exact
`Decimal` as text so nothing is lost to floating point. Sending `82.50` as a JSON
number is a 422 — send `"82.50"`. Exponent notation (`"1E+2"`) is refused too: it
round-trips as a different string than the economically-equal `"100"`, which would
weaken the ledger's honest de-duplication — send the plain decimal form.

> **Deployment note (artifact size).** The size cap is enforced **after** the
> base64 artifact is decoded into memory, so a grossly oversized upload is still
> read and decoded before it is rejected (it is never *written* — the over-cap path
> stores nothing). This is a correctness-safe but DoS-shaped ordering; for a
> hardened deployment, cap the request body size upstream at the reverse proxy or
> uvicorn (`--limit-max-requests` / a proxy `client_max_body_size`) so an oversized
> payload is refused before it reaches the app.

A candidate carries **no category**. Categorization is a downstream skill driven by
the deployment's chart of accounts, not something an extractor reports.

### Response

- **201** on a first write, with the stored candidate and `"duplicate": false`.
- **200** on a re-submission (see idempotency), with the *existing* candidate and
  `"duplicate": true`.

```json
{
  "duplicate": false,
  "candidate": {
    "candidate_id": "9f2c…",
    "source": "acme-extractor",
    "submission_id": "acme-18c9f2ab44e01",
    "vendor": "Home Depot",
    "amount": "82.50",
    "tax": "10.73",
    "date": "2026-06-14T00:00:00",
    "description": "Lumber and fasteners",
    "attribution_target_id": "job-001",
    "source_hint": "Receipt - site materials",
    "received_at": "2026-06-14T15:02:11+00:00",
    "artifact_media_type": "image/jpeg",
    "artifact_sha256": "…",
    "submitted_at": "2026-06-20T18:03:44.101020+00:00"
  }
}
```

The raw artifact is **not** echoed in the candidate JSON — it is served on its own
route (below). `artifact_sha256` is the integrity / traceability link to those
bytes.

---

## Idempotency

**Candidate identity is `(source, submission_id)`.** The `candidate_id` is
`sha256(source + "\n" + submission_id)` — deterministic and safe to use in a URL.

Submitting is **idempotent on that identity**: re-POSTing the same
`(source, submission_id)` writes nothing and returns **200** with the already-stored
candidate and `"duplicate": true`. **First write wins** — a re-POST with a *different*
payload does **not** mutate the stored candidate.

Give each artifact a stable `submission_id` and a retry (after a dropped connection,
say) is a safe no-op rather than a double-file.

A re-POST is recognized as a duplicate **only when its payload still validates.** The
field gate runs before the identity is looked up, so a re-POST of a known
`(source, submission_id)` that carries an *invalid* field is a **422** (naming the
field), not a short-circuit 200 — the stored candidate is left unchanged either way,
so this is never a partial write, just a stricter (and safe) response on a malformed
retry.

---

## Routes

| method + path | purpose |
|---|---|
| `POST /intake/candidates` | submit one candidate document (idempotent; 201 first write, 200 duplicate) |
| `GET /intake/candidates?status=pending\|confirmed\|rejected` | list candidates with their standing; **omit `status` for all statuses** |
| `GET /intake/artifact/{candidate_id}` | fetch a candidate's raw source artifact bytes, with its declared media type (404 if unknown) |
| `POST /intake/resolve` | the human review gate: confirm (files a ledger transaction) or reject |

### `GET /intake/candidates`

Returns every candidate in submission order, each with its `standing`:

- `pending` — no human decision yet;
- `confirmed` — a human confirmed it into the ledger;
- `rejected` — a human discarded it.

`?status=` filters to one standing. With no filter, **all** statuses are returned —
this is the one shared projection every surface (JSON here, the HTML review queue
later) reads, so they never disagree about where a candidate stands.

### `POST /intake/resolve`

```json
{ "candidate_id": "9f2c…", "action": "confirm",
  "vendor": "Home Depot", "amount": "82.50", "tax": "10.73",
  "date": "2026-06-14", "description": "Lumber and fasteners",
  "attribution_target_id": "job-001" }
```

**Confirm** files a ledger transaction from the candidate. The reviewer may correct
any field first; each supplied value is re-validated through the same gate the
submission passed, and any field left out falls back to the candidate's own value.

- `attribution_target_id` is **required** on confirm and must be one of the
  deployment's configured attribution targets (the app never invents a target).
- A confirm whose date falls in a **signed-closed** period is refused (**409**) — a
  closed period's books are write-guarded.
- **Honest dedupe.** The ledger de-duplicates on the transaction's natural business
  key (vendor + amount + tax + date + target + description). If a confirmed candidate
  matches an already-filed transaction, no second ledger row is written — and the
  response says so: `ledger_outcome` is `"already-present"` (vs `"stored"` for a new
  row). A duplicate is **visible**, never a silently dropped filing.

**Reject** records the decision (with an optional `reject_reason`) and leaves the
ledger untouched. The submission row and its artifact stay on disk — the trail is
append-only.

Errors: **404** an unknown `candidate_id`, **or** a candidate whose source artifact is
missing at confirm (a confirm files the receipt bytes with the ledger row, so it refuses
rather than filing a row with no artifact); **409** an already-decided candidate (its
recorded outcome is returned — re-opening a decided candidate is out of scope), **or** a
confirm whose (edited) date falls in a signed-closed period; **422** a bad confirmed field.

---

## Attribution target labels are the app's, not the wire's

An extractor always sends **opaque `attribution_target_id` strings** (`job-001`).
The port carries ids, never human-friendly names. Giving those ids display names —
an optional `attribution_target_labels` map — is bookkeeper-ui's own **deployment
config**, resolved app-side at render time. It is not part of the wire contract and
an extractor neither sends nor needs it.
