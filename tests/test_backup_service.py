"""Slice 6 · A (#82) — `BackupService` local core: init idempotency, identity, gitignore,
linear commits, nothing-staged no-ops, serialized concurrency, async subprocess, and the
config snapshot. All git assertions read the tmp repo directly (blocking `subprocess.run`
is fine in a test); the service under test always uses async exec.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import bookkeeper_ui.backup as backup_mod
from bookkeeper_ui.backup import BACKUP_GITIGNORE, BackupService, BackupStatus


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


# ===========================================================================
# Slice 6 · B (#83) — the push transport: fire-and-forget force-with-lease,
# retry/backoff, error-collapse, verified-success sidecar.
# ===========================================================================

SIDECAR = Path(".git") / "bookkeeper_last_push"


def init_bare_remote(path: Path) -> Path:
    """A temp BARE repo to push to (the offline-safe local stand-in for the client's remote)."""
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        capture_output=True, text=True, check=True,
    )
    return path


def file_url(path: Path) -> str:
    """A credential-free `file://` remote URL — allowed by the secrets guard (non-network,
    no embedded token), exactly what the AC's bare-repo fixture needs to exercise a real push."""
    return f"file://{path.resolve()}"


async def _inited_repo(data_dir: Path) -> BackupService:
    """A BackupService whose repo is inited with a baseline commit but no remote yet — the
    caller wires `origin` next, mirroring the real order (repo exists, consultant adds remote)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    svc = BackupService(data_dir)
    await svc.commit("baseline")
    return svc


def read_sidecar(data_dir: Path) -> dict:
    return json.loads((data_dir / SIDECAR).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- verified success

async def test_bank_pushes_to_bare_remote_and_writes_verified_sidecar(tmp_path):
    """A first `bank()` to an EMPTY bare remote succeeds: the remote's `main` lands at local
    HEAD, and the sidecar records that exact sha with a real ISO completion timestamp."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "ledger.jsonl").write_text("{}\n")
    run_git(data_dir, "remote", "add", "origin", file_url(bare))

    await svc.bank("banked: import 1 transaction")
    await svc._push_task  # drive the scheduled, non-awaited push to completion

    head = run_git(data_dir, "rev-parse", "HEAD").stdout.strip()
    assert run_git(bare, "rev-parse", "main").stdout.strip() == head  # remote advanced to HEAD
    sidecar = read_sidecar(data_dir)
    assert sidecar["pushed_sha"] == head
    stamp = datetime.fromisoformat(sidecar["pushed_at"])
    assert stamp.tzinfo is not None  # a real, timezone-aware COMPLETION time
    assert svc._last_push_error_class is None


