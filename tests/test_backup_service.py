"""Slice 6 · A (#82) — `BackupService` local core: init idempotency, identity, gitignore,
linear commits, nothing-staged no-ops, serialized concurrency, async subprocess, and the
config snapshot. All git assertions read the tmp repo directly (blocking `subprocess.run`
is fine in a test); the service under test always uses async exec.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import bookkeeper_ui.backup as backup_mod
from bookkeeper_ui.backup import BACKUP_GITIGNORE, BackupService


def run_git(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Read-only git query against the tmp backup repo (test-side; blocking is fine)."""
    return subprocess.run(
        ["git", "-C", str(data_dir), *args],
        capture_output=True,
        text=True,
    )


def commit_count(data_dir: Path) -> int:
    out = run_git(data_dir, "rev-list", "--count", "HEAD")
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def tracked_files(data_dir: Path) -> set[str]:
    out = run_git(data_dir, "ls-files")
    return set(out.stdout.split())


# --------------------------------------------------------------------------- init

async def test_commit_inits_repo_on_main_with_gitignore_and_initial_commit(tmp_path):
    """A first commit lazily inits: `.git` exists, branch is `main`, `.gitignore` is
    tracked, and there is at least one commit."""
    svc = BackupService(tmp_path)
    await svc.commit("first backup")

    assert (tmp_path / ".git").is_dir()
    head = run_git(tmp_path, "symbolic-ref", "--short", "HEAD")
    assert head.stdout.strip() == "main"
    assert (tmp_path / ".gitignore").is_file()
    assert ".gitignore" in tracked_files(tmp_path)
    assert commit_count(tmp_path) >= 1


async def test_ensure_repo_is_idempotent(tmp_path):
    """No reinit / no empty commit once `.git` exists — with no data change between two
    commits, and even across a second service instance, history does not grow."""
    svc = BackupService(tmp_path)
    await svc.commit("init")
    first_head = run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    await svc.commit("again, nothing changed")
    # A fresh instance on the SAME dir must also not reinit or add an empty commit.
    await BackupService(tmp_path).commit("third instance, still nothing")

    assert run_git(tmp_path, "rev-parse", "HEAD").stdout.strip() == first_head
    assert commit_count(tmp_path) == 1


