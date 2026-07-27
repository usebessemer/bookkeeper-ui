"""Slice 6 · #86 — the startup/idle backup sweep: launch re-verification, crash-orphan
recovery, idle-flush, and the app's periodic lifespan task.

Two layers under test:

- `BackupService.sweep()` — the discrete self-heal operation (commit an orphan → re-verify
  unpushed against the verified sidecar → coalesced flush), driven directly like the rest of
  `test_backup_service`. Git assertions read the tmp repo directly (blocking `subprocess.run`
  is fine test-side); the service always uses async exec, and the scheduled push is drained
  via `svc._push_task` exactly as the push-transport tests do.
- the `create_app` lifespan — that startup runs one launch sweep inline, that a periodic sweep
  then fires, that shutdown cancels it cleanly, and that `backup is None` is fully born-inert.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import bookkeeper_ui.api as api_mod
import bookkeeper_ui.backup as backup_mod
from bookkeeper_ui.api import create_app
from bookkeeper_ui.backup import BackupService
from bookkeeper_ui.confirmations import FileConfirmationStore
from bookkeeper_ui.config_loader import load_config
from bookkeeper_ui.ledger_store import FileLedgerStore
from bookkeeper_ui.reconciliations import FileReconciliationStore
from bookkeeper_ui.statement_store import FileStatementStore

SIDECAR = Path(".git") / "bookkeeper_last_push"


# --- git inspection + fixtures (test-side; blocking is fine) -----------------


def run_git(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(data_dir), *args], capture_output=True, text=True)


def commit_count(data_dir: Path) -> int:
    out = run_git(data_dir, "rev-list", "--count", "HEAD")
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def head_sha(data_dir: Path) -> str:
    return run_git(data_dir, "rev-parse", "HEAD").stdout.strip()


def head_subject(data_dir: Path) -> str:
    return run_git(data_dir, "log", "-1", "--format=%s").stdout.strip()


def tracked_files(data_dir: Path) -> set[str]:
    return set(run_git(data_dir, "ls-files").stdout.split())


def init_bare_remote(path: Path) -> Path:
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)],
                   capture_output=True, text=True, check=True)
    return path


def file_url(path: Path) -> str:
    return f"file://{path.resolve()}"


def write_sidecar(data_dir: Path, sha: str, when: datetime) -> None:
    """Forge a verified-push sidecar (`sha` pushed at `when`) — the stand-in for a prior
    session's verified push, so this session launches reading a real `backed_up`/green."""
    (data_dir / SIDECAR).write_text(
        json.dumps({"pushed_sha": sha, "pushed_at": when.isoformat()}), encoding="utf-8"
    )


def read_sidecar(data_dir: Path) -> dict:
    return json.loads((data_dir / SIDECAR).read_text(encoding="utf-8"))


async def _inited_repo(data_dir: Path) -> BackupService:
    """A BackupService whose repo is inited with one baseline commit and no remote yet — the
    caller wires `origin` next, mirroring the real order (repo exists, consultant adds remote)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    svc = BackupService(data_dir)
    await svc.commit("baseline")
    return svc


async def _settle(backup: BackupService | None) -> None:
    """Drain the last scheduled push cycle (a fast no-op with no reachable remote) so no pending
    task lingers into loop teardown — the `test_backup_wiring` hygiene, reused here."""
    if backup is not None and backup._push_task is not None:
        try:
            await backup._push_task
        except Exception:
            pass


# ============================================================================
# sweep() — idle-session flush (a recovered network self-heals with no bank)
# ============================================================================


async def test_sweep_flushes_an_idle_unbacked_session(tmp_path):
    """A session that ended with committed-but-unpushed work (no verified sidecar), then a
    reachable `origin`, flushes on a sweep with NO banked action: the remote advances to HEAD
    and the verified sidecar is written — the recovered-network self-heal."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "ledger.jsonl").write_text("{}\n")
    await svc.commit("banked: import 1 transaction")  # an unpushed local commit
    run_git(data_dir, "remote", "add", "origin", file_url(bare))
    assert not (data_dir / SIDECAR).exists()  # never verified-pushed → unbacked

    await svc.sweep()          # no bank — the sweep alone must flush it
    await svc._push_task       # drive the scheduled, non-awaited push to completion

    head = head_sha(data_dir)
    assert run_git(bare, "rev-parse", "main").stdout.strip() == head  # remote flushed to HEAD
    assert read_sidecar(data_dir)["pushed_sha"] == head               # verified sidecar written


# ============================================================================
# sweep() — launch re-verification reverts a stale green to pending
# ============================================================================


async def test_sweep_reverts_a_stale_green_to_pending(tmp_path, monkeypatch):
    """A HEAD level with the verified sidecar reads `backed_up` even when the *worktree* is
    dirty (status counts commits, not the tree) — a stale green a crash can leave. The launch
    sweep commits that orphan, advancing HEAD past the sidecar, so status honestly reverts to
    `pending`. The remote is unreachable here, so the flush can't re-green it (that path is the
    idle-flush test's job) — this isolates the re-verification."""
    monkeypatch.setattr(backup_mod, "_PUSH_BACKOFF_SCHEDULE", ())  # doomed push = one fast attempt

    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    # A configured-but-unreachable remote: `remote get-url` succeeds (→ remote_configured), the
    # push always fails offline, so a stale green can be observed and can't be silently re-greened.
    run_git(data_dir, "remote", "add", "origin", "file:///nonexistent/remote.git")
    write_sidecar(data_dir, head_sha(data_dir), datetime.now(timezone.utc))  # green: sidecar == HEAD

    before = await svc.status()
    assert before.state == backup_mod.STATE_BACKED_UP  # green while HEAD == sidecar…

    (data_dir / "ledger.jsonl").write_text("uncommitted crash-orphan\n")  # …but the tree is dirty
    stale = await svc.status()
    assert stale.state == backup_mod.STATE_BACKED_UP   # STALE green: dirty tree still reads green

    await svc.sweep()
    if svc._push_task is not None:
        await svc._push_task  # drain the doomed cycle (sidecar cannot advance → stays pending)

    after = await svc.status()
    assert after.state == backup_mod.STATE_PENDING     # re-verified: the stale green reverts
    assert after.unpushed_count == 1