async def test_up_to_date_push_still_refreshes_sidecar(tmp_path):
    """A second bank with nothing new (HEAD already on the remote) is `everything up-to-date`
    — still verified against the remote, so the sidecar stays truthful, not stale."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "ledger.jsonl").write_text("{}\n")
    run_git(data_dir, "remote", "add", "origin", file_url(bare))

    await svc.bank("first")
    await svc._push_task
    head = run_git(data_dir, "rev-parse", "HEAD").stdout.strip()

    # No file change → the second bank's commit is a no-op; HEAD is already on the remote.
    await svc.bank("nothing new")
    await svc._push_task
    assert read_sidecar(data_dir)["pushed_sha"] == head
    assert svc._last_push_error_class is None


# ------------------------------------------------------------- off the request path

async def test_bank_awaits_commit_but_never_awaits_the_push(tmp_path, monkeypatch):
    """`bank()` returns as soon as the local commit is durable; the network push is scheduled,
    not awaited — so a slow/failing push never delays the handler response."""
    svc = await _inited_repo(tmp_path / "data")
    run_git(tmp_path / "data", "remote", "add", "origin", "https://example.invalid/x/y.git")

    gate = asyncio.Event()
    real_run = svc._run

    async def gated_run(*args, **kwargs):
        if args and args[0] == "push":
            await gate.wait()  # hold the push open
            return backup_mod._GitResult(128, "", "fatal: Authentication failed")
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", gated_run)

    (tmp_path / "data" / "x.jsonl").write_text("x\n")
    await svc.bank("banked change")  # must NOT block on the gated push

    assert svc._push_task is not None and not svc._push_task.done()
    # the local commit, by contrast, is already durable
    assert run_git(tmp_path / "data", "rev-parse", "HEAD").returncode == 0
    gate.set()
    await svc._push_task  # clean up


async def test_in_flight_push_is_coalesced_not_stacked(tmp_path, monkeypatch):
    """A second bank while a push cycle is in flight does not start a competing cycle — the
    running one already carries current HEAD."""
    svc = await _inited_repo(tmp_path / "data")
    run_git(tmp_path / "data", "remote", "add", "origin", "https://example.invalid/x/y.git")

    gate = asyncio.Event()
    real_run = svc._run

    async def gated_run(*args, **kwargs):
        if args and args[0] == "push":
            await gate.wait()
            return backup_mod._GitResult(128, "", "fatal: Authentication failed")
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", gated_run)

    (tmp_path / "data" / "a.jsonl").write_text("a\n")
    await svc.bank("first")
    task1 = svc._push_task

    (tmp_path / "data" / "b.jsonl").write_text("b\n")
    await svc.bank("second")  # in-flight push not done → coalesce
    assert svc._push_task is task1

    gate.set()
    await svc._push_task


# -------------------------------------------------------------- offline / backoff

async def test_offline_push_retries_on_backoff_then_pends(tmp_path, monkeypatch):
    """A transient (offline) failure retries on the ~2s/8s/30s schedule, then gives up for the
    cycle: no sidecar, state honestly pending, the commit still safely local, nothing raised."""
    svc = await _inited_repo(tmp_path / "data")
    run_git(tmp_path / "data", "remote", "add", "origin", "https://example.invalid/x/y.git")

    push_calls = 0
    real_run = svc._run

    async def flaky_run(*args, **kwargs):
        nonlocal push_calls
        if args and args[0] == "push":
            push_calls += 1
            return backup_mod._GitResult(
                128, "", "fatal: unable to access: Could not resolve host example.invalid"
            )
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", flaky_run)

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(backup_mod.asyncio, "sleep", fake_sleep)

    await svc._push_cycle()  # run the cycle inline for a deterministic schedule check

    assert push_calls == 4  # initial attempt + 3 retries
    assert sleeps == [2.0, 8.0, 30.0]
    assert svc._last_push_error_class == "offline"
    assert not (tmp_path / "data" / SIDECAR).exists()  # never reads backed-up
    assert commit_count(tmp_path / "data") >= 1  # commit stayed safely local


async def test_auth_failure_is_terminal_no_retry(tmp_path, monkeypatch):
    """An auth failure will not self-heal within a cycle, so it gives up immediately (no backoff),
    records the `auth-failed` class, and writes no sidecar."""
    svc = await _inited_repo(tmp_path / "data")
    run_git(tmp_path / "data", "remote", "add", "origin", "https://example.invalid/x/y.git")

    push_calls = 0
    real_run = svc._run

    async def auth_run(*args, **kwargs):
        nonlocal push_calls
        if args and args[0] == "push":
            push_calls += 1
            return backup_mod._GitResult(128, "", "fatal: Authentication failed for 'https://...'")
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", auth_run)

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(backup_mod.asyncio, "sleep", fake_sleep)

    await svc._push_cycle()

    assert push_calls == 1  # no retry
    assert sleeps == []
    assert svc._last_push_error_class == "auth-failed"
    assert not (tmp_path / "data" / SIDECAR).exists()


async def test_timeout_kills_subprocess_and_pends(tmp_path, monkeypatch):
    """A push that overruns the bounded timeout is killed and reaped (returncode None) and
    treated as offline — no sidecar, nothing raised, no zombie left holding the lock."""
    svc = await _inited_repo(tmp_path / "data")
    run_git(tmp_path / "data", "remote", "add", "origin", "https://example.invalid/x/y.git")

    real_run = svc._run

    async def timing_out_run(*args, **kwargs):
        if args and args[0] == "push":
            return backup_mod._GitResult(None, "", "timed out")  # as _run returns on timeout
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", timing_out_run)

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(backup_mod.asyncio, "sleep", fake_sleep)

    await svc._push_cycle()
    assert sleeps == [2.0, 8.0, 30.0]  # a timeout is transient → retried then pends
    assert svc._last_push_error_class == "offline"
    assert not (tmp_path / "data" / SIDECAR).exists()


# --------------------------------------------------------- diverged remote refused

async def test_diverged_remote_is_refused_not_clobbered(tmp_path):
    """A remote that moved out-of-band fails `--force-with-lease` (stale info): the remote is
    NEVER clobbered, the sidecar does not advance, and the class is `rejected`."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "a.jsonl").write_text("a\n")
    run_git(data_dir, "remote", "add", "origin", file_url(bare))

    await svc.bank("first push")
    await svc._push_task
    first_sha = run_git(data_dir, "rev-parse", "HEAD").stdout.strip()
    assert run_git(bare, "rev-parse", "main").stdout.strip() == first_sha

    # Diverge the bare remote out-of-band via a second clone → the remote moves to B.
    work = tmp_path / "work"
    subprocess.run(["git", "clone", file_url(bare), str(work)],
                   capture_output=True, text=True, check=True)
    run_git(work, "config", "user.email", "d@t")
    run_git(work, "config", "user.name", "Diverge")
    (work / "diverge.txt").write_text("x\n")
    run_git(work, "add", "-A")
    run_git(work, "commit", "-m", "divergent")
    run_git(work, "push", "origin", "main")
    diverged_sha = run_git(work, "rev-parse", "HEAD").stdout.strip()
    assert run_git(bare, "rev-parse", "main").stdout.strip() == diverged_sha

    # Local advances → C; the next bank pushes over the diverged remote and must be refused.
    (data_dir / "b.jsonl").write_text("b\n")
    await svc.bank("second push over a diverged remote")
    await svc._push_task

    assert run_git(bare, "rev-parse", "main").stdout.strip() == diverged_sha  # NOT clobbered
    assert read_sidecar(data_dir)["pushed_sha"] == first_sha  # sidecar did not advance
    assert svc._last_push_error_class == "rejected"


