"""Slice 6 · A (Issue #82) — the backup engine's local core: init + serialized commit.

`BackupService` turns the data dir into a **git repository the app manages for the
user** — a durable, versioned, off-machine-ready copy of the books. This module is
the *local* half only: lazily initialise the repo, and append a linear commit at
each banked write. **No network** — the push transport is Issue #83, and *all*
wiring/UI is later issues. It stays a leaf: stdlib + `asyncio`/`subprocess` only,
importing nothing from `api`/`web` (mirroring `intake_confirm.py`), so the app can
construct it — or not — with no import cycle and no behavioural coupling.

Three invariants carry the design:

- **Born-safe.** Every git call goes through `_run`, which never raises: a missing
  `git`, a permissions fault, a non-zero exit — all are caught and logged, and the
  caller sees nothing. An unconstructed (`None`) service, or one whose commits keep
  failing, leaves the app fully functional; backup is best-effort, never load-bearing.

- **Serialized, and async.** Commits run under a single `asyncio.Lock` so two
  concurrent `commit()` calls can never collide on `.git/index.lock`; the push
  transport (#83) will share this same lock. Subprocesses are spawned with
  `asyncio.create_subprocess_exec` — **never** a blocking `subprocess.run` inside an
  `async def`, which would stall the event loop and the whole request path.

- **Restorable in one command.** `config.json` lives *outside* the data dir, so a
  clone of the backup alone could not run. Before each `git add` the service copies
  the active config into the tree as `config.snapshot.json` (git dedupes it when
  unchanged, so it is cheap), making a dead-disk clone `clone → run` restorable. The
  path is passed in (`config_path`) so this leaf never guesses config's location.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo-LOCAL commit identity. Set into `.git/config` (never `--global`) so a commit
# succeeds on a machine with no global git identity — an unattended client box.
_IDENTITY_NAME = "Bookkeeper Backup"
_IDENTITY_EMAIL = "backup@bookkeeper.local"

_INITIAL_COMMIT_MESSAGE = "Initialize Bookkeeper backup repository"

# The snapshot of the out-of-tree `config.json`, written into the backup tree so a
# clone is self-contained. Not matched by any `.gitignore` pattern → always tracked.
_CONFIG_SNAPSHOT_NAME = "config.snapshot.json"

# The backup `.gitignore`, written before the first commit. Kept as an exact,
# importable constant so the tree's ignore surface is auditable in one place.
#
#   exports/*/          per-export blob folders (large receipt copies) — subfolders
#                       ONLY, so the `exports/exports.jsonl` LOG stays tracked.
#   intake_drop/        the offline intake scratch dir — transient, not a record.
#   *.tmp / *.lock      transient scratch + lock files (never part of a record).
#   .git-credentials    defensive: tokens belong in the OS keychain, never the tree
#   .netrc              (see #83/#89). No such file is ever written here; excluding
#                       them means a misconfigured helper can never leak one in.
#
# Deliberately NOT ignored: `artifacts/` (the receipt blobs — kept, and isolated so
# a future LFS `.gitattributes` rule drops in) and every `*.jsonl` store.
BACKUP_GITIGNORE = """\
# Managed by the Bookkeeper backup engine — do not edit by hand.
exports/*/
intake_drop/
*.tmp
*.lock
.git-credentials
.netrc
"""


@dataclass(frozen=True)
class _GitResult:
    """Outcome of one git subprocess. `returncode is None` ⇒ the spawn itself failed
    (git missing, OS error) — already logged; callers treat it as a hard failure."""

    returncode: int | None
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class BackupService:
    """Local git backup for a data dir: lazy init + serialized, async, best-effort commit.

    Construct one instance per data dir (the app does, once, in assembly). The lock is
    per-instance and the push transport (#83) shares it, so all writes to a given data
    dir's repo serialize through this one object.

    `remote_config` is accepted and stored for #83 (the push transport); this slice does
    nothing with the network. `config_path`, when given, is the active `config.json`
    snapshotted into the tree before each commit (the one-command-restore guarantee).
    """

    def __init__(
        self,
        data_dir: str | Path,
        remote_config: object | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        # Resolve to an ABSOLUTE path now, so every git call runs against an
        # unambiguous location regardless of the process's cwd at call time.
        self._data_dir = Path(data_dir).resolve()
        self._remote_config = remote_config
        self._config_path = Path(config_path) if config_path is not None else None
        # One lock guards this data dir's git index for the object's lifetime; #83's
        # push acquires the same lock. asyncio.Lock binds to the running loop lazily.
        self._lock = asyncio.Lock()

    async def commit(self, message: str) -> None:
        """Snapshot config, stage everything, and commit — serialized and never raising.

        A no-op (nothing changed since the last commit) is a clean success: the tree is
        already backed up. Any failure is caught and logged, so a broken backup never
        propagates into the app's write path.
        """
        async with self._lock:
            try:
                await self._ensure_repo()
                self._snapshot_config()
                await self._add_and_commit(message)
            except Exception as exc:  # final safety net — backup never raises upward.
                logger.warning("backup commit failed for %s: %s", self._data_dir, exc)

    async def _ensure_repo(self) -> None:
        """Idempotently make `data_dir` a git repo. Must be called holding `self._lock`.

        No-op if `.git` already exists. Otherwise: `git init -b main` at the resolved
        data dir, set the repo-LOCAL identity, write the `.gitignore` and config snapshot
        BEFORE the first `git add`, and make an initial commit — so a fresh repo always
        carries at least one commit.
        """
        if (self._data_dir / ".git").exists():
            return
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("backup could not create data dir %s: %s", self._data_dir, exc)
            return
        await self._run("init", "-b", "main")
        # Repo-local identity (never --global): the commit must work with no global config.
        await self._run("config", "user.name", _IDENTITY_NAME)
        await self._run("config", "user.email", _IDENTITY_EMAIL)
        self._write_gitignore()
        self._snapshot_config()
        await self._add_and_commit(_INITIAL_COMMIT_MESSAGE)

    async def _add_and_commit(self, message: str) -> None:
        """`git add -A`, then commit only if something is actually staged.

        `git diff --cached --quiet` exits 1 when there are staged changes and 0 when
        there are none — so we never call `git commit` on an empty index (which would
        error) and never create an empty commit. Nothing staged ⇒ a clean no-op.
        """
        await self._run("add", "-A")
        staged = await self._run("diff", "--cached", "--quiet")
        if staged.returncode == 1:
            await self._run("commit", "-m", message)
        # returncode 0 (nothing staged) or None (spawn error, already logged) ⇒ no commit.

    def _write_gitignore(self) -> None:
        """Write the backup `.gitignore` (best-effort; logs and returns on failure)."""
        try:
            (self._data_dir / ".gitignore").write_text(BACKUP_GITIGNORE, encoding="utf-8")
        except OSError as exc:
            logger.warning("backup could not write .gitignore in %s: %s", self._data_dir, exc)

    def _snapshot_config(self) -> None:
        """Copy the active out-of-tree `config.json` into the tree as `config.snapshot.json`.

        Best-effort: no config path configured, or a missing/unreadable source, is a
        silent skip (logged on error) — it must never abort a commit.
        """
        if self._config_path is None:
            return
        try:
            if self._config_path.is_file():
                shutil.copyfile(self._config_path, self._data_dir / _CONFIG_SNAPSHOT_NAME)
        except OSError as exc:
            logger.warning("backup could not snapshot config %s: %s", self._config_path, exc)

    async def _run(self, *args: str) -> _GitResult:
        """Run one `git` subprocess in the data dir via async exec; never raise.

        Uses `asyncio.create_subprocess_exec` (not blocking `subprocess.run`) so the
        event loop is never stalled. A failed spawn (git missing, OS error) is caught
        and returned as `returncode=None`.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(self._data_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            result = _GitResult(
                returncode=proc.returncode,
                stdout=stdout.decode("utf-8", "replace"),
                stderr=stderr.decode("utf-8", "replace"),
            )
        except Exception as exc:  # git not installed, OS-level spawn failure, etc.
            logger.warning("backup git %s failed to run in %s: %s", args[:1], self._data_dir, exc)
            return _GitResult(returncode=None, stdout="", stderr=str(exc))
        # A non-zero exit is expected in normal flow (e.g. `diff --cached --quiet`), so
        # log at debug — the caller decides what a given non-zero code means.
        if not result.ok and result.returncode is not None:
            logger.debug(
                "backup git %s exited %s in %s: %s",
                args[:1], result.returncode, self._data_dir, result.stderr.strip(),
            )
        return result