async def test_resolves_data_dir_to_absolute(tmp_path, monkeypatch):
    """The repo inits at the resolved absolute data dir regardless of the process cwd —
    a relative `data_dir` must not init against wherever the loop happens to be."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "books").mkdir()
    svc = BackupService("books")  # relative
    await svc.commit("init")
    assert (tmp_path / "books" / ".git").is_dir()


# ----------------------------------------------------------------------- identity

async def test_commit_works_with_no_global_git_config(tmp_path, monkeypatch):
    """Repo-LOCAL identity is set, so a commit succeeds on a machine with no global/system
    git identity — and the identity lives in local config, never global."""
    # Disable global + system config so ONLY the repo-local identity can satisfy commit.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    svc = BackupService(tmp_path)
    await svc.commit("needs an identity")

    assert commit_count(tmp_path) == 1  # the commit actually happened
    name = run_git(tmp_path, "config", "--local", "user.name")
    email = run_git(tmp_path, "config", "--local", "user.email")
    assert name.stdout.strip() == "Bookkeeper Backup"
    assert email.stdout.strip() == "backup@bookkeeper.local"


# ----------------------------------------------------------------------- gitignore

async def test_gitignore_contents_exact(tmp_path):
    """The written `.gitignore` matches the exact importable constant, carries every
    required pattern, and never ignores `artifacts/` or the `exports/exports.jsonl` log."""
    await BackupService(tmp_path).commit("init")
    written = (tmp_path / ".gitignore").read_text(encoding="utf-8")

    assert written == BACKUP_GITIGNORE
    lines = written.splitlines()
    for required in ("exports/*/", "intake_drop/", "*.tmp", "*.lock",
                     ".git-credentials", ".netrc"):
        assert required in lines, f"missing gitignore pattern: {required}"
    # Must NOT blanket-ignore artifacts or all of exports/.
    assert "artifacts/" not in lines
    assert "exports/" not in lines


async def test_gitignore_functional_selection(tmp_path):
    """Functionally: subfolder blobs / intake_drop / *.tmp / *.lock / credential files are
    ignored, while the exports LOG, artifacts blobs, and jsonl stores are tracked."""
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "exports.jsonl").write_text("{}\n")
    (tmp_path / "exports" / "2026-Q1").mkdir()
    (tmp_path / "exports" / "2026-Q1" / "package.pdf").write_bytes(b"blob")
    (tmp_path / "intake_drop").mkdir()
    (tmp_path / "intake_drop" / "scan.png").write_bytes(b"x")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "receipt-1").write_bytes(b"r")
    (tmp_path / "ledger.jsonl").write_text("{}\n")
    (tmp_path / "scratch.tmp").write_text("x")
    (tmp_path / "a.lock").write_text("x")
    (tmp_path / ".git-credentials").write_text("secret")

    await BackupService(tmp_path).commit("mixed tree")
    tracked = tracked_files(tmp_path)

    assert "exports/exports.jsonl" in tracked
    assert "artifacts/receipt-1" in tracked
    assert "ledger.jsonl" in tracked
    assert "exports/2026-Q1/package.pdf" not in tracked
    assert "intake_drop/scan.png" not in tracked
    assert "scratch.tmp" not in tracked
    assert "a.lock" not in tracked
    assert ".git-credentials" not in tracked


# ------------------------------------------------------------------------- commits

async def test_commits_form_linear_history(tmp_path):
    """Each banked change appends exactly one commit; the history is strictly linear
    (no merges). The baseline init commit is taken first, then each change is a delta —
    mirroring the real flow where startup establishes the baseline before banked writes."""
    svc = BackupService(tmp_path)
    await svc.commit("baseline")  # inits + the single initial commit
    base = commit_count(tmp_path)
    assert base == 1

    for i in range(3):
        (tmp_path / f"file-{i}.jsonl").write_text(f"row {i}\n")
        await svc.commit(f"backup {i}")
        assert commit_count(tmp_path) == base + i + 1  # one commit per distinct change

    assert run_git(tmp_path, "rev-list", "--merges", "HEAD").stdout.strip() == ""
    assert tracked_files(tmp_path) >= {"file-0.jsonl", "file-1.jsonl", "file-2.jsonl"}


async def test_nothing_staged_is_a_clean_noop(tmp_path):
    """A commit with nothing changed adds no commit and raises nothing — not an empty
    commit, not an error."""
    svc = BackupService(tmp_path)
    await svc.commit("init")
    before = commit_count(tmp_path)
    await svc.commit("no changes since last time")
    assert commit_count(tmp_path) == before


async def test_concurrent_commits_serialize_without_index_lock_collision(tmp_path):
    """Many concurrent `commit()` calls serialize through the lock: all succeed, every
    file lands, no `.git/index.lock` is left behind, and history stays linear."""
    svc = BackupService(tmp_path)

    async def write_then_commit(i: int) -> None:
        (tmp_path / f"c{i}.jsonl").write_text(f"row {i}\n")
        await svc.commit(f"concurrent {i}")

    await asyncio.gather(*(write_then_commit(i) for i in range(10)))

    assert not (tmp_path / ".git" / "index.lock").exists()
    assert run_git(tmp_path, "rev-list", "--merges", "HEAD").stdout.strip() == ""
    assert tracked_files(tmp_path) >= {f"c{i}.jsonl" for i in range(10)}


# ---------------------------------------------------------------- async + resilience

async def test_commit_uses_async_subprocess_never_blocking_run(tmp_path, monkeypatch):
    """Git is spawned via `asyncio.create_subprocess_exec`, never a blocking
    `subprocess.run` inside the async path."""
    real = asyncio.create_subprocess_exec
    calls: list[tuple] = []

    async def spy(*args, **kwargs):
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(backup_mod.asyncio, "create_subprocess_exec", spy)

    def boom(*_a, **_k):
        raise AssertionError("blocking subprocess.run used in async backup path")

    monkeypatch.setattr(subprocess, "run", boom)

    (tmp_path / "ledger.jsonl").write_text("{}\n")
    await BackupService(tmp_path).commit("async only")

    assert calls, "expected async create_subprocess_exec to be used"
    assert all(a[0] == "git" for a in calls)


async def test_commit_never_raises_when_git_spawn_fails(tmp_path, monkeypatch):
    """A spawn failure (e.g. git not installed) is caught and logged — never raised."""
    async def fail(*_a, **_k):
        raise OSError("git not found")

    monkeypatch.setattr(backup_mod.asyncio, "create_subprocess_exec", fail)

    # Must not raise, and must not leave a broken partial repo assertion for the caller.
    await BackupService(tmp_path).commit("git is missing")  # no exception == pass


# ------------------------------------------------------------------- config snapshot

async def test_config_snapshot_written_and_committed(tmp_path):
    """The out-of-tree `config.json` is snapshotted into the tree as `config.snapshot.json`
    (matching content) and committed — the one-command-restore guarantee."""
    config_path = tmp_path / "config.json"
    config_path.write_text('{"tax_regime": "HST"}\n', encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    svc = BackupService(data_dir, config_path=config_path)
    await svc.commit("with config snapshot")

    snapshot = data_dir / "config.snapshot.json"
    assert snapshot.is_file()
    assert snapshot.read_text(encoding="utf-8") == '{"tax_regime": "HST"}\n'
    assert "config.snapshot.json" in tracked_files(data_dir)


async def test_config_snapshot_refreshes_on_change(tmp_path):
    """A changed config produces an updated snapshot and a new commit; git dedupes an
    unchanged one into a no-op."""
    config_path = tmp_path / "config.json"
    config_path.write_text('{"v": 1}\n', encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    svc = BackupService(data_dir, config_path=config_path)

    await svc.commit("v1")
    after_v1 = commit_count(data_dir)
    await svc.commit("v1 again")  # unchanged config → no-op
    assert commit_count(data_dir) == after_v1

    config_path.write_text('{"v": 2}\n', encoding="utf-8")
    await svc.commit("v2")
    assert commit_count(data_dir) == after_v1 + 1
    assert (data_dir / "config.snapshot.json").read_text(encoding="utf-8") == '{"v": 2}\n'


async def test_no_config_path_means_no_snapshot(tmp_path):
    """With no config path configured, no snapshot file appears — the feature is opt-in."""
    await BackupService(tmp_path).commit("no config configured")
    assert not (tmp_path / "config.snapshot.json").exists()
