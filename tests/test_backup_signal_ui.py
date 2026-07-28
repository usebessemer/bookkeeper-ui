"""Slice 6 · #87 — the backup safe-signal UI (the global chip + the loud nag banner).

Three things this issue owes, tested at the layers that own them:

- **The collapse** (`BackupSignalOut.from_status`): the framework's four-state
  `BackupStatus` ladder (+ its `escalated` nag flag) folds to EXACTLY the three visual
  states the UI renders — `backed_up` (the only ✓, carrying the push-COMPLETION time),
  `pending` (amber + the honest unpushed count), and `not_backed` (the LOUD danger state
  covering `unconfigured` / `never_backed` / a freshness- or error-escalated `pending`).
- **Born-safe** (the chip macro): a render that omits `backup` entirely, and a `None`
  status (no service wired), both default to the LOUD not-backed state — never blank,
  never the green ✓. A missed context can only ever over-alarm, never read falsely safe.
- **The wiring** (through the app): every full-page render carries the live chip; a fresh
  install (`backup=None`) shows the loud banner on the capture home; a `backed_up` state
  shows the green chip and NO banner; a banked capture pushes a fresh chip out-of-band.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import jinja2
from fastapi import FastAPI

from bookkeeper_ui.api import create_app
from bookkeeper_ui.backup import (
    STATE_BACKED_UP,
    STATE_NEVER_BACKED,
    STATE_PENDING,
    STATE_UNCONFIGURED,
    BackupStatus,
)
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.schemas import BackupSignalOut
from bookkeeper_ui.statement_store import FileStatementStore
from bookkeeper_ui.web import TEMPLATES_DIR

# A fixed verified push-completion time — the sidecar's `pushed_at`, the ONLY thing a
# backed_up chip may show as its timestamp (never the attempt time).
PUSH_AT = datetime(2026, 7, 1, 14, 32, 5, tzinfo=timezone.utc)


def _status(
    state: str,
    *,
    unpushed: int = 0,
    last_push_time: datetime | None = None,
    error: str | None = None,
    escalated: bool = False,
) -> BackupStatus:
    return BackupStatus(
        state=state,
        remote_configured=state != STATE_UNCONFIGURED,
        unpushed_count=unpushed,
        last_push_time=last_push_time,
        last_error_class=error,
        escalated=escalated,
    )


# ============================================================================
# the collapse — BackupSignalOut.from_status (unit)
# ============================================================================


def test_none_status_is_born_safe_loud_not_backed():
    """No service wired / no status → the LOUD not-backed alarm, never green, never blank."""
    sig = BackupSignalOut.from_status(None)
    assert sig.state == "not_backed"
    assert sig.css_class == "not-backed"
    assert sig.loud is True
    assert sig.show_check is False
    assert sig.last_push_time is None


def test_backed_up_is_the_only_green_and_carries_completion_time():
    """backed_up is the ONLY ✓ and the ONLY state that surfaces a timestamp — the
    sidecar's verified push-COMPLETION time, propagated verbatim."""
    sig = BackupSignalOut.from_status(_status(STATE_BACKED_UP, last_push_time=PUSH_AT))
    assert sig.state == "backed_up"
    assert sig.css_class == "backed-up"
    assert sig.show_check is True
    assert sig.loud is False
    assert sig.last_push_time == PUSH_AT.isoformat()  # completion time, not the attempt time


def test_pending_is_amber_with_the_honest_count_and_no_timestamp():
    """A quiet pending: amber, the honest unpushed count, no ✓, no banner, no timestamp."""
    sig = BackupSignalOut.from_status(
        _status(STATE_PENDING, unpushed=3, last_push_time=PUSH_AT, escalated=False)
    )
    assert sig.state == "pending"
    assert sig.css_class == "pending"
    assert sig.unpushed_count == 3
    assert sig.show_check is False
    assert sig.loud is False
    assert sig.last_push_time is None  # pending shows the count, not a (stale) green time
    # Honest status, not activity (backup-spec §6, #88): "Not yet backed up", never
    # "Backing up" — a quiet/offline-stalled pending has nothing actively in flight.
    assert sig.label == "Not yet backed up"
    assert "Backing up" not in sig.label