# ----------------------------------------------------------- unconfigured / secrets

async def test_no_remote_is_a_clean_unconfigured_noop(tmp_path):
    """With no `origin`, a push is an honest no-op — no sidecar, no error class, no exception."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "x.jsonl").write_text("x\n")

    await svc.bank("change with no remote")
    await svc._push_task

    assert not (data_dir / SIDECAR).exists()
    assert svc._last_push_error_class is None


async def test_tokenized_remote_is_refused_and_never_pushed(tmp_path, monkeypatch):
    """The secrets guard refuses a tokenized remote BEFORE any `git push` runs — so a token
    can never reach a git command line — and records no success."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    run_git(data_dir, "remote", "add", "origin",
            "https://x-access-token:ghp_secretsecret@github.com/client/books.git")

    push_attempted = False
    real_run = svc._run

    async def spy_run(*args, **kwargs):
        nonlocal push_attempted
        if args and args[0] == "push":
            push_attempted = True
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", spy_run)

    (data_dir / "x.jsonl").write_text("x\n")
    await svc.bank("change")
    await svc._push_task

    assert push_attempted is False  # refused before invoking git push
    assert not (data_dir / SIDECAR).exists()
    assert svc._last_push_error_class is None


async def test_real_push_leaves_no_secret_in_git_config(tmp_path):
    """After a real, successful push the repo's `.git/config` carries no credential — the
    service never sets a remote or writes a token; the origin URL stays credential-free."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "ledger.jsonl").write_text("{}\n")
    run_git(data_dir, "remote", "add", "origin", file_url(bare))

    await svc.bank("banked change")
    await svc._push_task
    assert (data_dir / SIDECAR).exists()  # the push really happened

    config_text = (data_dir / ".git" / "config").read_text(encoding="utf-8")
    for secret_marker in ("x-access-token", "ghp_", "password", "oauth", "token"):
        assert secret_marker not in config_text.lower()
    origin_url = run_git(data_dir, "config", "--local", "remote.origin.url").stdout.strip()
    assert urlparse(origin_url).username is None  # no embedded credentials


# ------------------------------------------------------- never mutates local history

async def test_push_never_fetches_pulls_merges_or_rebases(tmp_path, monkeypatch):
    """The transport is push-only: across a full bank+push it runs `push` (and read-only
    `ls-remote`/`rev-parse`/`remote get-url`) but NEVER pull/merge/rebase/fetch."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "a.jsonl").write_text("a\n")
    run_git(data_dir, "remote", "add", "origin", file_url(bare))

    verbs: list[str] = []
    real_run = svc._run

    async def recording_run(*args, **kwargs):
        verbs.append(args[0] if args else "")
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", recording_run)

    await svc.bank("change")
    await svc._push_task

    assert "push" in verbs
    for forbidden in ("pull", "merge", "rebase", "fetch"):
        assert forbidden not in verbs, f"backup must never run git {forbidden}"


