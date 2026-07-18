"""Slice 4 · D — the exports UI: listing page + guarded download + POST /ui/export + nav.

Drives the HTML surface `register_ui` mounts on `create_app` (httpx over ASGI, the
Slice 1/2/3/4 style) with injected temp-path stores **and** an injected `export_dir`
under `tmp_path`. D is the read/serve surface over what B wrote — it reads the *same*
append-only `exports.jsonl` log the JSON `GET /exports` reads (never a second reader),
serves files with `FileResponse` from the local exports dir only, and reuses B's
`export_package` for the human export action. D writes only through B's exporter and
mutates nothing on the read/serve paths.

- AC-17 (the load-bearing criterion): the download guard — each manifest-listed Core
  file serves byte-for-byte; an unknown `export_id`/`filename` or any traversal attempt
  → 404 and no file outside the exports dir is ever served (the allow-list keys off the
  log, not disk presence).
- Listing + nav: `GET /ui/exports` lists the whole log newest-first with working
  download links; an empty log renders an honest empty state; the reads are
  mutation-proven read-only; the shared nav carries "Package" and "Exports".
- The export action `POST /ui/export`: the human twin of B's JSON `POST /export` —
  a PROPOSED period exports (one folder + one log row); a BLOCKED rebuild renders the
  refusal partial quoting `unmet_close` verbatim and writes nothing; the handler
  re-obtains via `build_package` at request time and never rides a stale preview.

The framework skill (`generate_accountant_package`, via `build_package`) is called
**as-is**; the app writes only local files (no transmission of any kind). Scaffolding
mirrors tests/test_export.py + tests/test_package_ui.py; builders come from conftest.py.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from markupsafe import escape

from bookkeeper.config import BookkeeperConfig

from bookkeeper_ui.anomaly_reviews import FileAnomalyReviewStore
from bookkeeper_ui.api import create_app
from bookkeeper_ui.closes import FileCloseStore
from bookkeeper_ui.confirmations import (
    SOURCE_HUMAN,
    Confirmation,
    FileConfirmationStore,
)
from bookkeeper_ui.ledger_store import FileLedgerStore, transaction_key
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.waivers import FileWaiverStore, Waiver
from tests.conftest import make_txn

PERIOD = "2026-Q2"
AT = datetime(2026, 7, 1, tzinfo=timezone.utc)
CORE_FILES = {"package.json", "entries.csv", "tax_summary.csv", "manifest.json"}
HASHED_FILES = {"package.json", "entries.csv", "tax_summary.csv"}


# --- Harness + config builders (mirrors test_export.py) -----------------------


@dataclass
class Harness:
    app: FastAPI
    config: BookkeeperConfig
    tmp: Path
    export_dir: Path
    ledger_store: FileLedgerStore
    confirmation_store: FileConfirmationStore
    statement_store: FileStatementStore
    reconciliation_store: FileReconciliationStore
    close_store: FileCloseStore
    anomaly_review_store: FileAnomalyReviewStore
    waiver_store: FileWaiverStore

    @property
    def log_path(self) -> Path:
        return self.export_dir / "exports.jsonl"


def _config(examples_dir: Path, **overrides: object) -> BookkeeperConfig:
    data = json.loads((examples_dir / "config.json").read_text(encoding="utf-8"))
    if "tax_regime" in overrides:
        data["tax_regime"] = overrides["tax_regime"]
    return BookkeeperConfig.from_mapping(data)


def _harness(examples_dir: Path, tmp_path: Path, config: BookkeeperConfig | None = None) -> Harness:
    config = config or _config(examples_dir)
    export_dir = tmp_path / "exports"
    ledger_store = FileLedgerStore(tmp_path / "ledger.jsonl")
    confirmation_store = FileConfirmationStore(tmp_path / "confirmations.jsonl")
    statement_store = FileStatementStore(tmp_path / "statements.jsonl")
    reconciliation_store = FileReconciliationStore(tmp_path / "reconciliations.jsonl")
    close_store = FileCloseStore(tmp_path / "closes.jsonl")
    anomaly_review_store = FileAnomalyReviewStore(tmp_path / "anomaly_reviews.jsonl")
    waiver_store = FileWaiverStore(tmp_path / "reconciliation_waivers.jsonl")
    app = create_app(
        config=config,
        ledger_store=ledger_store,
        confirmation_store=confirmation_store,
        statement_store=statement_store,
        reconciliation_store=reconciliation_store,
        close_store=close_store,
        anomaly_review_store=anomaly_review_store,
        waiver_store=waiver_store,
        export_dir=export_dir,
    )
    return Harness(
        app, config, tmp_path, export_dir, ledger_store, confirmation_store,
        statement_store, reconciliation_store, close_store, anomaly_review_store,
        waiver_store,
    )


@pytest.fixture
def harness(examples_dir, tmp_path) -> Harness:
    return _harness(examples_dir, tmp_path)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# Grounded against the shipped example config (same as test_export.py):
#   "AWS"  → owner-rule proposal.  "Rent" → chart-match proposal.
#   "Zzxq Gibberish" → below the categorize floor → FLAGGED (blocks until confirmed).
def _aws() -> object:
    return make_txn(vendor="AWS", amount="50.00", tax="0", date=datetime(2026, 5, 1), description="cloud")


def _rent() -> object:
    return make_txn(vendor="Rent", amount="20.00", tax="0", date=datetime(2026, 5, 2), description="office rent")


def _flagged() -> object:
    return make_txn(vendor="Zzxq Gibberish", amount="5.00", tax="0", date=datetime(2026, 5, 3), description="")


async def _waive(h: Harness, period: str = PERIOD) -> None:
    await h.waiver_store.record(Waiver(period=period, waived_at=AT, waived_by="human", note=""))


async def _ready_three_sources(h: Harness) -> object:
    """A READY close (all three proposal sources + a waiver) → a PROPOSED package.

    The confirmed flag clears `categorization_complete`; the waiver clears
    reconciliation. Returns the flagged (now human-confirmed) transaction.
    """
    await h.ledger_store.store(_aws())
    await h.ledger_store.store(_rent())
    flagged = _flagged()
    await h.ledger_store.store(flagged)
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(flagged), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    await _waive(h)
    return flagged


async def _diverged_ready(h: Harness) -> None:
    """A READY close whose AWS entry is CORRECTED away from its proposal → divergence_count ≥ 1."""
    await h.ledger_store.store(_aws())      # owner-rule proposes 5100-software-subscriptions
    await h.ledger_store.store(_rent())
    flagged = _flagged()
    await h.ledger_store.store(flagged)
    aws = _aws()
    await h.confirmation_store.record(       # override AWS to a different account → a divergence
        Confirmation(transaction_id=transaction_key(aws), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    await h.confirmation_store.record(
        Confirmation(transaction_id=transaction_key(flagged), account="5000-office-supplies",
                     source=SOURCE_HUMAN, decided_at=AT)
    )
    await _waive(h)


async def _post_export_json(app: FastAPI, period: str = PERIOD) -> httpx.Response:
    """Produce a real export via B's JSON `POST /export` (the true artifact)."""
    async with _client(app) as client:
        return await client.post("/export", params={"period": period})