# ============================================================================
# sweep() — a crash-orphan is committed on the next start
# ============================================================================


async def test_sweep_commits_a_crash_orphan(tmp_path):
    """An uncommitted change a crash-mid-write left in the tree is committed by the sweep under
    its own recovery subject (NOT a `banked:` subject — it is not a banked business event)."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "confirmations.jsonl").write_text("orphaned by a crash mid-write\n")
    before = commit_count(data_dir)

    await svc.sweep()

    assert commit_count(data_dir) == before + 1
    assert head_subject(data_dir) == backup_mod._SWEEP_COMMIT_MESSAGE
    assert not head_subject(data_dir).startswith("banked:")
    assert "confirmations.jsonl" in tracked_files(data_dir)
    await _settle(svc)  # the sweep scheduled an (unconfigured) push — drain it


async def test_sweep_on_a_clean_backed_up_repo_commits_nothing_and_pushes_nothing(tmp_path):
    """A clean, fully-backed-up repo (sidecar == HEAD) sweeps to a pure no-op: no new commit,
    and — because nothing is unpushed — no new push cycle is scheduled (coalescing/quiet)."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    run_git(data_dir, "remote", "add", "origin", file_url(bare))
    await svc.bank("banked: import 1 transaction")
    await svc._push_task  # push verified → sidecar == HEAD, tree clean
    settled_task = svc._push_task
    before = commit_count(data_dir)

    await svc.sweep()

    assert commit_count(data_dir) == before          # no new commit on a clean tree
    assert svc._push_task is settled_task            # no NEW push scheduled — nothing unpushed


# ============================================================================
# sweep() — no-op when the repo is not inited (unconfigured / never banked)
# ============================================================================


async def test_sweep_is_a_noop_when_repo_not_inited(tmp_path):
    """With nothing ever banked (no `.git`), the sweep is a pure no-op: it does NOT init the
    repo (that is the first bank's job) and schedules no push — an unconfigured app stays so."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    svc = BackupService(data_dir)

    await svc.sweep()

    assert not (data_dir / ".git").exists()  # never created a repo the app hadn't asked for
    assert svc._push_task is None


# ============================================================================
# the create_app lifespan — launch sweep · periodic sweep · born-inert
# ============================================================================


def _minimal_app(data_dir: Path, *, backup: BackupService | None):
    """`create_app` over just the required stores in one data dir — enough to drive the
    lifespan without standing up every Slice-3/4/5 store."""
    config = load_config(Path("examples/config.json"))
    return create_app(
        config=config,
        ledger_store=FileLedgerStore(data_dir / "ledger.jsonl"),
        confirmation_store=FileConfirmationStore(data_dir / "confirmations.jsonl"),
        statement_store=FileStatementStore(data_dir / "statements.jsonl"),
        reconciliation_store=FileReconciliationStore(data_dir / "reconciliations.jsonl"),
        backup=backup,
    )


async def test_lifespan_launch_sweep_commits_a_crash_orphan(tmp_path):
    """App startup runs the launch sweep inline: an orphan left in the tree is committed before
    the first request is served."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "ledger.jsonl").write_text("orphaned before launch\n")
    app = _minimal_app(data_dir, backup=svc)
    before = commit_count(data_dir)

    async with app.router.lifespan_context(app):
        pass  # entering runs startup (the launch sweep); exiting runs a clean shutdown

    assert commit_count(data_dir) == before + 1
    assert head_subject(data_dir) == backup_mod._SWEEP_COMMIT_MESSAGE
    await _settle(svc)  # the launch sweep scheduled an (unconfigured) push — drain it


async def test_lifespan_runs_a_periodic_sweep_then_cancels_it_on_shutdown(tmp_path, monkeypatch):
    """After the launch sweep, a periodic sweep fires on the configured interval; leaving the
    lifespan cancels the task cleanly (no lingering task, no raised error)."""
    monkeypatch.setattr(api_mod, "_BACKUP_SWEEP_INTERVAL_SECONDS", 0.01)
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)

    calls = 0
    real_sweep = svc.sweep

    async def counting_sweep() -> None:
        nonlocal calls
        calls += 1
        await real_sweep()

    monkeypatch.setattr(svc, "sweep", counting_sweep)
    app = _minimal_app(data_dir, backup=svc)

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)  # ≥1 launch + several periodic ticks at a 0.01s interval

    assert calls >= 2  # the launch sweep AND at least one periodic tick fired
    await _settle(svc)  # drain the last sweep's (unconfigured) push cycle


async def test_lifespan_is_born_inert_with_no_backup(tmp_path):
    """With `backup=None` the lifespan starts and stops with no work: no sweep, no task, and —
    crucially — no `.git`, so the app is exactly pre-feature."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _minimal_app(data_dir, backup=None)

    async with app.router.lifespan_context(app):
        pass

    assert not (data_dir / ".git").exists()