# ---------------------------------------------------------------- born-safe cycle

async def test_push_cycle_never_lets_an_exception_escape(tmp_path, monkeypatch):
    """Even an unexpected error inside an attempt is swallowed — the cycle runs as a bare task,
    so an escape would surface as an unhandled-exception warning and (worse) a lost lock."""
    svc = await _inited_repo(tmp_path / "data")

    async def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(svc, "_push_once", boom)
    await svc._push_cycle()  # must not raise


# ===========================================================================
# Slice 6 · C (#84) — BackupStatus: truthful state computed live from git on the
# sidecar-sha basis (four-state ladder + freshness escalation). Every ladder rung is
# built from constructed git states; born-safe/never-pushed never reads green; the
# `@{u}` fatal is avoided; freshness escalation fires on stale-success-while-unbacked.
# ===========================================================================


def iso_days_ago(days: float) -> str:
    """An ISO, timezone-aware completion timestamp `days` in the past — for a stale sidecar."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def write_sidecar(data_dir: Path, pushed_sha: str, pushed_at: str) -> None:
    """Write a verified-push sidecar directly, so a ladder/freshness state can be constructed
    deterministically without depending on a real network push or on wall-clock timing."""
    payload = json.dumps({"pushed_sha": pushed_sha, "pushed_at": pushed_at})
    (data_dir / SIDECAR).write_text(payload, encoding="utf-8")


def head_sha(data_dir: Path) -> str:
    return run_git(data_dir, "rev-parse", "HEAD").stdout.strip()


# --------------------------------------------------------------------- ladder rungs

async def test_status_unconfigured_when_not_inited(tmp_path):
    """A not-yet-inited data dir reads `unconfigured` and raises nothing — the born-safe
    default, never an exception."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()  # exists but no `git init`

    status = await BackupService(data_dir).status()

    assert isinstance(status, BackupStatus)
    assert status.state == backup_mod.STATE_UNCONFIGURED
    assert status.remote_configured is False
    assert status.unpushed_count == 0
    assert status.last_push_time is None
    assert status.escalated is False


async def test_status_unconfigured_when_inited_but_no_remote(tmp_path):
    """A repo with commits but NO `origin` remote reads `unconfigured` — no remote outweighs
    having local history."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)

    status = await svc.status()
    assert status.state == backup_mod.STATE_UNCONFIGURED
    assert status.remote_configured is False


async def test_status_never_backed_when_remote_but_no_sidecar(tmp_path):
    """A remote is configured but no verified push has ever landed → `never_backed`; the
    commits are all counted unpushed, and it can NEVER read green."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")

    status = await svc.status()
    assert status.state == backup_mod.STATE_NEVER_BACKED
    assert status.state != backup_mod.STATE_BACKED_UP  # born-safe: never green without a push
    assert status.remote_configured is True
    assert status.unpushed_count == 1  # the baseline commit is unpushed
    assert status.last_push_time is None