async def _post_ui_export(app: FastAPI, period: str = PERIOD, **form: object) -> httpx.Response:
    async with _client(app) as client:
        return await client.post("/ui/export", data={"period": period, **form})


def _log_rows(h: Harness) -> list[str]:
    if not h.log_path.exists():
        return []
    return [line for line in h.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _export_folders(h: Harness) -> list[Path]:
    if not h.export_dir.exists():
        return []
    return sorted(p for p in h.export_dir.iterdir() if p.is_dir())


def _tree_bytes(export_dir: Path) -> dict[str, bytes]:
    """Every file under the exports dir → its exact bytes (the read-only snapshot)."""
    if not export_dir.exists():
        return {}
    return {
        str(p.relative_to(export_dir)): p.read_bytes()
        for p in sorted(export_dir.rglob("*"))
        if p.is_file()
    }


def _download_hrefs(html: str) -> list[str]:
    """Every guarded-download href the rendered surface offers, in document order."""
    return re.findall(r'href="(/ui/exports/[^"]+/[^"]+)"', html)


# ============================================================================
# AC-17 — the download guard (the load-bearing criterion)
# ============================================================================


async def test_download_each_core_file_byte_for_byte(harness: Harness):
    """AC-17 (happy path): each of the four Core files serves 200, byte-for-byte the
    content on disk; the three hashed files also match the manifest's recorded sha256."""
    await _ready_three_sources(harness)
    export_id = (await _post_export_json(harness.app)).json()["export_id"]
    folder = harness.export_dir / export_id
    manifest = json.loads((folder / "manifest.json").read_bytes())
    recorded = {f["name"]: f["sha256"] for f in manifest["files"]}
    assert set(recorded) == HASHED_FILES  # manifest never hashes itself

    async with _client(harness.app) as client:
        for name in CORE_FILES:
            resp = await client.get(f"/ui/exports/{export_id}/{name}")
            assert resp.status_code == 200, name
            on_disk = (folder / name).read_bytes()
            assert resp.content == on_disk  # byte-for-byte
            if name in recorded:  # the three hashed files also match the manifest hash
                assert hashlib.sha256(resp.content).hexdigest() == recorded[name]


async def test_unknown_export_id_404(harness: Harness):
    """AC-17: a well-formed but nonexistent `export_id` → 404, no traceback."""
    await _ready_three_sources(harness)
    await _post_export_json(harness.app)  # a real export exists, but under a different id
    async with _client(harness.app) as client:
        resp = await client.get("/ui/exports/2026-Q2--00000000T000000000000Z/package.json")
    assert resp.status_code == 404


async def test_unlisted_filename_404_even_when_present_on_disk(harness: Harness):
    """AC-17: a real `export_id` but a filename not in the record's file-list → 404 —
    proving the guard keys off the manifest/log allow-list, not disk presence. A real
    neighbouring file planted in the folder (not in the manifest) is refused too."""
    await _ready_three_sources(harness)
    export_id = (await _post_export_json(harness.app)).json()["export_id"]
    # Plant a real file in the export folder that is NOT one of the recorded Core files.
    planted = harness.export_dir / export_id / "secret.txt"
    planted.write_bytes(b"top secret")

    async with _client(harness.app) as client:
        assert (await client.get(f"/ui/exports/{export_id}/secret.json")).status_code == 404
        resp = await client.get(f"/ui/exports/{export_id}/secret.txt")
    assert resp.status_code == 404          # on disk, but not in the allow-list
    assert b"top secret" not in resp.content


async def test_traversal_attempts_404_and_nothing_escapes(harness: Harness):
    """AC-17: `../…`, an absolute path, and a URL-encoded `..%2F…` each → 404, and a
    sentinel planted OUTSIDE the exports dir is never returned by any of them."""
    await _ready_three_sources(harness)
    export_id = (await _post_export_json(harness.app)).json()["export_id"]

    # A sentinel one level up from the exports dir — the target a traversal would seek.
    sentinel = harness.tmp / "sentinel.txt"
    sentinel.write_bytes(b"SENTINEL-OUTSIDE-EXPORTS")

    attempts = [
        f"/ui/exports/{export_id}/{quote('../sentinel.txt', safe='')}",
        f"/ui/exports/{export_id}/{quote('../../sentinel.txt', safe='')}",
        f"/ui/exports/{export_id}/{quote(str(sentinel), safe='')}",        # absolute path
        f"/ui/exports/{export_id}/..%2F..%2Fsentinel.txt",                  # pre-encoded ..%2F
        f"/ui/exports/{quote('../..', safe='')}/package.json",             # traversal in the id
    ]
    async with _client(harness.app) as client:
        for url in attempts:
            resp = await client.get(url)
            assert resp.status_code == 404, url
            assert b"SENTINEL-OUTSIDE-EXPORTS" not in resp.content, url


async def test_serves_only_from_the_injected_exports_dir(harness: Harness):
    """AC-17: a file that exists in a *sibling* dir (outside the injected exports dir) is
    never reachable — the only served bytes come from `exports/<export_id>/`."""
    await _ready_three_sources(harness)
    export_id = (await _post_export_json(harness.app)).json()["export_id"]
    sibling = harness.tmp / "other" / "package.json"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_bytes(b'{"leak": true}')

    async with _client(harness.app) as client:
        # Reference the sibling by an absolute path and by climbing out — both refused.
        for name in [str(sibling), "../other/package.json"]:
            resp = await client.get(f"/ui/exports/{export_id}/{quote(name, safe='')}")
            assert resp.status_code == 404
            assert b'"leak"' not in resp.content


async def test_listed_but_missing_on_disk_is_404_not_500(harness: Harness):
    """AC-17 (robustness): a file in the allow-list but absent on disk → 404, never a 500."""
    await _ready_three_sources(harness)
    export_id = (await _post_export_json(harness.app)).json()["export_id"]
    (harness.export_dir / export_id / "entries.csv").unlink()  # listed, now missing
    async with _client(harness.app) as client:
        resp = await client.get(f"/ui/exports/{export_id}/entries.csv")
    assert resp.status_code == 404


async def test_listed_symlink_escaping_root_is_404_and_never_served(harness: Harness):
    """AC-17 (the containment second-wall): a LISTED filename (`package.json`, in the
    allow-list) whose on-disk entry is a symlink resolving OUTSIDE the exports root → 404,
    and the outside sentinel's bytes are never served. This REACHES web.py's
    `candidate.is_relative_to(root)` wall — the allow-list membership passes, so the
    router-404'd traversal inputs never exercise it — pinning the defense-in-depth wall:
    drop it and the resolved outside file leaks (200 + sentinel)."""
    await _ready_three_sources(harness)
    export_id = (await _post_export_json(harness.app)).json()["export_id"]

    # A sentinel OUTSIDE the exports dir — the target a resolved symlink would escape to.
    outside = harness.tmp / "outside_secret.json"
    outside.write_bytes(b"SENTINEL-OUTSIDE-EXPORTS")

    # Replace the real, listed package.json with a symlink pointing at the outside file.
    listed = harness.export_dir / export_id / "package.json"
    listed.unlink()
    listed.symlink_to(outside)

    async with _client(harness.app) as client:
        resp = await client.get(f"/ui/exports/{export_id}/package.json")
    assert resp.status_code == 404
    assert b"SENTINEL-OUTSIDE-EXPORTS" not in resp.content


# ============================================================================
# Listing + nav
# ============================================================================


async def test_listing_newest_first_with_working_download_links(harness: Harness):
    """AC-6: `GET /ui/exports` → 200 listing every export newest-first, each row rendering
    its id/period/status/time/divergence and one *working* download link per Core file."""
    await _ready_three_sources(harness)
    id1 = (await _post_export_json(harness.app)).json()["export_id"]
    id2 = (await _post_export_json(harness.app)).json()["export_id"]
    assert id1 != id2

    async with _client(harness.app) as client:
        resp = await client.get("/ui/exports")
        assert resp.status_code == 200
        html = resp.text

        # Newest-first: the later-inserted export appears above the earlier one.
        assert html.index(id2) < html.index(id1)
        # Each row renders its fields.
        for export_id in (id1, id2):
            assert export_id in html
        assert ">proposed<" in html
        assert PERIOD in html

        # Every download link on the page resolves 200 — ties the listing to the guard.
        hrefs = _download_hrefs(html)
        assert len(hrefs) == 2 * len(CORE_FILES)  # four Core files per export, two exports
        for href in hrefs:
            assert (await client.get(href)).status_code == 200


async def test_empty_log_renders_honest_empty_state(harness: Harness):
    """AC-7: an empty log → `GET /ui/exports` returns 200 with an honest empty state
    (not a 500, not a blank body)."""
    async with _client(harness.app) as client:
        resp = await client.get("/ui/exports")
    assert resp.status_code == 200
    assert "No exports yet" in resp.text
    assert _download_hrefs(resp.text) == []


async def test_listing_and_downloads_are_mutation_proven_read_only(harness: Harness):
    """AC-8: `GET /ui/exports` and every `GET /ui/exports/{…}` leave `exports.jsonl` and
    every export folder byte-identical before/after (this slice's read paths never write)."""
    await _ready_three_sources(harness)
    await _post_export_json(harness.app)
    await _post_export_json(harness.app)

    before = _tree_bytes(harness.export_dir)
    async with _client(harness.app) as client:
        html = (await client.get("/ui/exports")).text
        for href in _download_hrefs(html):
            await client.get(href)
    after = _tree_bytes(harness.export_dir)
    assert after == before  # nothing on the read/serve path wrote a byte


async def test_nav_carries_package_and_exports_on_every_page(harness: Harness):
    """AC-9: `base.html` renders the "Package" and "Exports" nav links (assert on a
    rendered UI page's HTML)."""
    async with _client(harness.app) as client:
        html = (await client.get("/ui/exports")).text
    assert 'href="/ui/package?period=2026-Q2">Package</a>' in html
    assert 'href="/ui/exports?period=2026-Q2">Exports</a>' in html


# ============================================================================
# The export action — POST /ui/export (the human twin of B's JSON POST /export)
# ============================================================================


async def test_post_ui_export_happy_path_writes_one_export_with_working_links(harness: Harness):
    """AC-10: `POST /ui/export` for a PROPOSED period → 200 partial with the export id +
    one working download link per Core file; exactly one new folder + one new log row."""
    await _diverged_ready(harness)  # PROPOSED with divergence_count ≥ 1

    resp = await _post_ui_export(harness.app, PERIOD, acknowledged="on")
    assert resp.status_code == 200
    html = resp.text
    assert "Exported to local files" in html

    rows = _log_rows(harness)
    assert len(rows) == 1  # exactly one appended log row
    export_id = json.loads(rows[0])["export_id"]
    assert export_id in html
    assert len(_export_folders(harness)) == 1

    hrefs = _download_hrefs(html)
    assert len(hrefs) == len(CORE_FILES)  # one link per Core file
    async with _client(harness.app) as client:
        for href in hrefs:
            assert (await client.get(href)).status_code == 200


async def test_post_ui_export_blocked_refusal_renders_partial_and_writes_nothing(harness: Harness):
    """AC-11: with a BLOCKED close, `POST /ui/export` → 200 partial quoting `unmet_close`
    VERBATIM, and no export folder / no new log row (mutation-proven byte-identical)."""
    await harness.ledger_store.store(_flagged())  # blocks categorization_complete
    await _waive(harness)                          # isolate the flag as the sole blocker

    # The verbatim reason the JSON route would 409 with — the partial must quote it.
    async with _client(harness.app) as client:
        reason = (await client.get("/package", params={"period": PERIOD})).json()["unmet_close"]
    assert "categorization_complete" in reason

    before = _tree_bytes(harness.export_dir)
    resp = await _post_ui_export(harness.app, PERIOD)
    assert resp.status_code == 200                 # a human refusal is a 200 partial
    assert str(escape(reason)) in resp.text        # unmet_close verbatim (HTML-escaped as Jinja does)
    assert _log_rows(harness) == []                # no log row
    assert _export_folders(harness) == []          # no folder
    assert _tree_bytes(harness.export_dir) == before


async def test_post_ui_export_reobtains_and_never_rides_a_stale_preview(harness: Harness):
    """AC-12: the handler rebuilds via `build_package` at request time — flipping the close
    to BLOCKED *after* a successful preview still yields the refusal partial, never an
    export of stale preview state (mirrors B's AC-5)."""
    await _ready_three_sources(harness)
    async with _client(harness.app) as client:
        preview = (await client.get("/ui/package", params={"period": PERIOD})).text
        assert "proposed" in preview  # the preview was PROPOSED at view time

    # Flip to BLOCKED after the preview: a fresh unresolved flag breaks categorization.
    await harness.ledger_store.store(
        make_txn(vendor="Late Gibberish Qqzz", amount="7.00", tax="0",
                 date=datetime(2026, 5, 20), description="")
    )

    before = _tree_bytes(harness.export_dir)
    resp = await _post_ui_export(harness.app, PERIOD)
    assert resp.status_code == 200
    assert "categorization_complete" in resp.text  # refused on the rebuilt (BLOCKED) close
    assert _log_rows(harness) == []
    assert _tree_bytes(harness.export_dir) == before


async def test_ui_export_refusal_leaves_a_prior_export_byte_identical(harness: Harness):
    """AC-11/12 (non-empty baseline): the refusal tests above capture `before` with no
    prior export, so their byte-identity check reduces to `{} == {}`. This seeds one
    SUCCESSFUL export first, so the comparison protects REAL prior bytes — after a later
    BLOCKED `POST /ui/export`, the earlier folder + log row are left byte-identical and no
    second folder appears (mirrors B's `test_export_blocked_after_prior_export_...`)."""
    await _ready_three_sources(harness)
    ok = await _post_ui_export(harness.app, PERIOD)
    assert ok.status_code == 200
    assert len(_export_folders(harness)) == 1
    before = _tree_bytes(harness.export_dir)
    assert before  # a real export is present — the baseline is genuinely non-empty

    # Flip the period to BLOCKED: a fresh unresolved flag breaks categorization_complete.
    await harness.ledger_store.store(
        make_txn(vendor="Late Gibberish Qqzz", amount="7.00", tax="0",
                 date=datetime(2026, 5, 20), description="")
    )
    resp = await _post_ui_export(harness.app, PERIOD)
    assert resp.status_code == 200
    assert "categorization_complete" in resp.text     # refused on the rebuilt (BLOCKED) close
    assert _tree_bytes(harness.export_dir) == before   # prior export byte-identical
    assert len(_export_folders(harness)) == 1          # no second folder