def test_escalated_pending_becomes_loud_not_backed():
    """A freshness-escalated pending crosses into the LOUD not-backed nag class."""
    sig = BackupSignalOut.from_status(
        _status(STATE_PENDING, unpushed=2, last_push_time=PUSH_AT, escalated=True)
    )
    assert sig.state == "not_backed"
    assert sig.loud is True
    assert sig.show_check is False
    assert sig.reason == "stale"


def test_unconfigured_and_never_backed_are_loud():
    """The born-safe pre-first-push rungs both read LOUD not-backed (never green)."""
    unconf = BackupSignalOut.from_status(_status(STATE_UNCONFIGURED))
    never = BackupSignalOut.from_status(_status(STATE_NEVER_BACKED))
    for sig in (unconf, never):
        assert sig.state == "not_backed"
        assert sig.loud is True
        assert sig.show_check is False
    assert unconf.reason == "unconfigured"
    assert never.reason == "never_backed"


def test_escalating_error_classes_name_their_reason():
    """A stuck token / diverged remote escalate a pending immediately, each with its reason."""
    auth = BackupSignalOut.from_status(
        _status(STATE_PENDING, unpushed=1, last_push_time=PUSH_AT, error="auth-failed",
                escalated=True)
    )
    rejected = BackupSignalOut.from_status(
        _status(STATE_PENDING, unpushed=1, last_push_time=PUSH_AT, error="rejected",
                escalated=True)
    )
    assert (auth.state, auth.loud, auth.reason) == ("not_backed", True, "auth")
    assert (rejected.state, rejected.loud, rejected.reason) == ("not_backed", True, "rejected")


# ============================================================================
# born-safe + the three states — the chip macro (template)
# ============================================================================

_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True
)


def _render_chip(sig: BackupSignalOut | None) -> str:
    tmpl = _ENV.from_string(
        "{% from '_backup_chip.html' import backup_chip %}{{ backup_chip(sig) }}"
    )
    return tmpl.render(sig=sig)


def test_contextless_base_render_is_not_false_safe():
    """A full-page render (base.html) that FORGETS to pass `backup` must default to the
    LOUD not-backed chip — never `backed-up`, never the ✓. This is the born-safe floor:
    a missed context can only over-alarm, never read falsely green."""
    html = _ENV.get_template("base.html").render()  # deliberately NO `backup` in context
    assert 'class="status-local not-backed"' in html
    assert "backed-up" not in html
    assert "&check;" not in html  # the ✓ never renders without a real backed_up status


def test_chip_renders_the_three_state_tokens():
    """Each visual state renders its own `.status-local` modifier (→ its trust token)."""
    backed = _render_chip(BackupSignalOut.from_status(_status(STATE_BACKED_UP, last_push_time=PUSH_AT)))
    pending = _render_chip(BackupSignalOut.from_status(_status(STATE_PENDING, unpushed=4, last_push_time=PUSH_AT)))
    loud = _render_chip(BackupSignalOut.from_status(_status(STATE_UNCONFIGURED)))

    assert 'class="status-local backed-up"' in backed
    assert "&check;" in backed  # the ✓ shows only here
    assert 'class="status-local pending"' in pending
    assert "4 unsaved" in pending  # the honest unpushed count
    assert "Not yet backed up" in pending  # honest status readout, not "Backing up"
    assert "Backing up" not in pending
    assert "&check;" not in pending
    assert 'class="status-local not-backed"' in loud
    assert "&check;" not in loud


def test_backed_up_chip_shows_the_completion_timestamp():
    """The backed_up chip prints the sidecar's push-COMPLETION time (in a <time> datetime)."""
    html = _render_chip(BackupSignalOut.from_status(_status(STATE_BACKED_UP, last_push_time=PUSH_AT)))
    assert f'datetime="{PUSH_AT.isoformat()}"' in html