async def test_status_backed_up_after_verified_push(tmp_path):
    """After a real, verified push (HEAD == pushed_sha) the status is `backed_up`: green,
    zero unpushed, a real aware completion timestamp, no error."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "ledger.jsonl").write_text("{}\n")
    run_git(data_dir, "remote", "add", "origin", file_url(bare))

    await svc.bank("banked change")
    await svc._push_task

    status = await svc.status()
    assert status.state == backup_mod.STATE_BACKED_UP
    assert status.remote_configured is True
    assert status.unpushed_count == 0
    assert status.last_push_time is not None and status.last_push_time.tzinfo is not None
    assert status.last_error_class is None
    assert status.escalated is False


async def test_status_pending_when_head_ahead_of_pushed_sha(tmp_path):
    """A commit made after the last verified push leaves HEAD ahead of the pushed sha →
    `pending` with an honest unpushed count, and (recent push) not escalated."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "ledger.jsonl").write_text("{}\n")
    run_git(data_dir, "remote", "add", "origin", file_url(bare))
    await svc.bank("first")
    await svc._push_task

    # Two more local commits, not pushed → HEAD is two ahead of the verified sha.
    (data_dir / "a.jsonl").write_text("a\n")
    await svc.commit("second, local only")
    (data_dir / "b.jsonl").write_text("b\n")
    await svc.commit("third, local only")

    status = await svc.status()
    assert status.state == backup_mod.STATE_PENDING
    assert status.unpushed_count == 2
    assert status.state != backup_mod.STATE_BACKED_UP  # committed-but-unpushed is never green
    assert status.escalated is False  # the last push is recent


async def test_committed_but_unpushed_can_never_read_backed_up(tmp_path):
    """The green invariant: even with a remote and a sidecar present, a HEAD ahead of the
    pushed sha reads `pending`, never `backed_up`."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    base = head_sha(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    write_sidecar(data_dir, base, iso_days_ago(0))  # a fresh verified push of the baseline

    (data_dir / "later.jsonl").write_text("x\n")
    await svc.commit("a banked change after the push")

    status = await svc.status()
    assert status.state == backup_mod.STATE_PENDING
    assert status.state != backup_mod.STATE_BACKED_UP


# ------------------------------------------------------- upstream-independent counting

async def test_unpushed_count_is_upstream_independent_never_uses_at_u(tmp_path, monkeypatch):
    """The unpushed count is computed against the SIDECAR sha, never `@{u}` (which fatals with
    no upstream). No tracking branch is ever set here, so `@{u}` would exit 128 — status must
    still return a correct count without any `@{u}`/`@{upstream}` revspec."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    base = head_sha(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    write_sidecar(data_dir, base, iso_days_ago(0))
    (data_dir / "new.jsonl").write_text("x\n")
    await svc.commit("second")

    seen_args: list[tuple] = []
    real_run = svc._run

    async def recording_run(*args, **kwargs):
        seen_args.append(args)
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", recording_run)

    status = await svc.status()

    assert status.state == backup_mod.STATE_PENDING
    assert status.unpushed_count == 1
    joined = " ".join(a for call in seen_args for a in call)
    assert "@{u}" not in joined and "@{upstream}" not in joined
    # the count really was taken against the sidecar sha
    assert any("rev-list" in call and f"{base}..HEAD" in call for call in seen_args)


async def test_unknown_sidecar_sha_falls_back_to_total_never_fatals(tmp_path):
    """A sidecar sha not in local history (rewritten/foreign) makes `<sha>..HEAD` fatal; the
    count folds back to the total-commit count instead of raising or fatalling."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    write_sidecar(data_dir, "0" * 40, iso_days_ago(0))  # a sha this repo has never seen

    status = await svc.status()
    assert status.unpushed_count == commit_count(data_dir)  # folded back to all commits


# --------------------------------------------------------------- freshness escalation

async def test_freshness_escalation_fires_when_pending_push_is_stale(tmp_path):
    """A `pending` state whose verified push is older than the (default 3-day) threshold
    escalates to the LOUD nag class — stale-success-while-unbacked surfaces loudly."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    base = head_sha(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    write_sidecar(data_dir, base, iso_days_ago(10))  # last verified push was 10 days ago
    (data_dir / "new.jsonl").write_text("x\n")
    await svc.commit("banked, unpushed for days")

    status = await svc.status()
    assert status.state == backup_mod.STATE_PENDING
    assert status.escalated is True


