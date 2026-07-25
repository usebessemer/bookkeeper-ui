"""Slice 6 · A+B (Issues #82, #83) — the backup engine: init + commit + push transport.

`BackupService` turns the data dir into a **git repository the app manages for the
user** — a durable, versioned, off-machine-ready copy of the books. Issue #82 built
the *local* half: lazily initialise the repo, and append a linear commit at each
banked write. Issue #83 adds the **network push** — `bank()` commits locally (awaited,
the durability floor) then schedules a best-effort `git push` off the request path, so
no handler response ever waits on the network. All wiring/UI is later issues. It stays
a leaf: stdlib + `asyncio`/`subprocess` only, importing nothing from `api`/`web`
(mirroring `intake_confirm.py`), so the app can construct it — or not — with no import
cycle and no behavioural coupling.

The push is **fire-and-forget, force-with-lease, and honest when it can't reach the
remote.** It never pulls/merges/rebases and runs fully non-interactively, so a push can
never hang on a credential prompt. Every subprocess result is captured and a non-zero
exit is collapsed to an error *class* (`offline`/`auth-failed`/`rejected`) — logged,
never raised into a handler or shown as raw stderr. Transient failures retry with a
capped backoff then give up for the cycle, leaving commits queued locally
(git-as-offline-queue); the next `bank()` pushes current HEAD carrying them all. The
remote itself is read from git (`origin`, set up per the consultant runbook, Issue #89)
— never from an env var — and any remote carrying embedded credentials (or a non-https
network scheme) is refused, so a secret can never reach a command line or the tree.

Four invariants carry the design:

- **Born-safe.** Every git call goes through `_run`, which never raises: a missing
  `git`, a permissions fault, a non-zero exit — all are caught and logged, and the
  caller sees nothing. An unconstructed (`None`) service, or one whose commits keep
  failing, leaves the app fully functional; backup is best-effort, never load-bearing.

- **Serialized, and async.** Commits *and* push attempts run under a single
  `asyncio.Lock` so they can never collide on `.git`'s index/refs. The lock is held
  only around each subprocess, **not** across the push's backoff sleeps — so a commit
  can always interleave between two push attempts and never waits on the network.
  Subprocesses are spawned with `asyncio.create_subprocess_exec` — **never** a blocking
  `subprocess.run` inside an `async def`, which would stall the event loop.

- **Off the request path, honestly pending.** The push is scheduled via
  `asyncio.create_task` (its reference retained so the loop can't GC it mid-push) and
  never awaited by `bank()`. A cycle already in flight is coalesced — it pushes current
  HEAD, so it carries any commit banked while it runs. When it gives up, nothing is
  clobbered and no state lies: the sidecar (`.git/bookkeeper_last_push`, written only on
  a *verified* remote advance) simply doesn't move, so status reads pending until a later
  `bank()` succeeds.

- **Restorable in one command.** `config.json` lives *outside* the data dir, so a
  clone of the backup alone could not run. Before each `git add` the service copies
  the active config into the tree as `config.snapshot.json` (git dedupes it when
  unchanged, so it is cheap), making a dead-disk clone `clone → run` restorable. The
  path is passed in (`config_path`) so this leaf never guesses config's location.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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


# --------------------------------------------------------------------------- push

# The verified-push sidecar: `pushed_sha` + the ISO *completion* time, written ONLY
# after a push is confirmed on the remote. It lives inside `.git/` — which is never
# staged — so it is structurally impossible to commit into the backup tree.
_PUSH_SIDECAR_NAME = "bookkeeper_last_push"

# One push subprocess is bounded so a stalled network can never hang the cycle. A
# non-interactive `git push` that stalls is killed and treated as `offline` (retryable).
_PUSH_TIMEOUT_SECONDS = 30.0

# Capped exponential backoff between retries WITHIN a single cycle. After the last
# delay we give up for the cycle, leaving commits queued locally for the next `bank()`.
# len(schedule) retries follow the initial attempt ⇒ 4 attempts, sleeping 2s/8s/30s.
_PUSH_BACKOFF_SCHEDULE = (2.0, 8.0, 30.0)

# Push outcome kinds. Only `failed` is ever retried; the rest are terminal for the cycle.
_PUSH_SUCCESS = "success"          # verified on the remote → sidecar written
_PUSH_UNCONFIGURED = "unconfigured"  # no `origin` remote → honest no-op, not an error
_PUSH_FORBIDDEN = "forbidden"      # remote is not plain https:// → refused (secrets guard)
_PUSH_FAILED = "failed"            # a real push failure, carrying an error class


@dataclass(frozen=True)
class _PushOutcome:
    """Result of one push cycle attempt. `error_class` is one of `offline`/`auth-failed`/
    `rejected` when `kind == _PUSH_FAILED`, else `None`. `retryable` gates the backoff."""

    kind: str
    error_class: str | None = None
    retryable: bool = False


def _is_safe_remote(url: str) -> bool:
    """The secrets guard. True only for a credential-free remote we are willing to push to.

    The load-bearing rule is **no embedded userinfo** (`user:token@host`): a token in the URL
    would land in a git command line, process listing, log, and `.git/config`, so any such URL
    is always refused — this is what keeps a secret out of the tree. On top of that, a *network*
    remote must be plain `https://` (never `http`/`ssh`/`git`, per the consultant runbook's `gh`
    HTTPS setup). A `file://` remote is allowed: it is inherently credential-free and non-network
    — a legitimate local backup target (an external drive / NAS mount), and what the tests use.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.username or parsed.password:
        return False  # embedded credentials — always refused (the secrets guard).
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    if parsed.scheme == "file":
        return True
    return False  # http/ssh/git/scp-style/empty scheme — never used.