def test_pulse_partial_pushes_the_chip_out_of_band():
    """The capture-home pulse partial (`_intake_resolved.html`) carries a fresh chip marked
    for htmx's out-of-band swap, so a banked confirm/reject refreshes the live nav signal."""
    sig = BackupSignalOut.from_status(_status(STATE_PENDING, unpushed=1, last_push_time=PUSH_AT))
    html = _ENV.get_template("_intake_resolved.html").render(
        action="confirm", pending=0, filed_today=1, period="2026-Q2",
        confirm_period="2026-Q2", ledger_outcome="stored", backup=sig,
    )
    assert 'id="backup-chip"' in html
    assert 'hx-swap-oob="true"' in html


# ============================================================================
# the wiring — through the app (integration)
# ============================================================================


class _FakeBackup:
    """A duck-typed `BackupService` that reports a fixed status — so a full-page render can
    be driven into any of the three states without standing up a real git repo + sidecar.
    Only `status()` is exercised on the GET path; `bank`/`sweep` are inert no-ops."""

    def __init__(self, status: BackupStatus | None) -> None:
        self._status = status

    async def status(self) -> BackupStatus | None:
        return self._status

    async def bank(self, message: str) -> None:  # pragma: no cover - GET path never banks
        pass

    async def sweep(self) -> None:  # pragma: no cover - lifespan not run under ASGITransport
        pass


def _app(tmp_path: Path, examples_dir: Path, backup: object | None) -> FastAPI:
    return create_app(
        config=load_config(examples_dir / "config.json"),
        ledger_store=FileLedgerStore(tmp_path / "ledger.jsonl"),
        confirmation_store=FileConfirmationStore(tmp_path / "confirmations.jsonl"),
        statement_store=FileStatementStore(tmp_path / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(tmp_path / "reconciliations.jsonl"),
        backup=backup,
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_fresh_install_home_is_loud(tmp_path, examples_dir):
    """A fresh install (no backup wired) renders the LOUD not-backed chip AND the page-level
    banner on the capture home — an unbacked-up machine screams on the front door by default."""
    async with _client(_app(tmp_path, examples_dir, backup=None)) as client:
        html = (await client.get("/")).text
    assert 'class="status-local not-backed"' in html
    assert 'class="backup-banner loud"' in html
    assert "Your books are not backed up" in html


async def test_backed_up_home_is_green_with_no_banner(tmp_path, examples_dir):
    """A verified-backed-up state reads green in the chip and raises NO loud banner."""
    backup = _FakeBackup(_status(STATE_BACKED_UP, last_push_time=PUSH_AT))
    async with _client(_app(tmp_path, examples_dir, backup)) as client:
        html = (await client.get("/")).text
    assert 'class="status-local backed-up"' in html
    assert "backup-banner loud" not in html
    assert f'datetime="{PUSH_AT.isoformat()}"' in html  # the completion time is shown


async def test_pending_home_is_amber_with_no_banner(tmp_path, examples_dir):
    """A quiet pending reads amber with the honest count and NO loud banner."""
    backup = _FakeBackup(_status(STATE_PENDING, unpushed=2, last_push_time=PUSH_AT))
    async with _client(_app(tmp_path, examples_dir, backup)) as client:
        html = (await client.get("/")).text
    assert 'class="status-local pending"' in html
    assert "2 unsaved" in html
    assert "backup-banner loud" not in html


async def test_chip_rides_every_full_page(tmp_path, examples_dir):
    """The safe-signal is global: every full-page surface carries the live nav chip."""
    backup = _FakeBackup(_status(STATE_BACKED_UP, last_push_time=PUSH_AT))
    async with _client(_app(tmp_path, examples_dir, backup)) as client:
        for path in ("/", "/ui/import-files", "/ui/queue?period=2026-Q2",
                     "/ui/reconcile?period=2026-Q2", "/ui/ledger?period=2026-Q2"):
            html = (await client.get(path)).text
            assert 'id="backup-chip"' in html, path
            assert 'class="status-local backed-up"' in html, path