async def test_recent_pending_push_does_not_escalate(tmp_path):
    """A `pending` state within the freshness window stays quiet amber — not escalated."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    base = head_sha(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    write_sidecar(data_dir, base, iso_days_ago(1))  # well within the 3-day window
    (data_dir / "new.jsonl").write_text("x\n")
    await svc.commit("banked recently")

    status = await svc.status()
    assert status.state == backup_mod.STATE_PENDING
    assert status.escalated is False


async def test_terminal_push_error_escalates_pending_immediately(tmp_path):
    """A non-retryable error (`auth-failed`/`rejected`) escalates a `pending` state at once,
    without waiting for the freshness clock — an expired token surfaces loudly now."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    base = head_sha(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    write_sidecar(data_dir, base, iso_days_ago(0))  # last push was seconds ago (fresh)
    (data_dir / "new.jsonl").write_text("x\n")
    await svc.commit("banked, then the token was revoked")

    for terminal in ("auth-failed", "rejected"):
        svc._last_push_error_class = terminal
        status = await svc.status()
        assert status.state == backup_mod.STATE_PENDING
        assert status.escalated is True, f"{terminal} must escalate immediately"
        assert status.last_error_class == terminal


async def test_offline_error_alone_does_not_escalate(tmp_path):
    """A transient `offline` error does NOT escalate on its own — only elapsed time does, so an
    off-grid contractor is given the full freshness window before being nagged."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    base = head_sha(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    write_sidecar(data_dir, base, iso_days_ago(1))  # recent push, just briefly offline since
    (data_dir / "new.jsonl").write_text("x\n")
    await svc.commit("banked while briefly offline")
    svc._last_push_error_class = "offline"

    status = await svc.status()
    assert status.state == backup_mod.STATE_PENDING
    assert status.escalated is False


async def test_non_pending_states_never_escalate(tmp_path):
    """Escalation is scoped to `pending`: a `never_backed` state, even with a terminal push
    error recorded, does not set `escalated` — its own rung already carries the UI treatment."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    svc._last_push_error_class = "auth-failed"

    status = await svc.status()
    assert status.state == backup_mod.STATE_NEVER_BACKED
    assert status.escalated is False


# --------------------------------------------------------------- corrupt sidecar / fields

async def test_corrupt_sidecar_is_treated_as_never_backed(tmp_path):
    """A malformed sidecar is treated as "no verified push" → `never_backed`, never green — a
    corrupt record can't be read as a successful backup."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    (data_dir / SIDECAR).write_text("not valid json{", encoding="utf-8")

    status = await svc.status()
    assert status.state == backup_mod.STATE_NEVER_BACKED
    assert status.last_push_time is None
    assert status.state != backup_mod.STATE_BACKED_UP


async def test_sidecar_missing_timestamp_never_reads_green(tmp_path):
    """`backed_up` requires a REAL timestamp: a sidecar carrying a valid sha but no parseable
    `pushed_at` is rejected as unverified → `never_backed`, not green."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    base = head_sha(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    (data_dir / SIDECAR).write_text(json.dumps({"pushed_sha": base}), encoding="utf-8")

    status = await svc.status()
    assert status.state == backup_mod.STATE_NEVER_BACKED
    assert status.state != backup_mod.STATE_BACKED_UP