def _classify_push_error(stderr: str) -> tuple[str, bool]:
    """Collapse a push's stderr to `(error_class, retryable)` — raw stderr never surfaces.

    A lease/non-fast-forward `rejected` (a diverged remote) and an `auth-failed` will not
    self-heal within one cycle, so both are terminal — a diverged remote is REFUSED, never
    clobbered. Anything else is assumed a transient network blip (`offline`) and retried.
    """
    text = stderr.lower()
    if any(m in text for m in ("stale info", "[rejected]", "non-fast-forward",
                               "fetch first", "cannot lock ref")):
        return "rejected", False
    if any(m in text for m in ("authentication failed", "could not read username",
                               "could not read password", "terminal prompts disabled",
                               "permission denied", "403 forbidden",
                               "invalid username or password")):
        return "auth-failed", False
    return "offline", True


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap a timed-out subprocess so it never lingers as a zombie."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except Exception:  # best-effort reap — never let cleanup raise into the caller.
        pass


class BackupService:
    """Local git backup for a data dir: lazy init + serialized commit + best-effort push.

    Construct one instance per data dir (the app does, once, in assembly). The lock is
    per-instance and the push transport shares it, so all writes to a given data dir's
    repo serialize through this one object.

    The push *remote* is read from git (`origin`, established once by the consultant
    runbook — Issue #89), never from a constructor argument or env var. `remote_config`
    is accepted for construction-signature stability but the network is not steered by it.
    `config_path`, when given, is the active `config.json` snapshotted into the tree before
    each commit (the one-command-restore guarantee).
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
        # One lock guards this data dir's git index/refs for the object's lifetime; the
        # push acquires the same lock. asyncio.Lock binds to the running loop lazily.
        self._lock = asyncio.Lock()
        # The in-flight push cycle, retained so the loop cannot GC it mid-push; a new
        # cycle is only scheduled once this one is done() (coalescing).
        self._push_task: asyncio.Task[None] | None = None
        # The most recent cycle's collapsed error class (`offline`/`auth-failed`/`rejected`)
        # or None on success/unconfigured. The one push fact not recomputable from git —
        # BackupStatus (#84) reads it; everything else it derives live from git + sidecar.
        self._last_push_error_class: str | None = None

    async def bank(self, message: str) -> None:
        """Commit locally (awaited — the durability floor), then schedule a best-effort push.

        The local commit is the guarantee: once `bank()` returns, the change is durably in
        the repo. The network push is *scheduled* off the request path and never awaited, so
        no handler response waits on the network; if it fails the commit stays safely queued
        locally (git-as-offline-queue) for the next `bank()` to carry.
        """
        await self.commit(message)
        self._schedule_push()

    def _schedule_push(self) -> None:
        """Start a push cycle unless one is already in flight (coalesced).

        Runs synchronously (no awaits) so it cannot interleave: if a cycle is still running
        it already pushes *current* HEAD and will carry this bank's commit, so we don't stack
        a second; a cycle that has finished/given up is `done()`, so the next bank starts a
        fresh one. The task reference is held on the instance so the loop can't GC it.
        """
        if self._push_task is not None and not self._push_task.done():
            return
        self._push_task = asyncio.create_task(self._push_cycle())

    async def _push_cycle(self) -> None:
        """Push current HEAD to `origin`, retrying transient failures with capped backoff.

        Best-effort and never raising (it runs as a bare task). Each attempt takes the lock
        only for its subprocesses — the backoff sleeps happen lock-free, so commits can
        interleave. A terminal outcome (success / no remote / refused / a non-retryable
        failure) ends the cycle; a retryable failure sleeps and tries again until the
        schedule is exhausted, then gives up leaving state honestly pending.
        """
        try:
            for attempt in range(len(_PUSH_BACKOFF_SCHEDULE) + 1):
                async with self._lock:
                    outcome = await self._push_once()
                self._last_push_error_class = outcome.error_class
                if outcome.kind != _PUSH_FAILED:
                    return  # success / unconfigured / forbidden are all terminal
                if not outcome.retryable or attempt == len(_PUSH_BACKOFF_SCHEDULE):
                    return  # give up for this cycle — commits stay queued locally
                await asyncio.sleep(_PUSH_BACKOFF_SCHEDULE[attempt])
        except Exception as exc:  # a task must never surface an unhandled exception.
            logger.warning("backup push cycle failed for %s: %s", self._data_dir, exc)

    async def _push_once(self) -> _PushOutcome:
        """One push attempt. Must be called holding `self._lock`.

        Reads the `origin` URL from git (no remote ⇒ honest no-op); refuses a credential-carrying
        or non-https-network remote (secrets guard); runs a bounded, non-interactive
        `git push --force-with-lease`; and on exit 0 verifies the remote actually advanced
        before recording success.
        """
        url_result = await self._run("remote", "get-url", "origin")
        if not url_result.ok:
            return _PushOutcome(_PUSH_UNCONFIGURED)  # no `origin` → clean, honest no-op
        if not _is_safe_remote(url_result.stdout.strip()):
            # Never echo the URL — it may carry a token; the guard's whole point is to keep
            # a credential off every command line, log line, and out of `.git/config`.
            logger.warning(
                "backup refusing to push: origin is not a credential-free https:// "
                "(or file://) remote — a tokenized or non-https remote is never used"
            )
            return _PushOutcome(_PUSH_FORBIDDEN)
        push = await self._run(
            "push", "--force-with-lease", "origin", "main", timeout=_PUSH_TIMEOUT_SECONDS
        )
        if push.returncode is None:  # spawn error or a killed timeout → assume transient.
            return _PushOutcome(_PUSH_FAILED, "offline", retryable=True)
        if push.ok:
            return await self._verify_and_record_push()
        error_class, retryable = _classify_push_error(push.stderr)
        logger.warning("backup push to origin failed (%s)", error_class)
        return _PushOutcome(_PUSH_FAILED, error_class, retryable)

    async def _verify_and_record_push(self) -> _PushOutcome:
        """After exit 0, confirm the remote's `main` equals local HEAD, then write the sidecar.

        The sidecar records a *verified* state, not a hopeful one: we read the remote ref back
        (`ls-remote`) and only write on a match — so `everything up-to-date` is captured just
        like a fresh advance, and a push that somehow exits 0 without landing never reads green.
        """
        head = await self._run("rev-parse", "HEAD")
        remote = await self._run(
            "ls-remote", "origin", "refs/heads/main", timeout=_PUSH_TIMEOUT_SECONDS
        )
        local_sha = head.stdout.strip() if head.ok else ""
        remote_sha = (
            remote.stdout.split("\t", 1)[0].strip()
            if remote.ok and remote.stdout.strip()
            else ""
        )
        if not local_sha or remote_sha != local_sha:
            logger.warning(
                "backup push exited 0 but remote main did not verify to HEAD in %s",
                self._data_dir,
            )
            return _PushOutcome(_PUSH_FAILED, None, retryable=False)
        self._write_push_sidecar(local_sha)
        return _PushOutcome(_PUSH_SUCCESS)

    def _write_push_sidecar(self, pushed_sha: str) -> None:
        """Write the verified-push sidecar into `.git/` (best-effort; logs on failure).

        Carries the pushed sha and the ISO *completion* time (now — never the attempt time).
        Living inside `.git/` means it is structurally unstageable, so it can never be
        committed into the backup tree.
        """
        payload = json.dumps(
            {
                "pushed_sha": pushed_sha,
                "pushed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        sidecar = self._data_dir / ".git" / _PUSH_SIDECAR_NAME
        try:
            sidecar.write_text(payload, encoding="utf-8")
        except OSError as exc:
            logger.warning("backup could not write push sidecar in %s: %s", self._data_dir, exc)

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

    async def _run(self, *args: str, timeout: float | None = None) -> _GitResult:
        """Run one `git` subprocess in the data dir via async exec; never raise.

        Uses `asyncio.create_subprocess_exec` (not blocking `subprocess.run`) so the
        event loop is never stalled. `GIT_TERMINAL_PROMPT=0` is forced so git *fails*
        instead of blocking on a credential prompt — a network op can never hang the loop.
        When `timeout` is given (the push/verify ops), a subprocess that overruns is killed
        and reaped and returned as `returncode=None`. A failed spawn (git missing, OS error)
        is likewise caught and returned as `returncode=None`.
        """
        # Force non-interactive: no terminal/askpass prompt can ever appear, so a push that
        # needs credentials fails cleanly (classified `auth-failed`) instead of hanging.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(self._data_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except Exception as exc:  # git not installed, OS-level spawn failure, etc.
            logger.warning("backup git %s failed to run in %s: %s", args[:1], self._data_dir, exc)
            return _GitResult(returncode=None, stdout="", stderr=str(exc))

        try:
            if timeout is None:
                stdout, stderr = await proc.communicate()
            else:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            await _reap(proc)
            logger.warning(
                "backup git %s timed out after %ss in %s", args[:1], timeout, self._data_dir
            )
            return _GitResult(returncode=None, stdout="", stderr="timed out")
        except Exception as exc:  # e.g. an OS error mid-communicate — treated as a hard fail.
            logger.warning("backup git %s errored in %s: %s", args[:1], self._data_dir, exc)
            return _GitResult(returncode=None, stdout="", stderr=str(exc))

        result = _GitResult(
            returncode=proc.returncode,
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
        )
        # A non-zero exit is expected in normal flow (e.g. `diff --cached --quiet`), so
        # log at debug — the caller decides what a given non-zero code means.
        if not result.ok and result.returncode is not None:
            logger.debug(
                "backup git %s exited %s in %s: %s",
                args[:1], result.returncode, self._data_dir, result.stderr.strip(),
            )
        return result