async def test_status_surfaces_last_error_class_and_completion_time(tmp_path):
    """The raw fields are wired through: `last_error_class` mirrors the service's last collapsed
    class, and `last_push_time` is the sidecar's push-completion timestamp."""
    bare = init_bare_remote(tmp_path / "remote.git")
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    (data_dir / "ledger.jsonl").write_text("{}\n")
    run_git(data_dir, "remote", "add", "origin", file_url(bare))
    await svc.bank("first")
    await svc._push_task

    recorded = read_sidecar(data_dir)["pushed_at"]
    status = await svc.status()
    assert status.last_push_time == datetime.fromisoformat(recorded)
    assert status.last_error_class is None  # success cleared it


# --------------------------------------------------------------- env-configurable threshold

def test_freshness_threshold_default_is_three_days():
    """With no env override the threshold is the DECISION's 3 days (not the proposed 24h)."""
    assert backup_mod._DEFAULT_FRESHNESS_THRESHOLD == timedelta(days=3)


def test_freshness_threshold_is_env_configurable(monkeypatch):
    """`BOOKKEEPER_BACKUP_FRESHNESS_DAYS` overrides the default (whole or fractional days);
    a missing / non-numeric / non-positive value falls back cleanly to 3 days."""
    monkeypatch.delenv("BOOKKEEPER_BACKUP_FRESHNESS_DAYS", raising=False)
    assert backup_mod._read_freshness_threshold_from_env() == timedelta(days=3)

    monkeypatch.setenv("BOOKKEEPER_BACKUP_FRESHNESS_DAYS", "0.5")
    assert backup_mod._read_freshness_threshold_from_env() == timedelta(days=0.5)

    for bad in ("not-a-number", "0", "-2"):
        monkeypatch.setenv("BOOKKEEPER_BACKUP_FRESHNESS_DAYS", bad)
        assert backup_mod._read_freshness_threshold_from_env() == timedelta(days=3)


async def test_env_threshold_shortens_escalation_window(tmp_path, monkeypatch):
    """A shortened env threshold escalates a pending state that a longer one would leave quiet —
    the constructor reads the env var (no explicit arg), proving it's genuinely env-driven."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BOOKKEEPER_BACKUP_FRESHNESS_DAYS", "0.5")
    svc = BackupService(data_dir)  # threshold comes from the env, not an argument
    data_dir.mkdir(parents=True, exist_ok=True)
    await svc.commit("baseline")
    base = head_sha(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    write_sidecar(data_dir, base, iso_days_ago(1))  # 1 day old: under 3d, but over 0.5d
    (data_dir / "new.jsonl").write_text("x\n")
    await svc.commit("banked")

    status = await svc.status()
    assert status.state == backup_mod.STATE_PENDING
    assert status.escalated is True  # 1 day > the 0.5-day env threshold


async def test_explicit_threshold_argument_overrides_env(tmp_path, monkeypatch):
    """An explicit `freshness_threshold` argument wins over the env var — the test-friendly
    deterministic override."""
    monkeypatch.setenv("BOOKKEEPER_BACKUP_FRESHNESS_DAYS", "0.5")
    svc = BackupService(tmp_path / "data", freshness_threshold=timedelta(days=30))
    assert svc._freshness_threshold == timedelta(days=30)


# ------------------------------------------------------------------- read-only guarantee

async def test_status_is_read_only_and_never_mutates_history(tmp_path, monkeypatch):
    """`status()` runs only non-mutating git queries — never add/commit/push/fetch/pull/merge/
    rebase/init — so it can never change history or collide with a write."""
    data_dir = tmp_path / "data"
    svc = await _inited_repo(data_dir)
    run_git(data_dir, "remote", "add", "origin", "https://example.invalid/x/y.git")
    before = commit_count(data_dir)

    verbs: list[str] = []
    real_run = svc._run

    async def recording_run(*args, **kwargs):
        verbs.append(args[0] if args else "")
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(svc, "_run", recording_run)

    await svc.status()

    assert commit_count(data_dir) == before
    assert set(verbs) <= {"remote", "rev-list"}  # only read-only queries
    for forbidden in ("add", "commit", "push", "fetch", "pull", "merge", "rebase", "init"):
        assert forbidden not in verbs, f"status must never run git {forbidden}"
