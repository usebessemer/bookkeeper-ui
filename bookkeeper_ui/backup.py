"""Slice 6 · A+B+C (Issues #82, #83, #84) — the backup engine: init + commit + push + status.

`BackupService` turns the data dir into a **git repository the app manages for the
user** — a durable, versioned, off-machine-ready copy of the books. Issue #82 built
the *local* half: lazily initialise the repo, and append a linear commit at each
banked write. Issue #83 adds the **network push** — `bank()` commits locally (awaited,
the durability floor) then schedules a best-effort `git push` off the request path, so
no handler response ever waits on the network. Issue #84 adds a read-only `status()`: a
truthful `BackupStatus` computed live from git and the verified-push sidecar on every
call — **never a cached boolean** — that the UI (Issue #87) renders. All wiring/UI is
later issues. It stays
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Repo-LOCAL commit identity. Set into `.git/config` (never `--global`) so a commit
# succeeds on a machine with no global git identity — an unattended client box.
_IDENTITY_NAME = "Bookkeeper Backup"
_IDENTITY_EMAIL = "backup@bookkeeper.local"

_INITIAL_COMMIT_MESSAGE = "Initialize Bookkeeper backup repository"

# The sweep-recovery commit subject (Issue #86). A sweep-committed change is NOT a banked
# business event, so it is deliberately NOT a `banked: …` subject — it is the recovery of an
# uncommitted change a crash-mid-write left orphaned, made auditable as exactly that.
_SWEEP_COMMIT_MESSAGE = "Bookkeeper backup sweep — recovered uncommitted changes"

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


# ------------------------------------------------------------------------- status

# The four-state backup ladder. `status()` collapses live git + the verified-push sidecar
# to EXACTLY one of these on every call — never a cached boolean. Ordered worst→best.
STATE_UNCONFIGURED = "unconfigured"   # no `origin` remote, or the repo isn't inited yet
STATE_NEVER_BACKED = "never_backed"   # a remote exists but no verified push has ever landed
STATE_PENDING = "pending"             # HEAD is ahead of the last verified-pushed sha
STATE_BACKED_UP = "backed_up"         # HEAD == the last verified-pushed sha (the ONLY green)

# Freshness escalation: how long a `pending` state may sit as quiet amber before it becomes
# the LOUD nag class. DECISION (Stu, 2026-07-23): 3 days, not the proposed 24h — a contractor
# may be off-grid on a job site for a few days and 24h would false-alarm. Env-configurable
# (whole or fractional days); a missing / non-numeric / non-positive value falls back to 3d,
# so a mis-set env var can never zero the threshold (which would nag on every pending state).
_DEFAULT_FRESHNESS_THRESHOLD = timedelta(days=3)
_FRESHNESS_THRESHOLD_ENV_VAR = "BOOKKEEPER_BACKUP_FRESHNESS_DAYS"

# Error classes that make a `pending` state escalate to LOUD *immediately*, without waiting
# for the freshness clock: both are non-retryable (see `_classify_push_error`) and will NOT
# self-heal — an expired/revoked token (`auth-failed`) or a diverged remote (`rejected`) must
# surface loudly now, not climb quietly as amber. `offline` is deliberately excluded: it is
# transient (the off-grid contractor), and the freshness threshold already covers it in time.
_ESCALATING_ERROR_CLASSES = frozenset({"auth-failed", "rejected"})


def _read_freshness_threshold_from_env() -> timedelta:
    """The freshness threshold from the env var, defaulting to 3 days.

    A missing, non-numeric, or non-positive value is a clean fall-back to the default — a
    mis-set env var can never make the threshold zero or negative (which would escalate every
    pending state at once).
    """
    raw = os.environ.get(_FRESHNESS_THRESHOLD_ENV_VAR)
    if raw is None:
        return _DEFAULT_FRESHNESS_THRESHOLD
    try:
        days = float(raw)
    except ValueError:
        return _DEFAULT_FRESHNESS_THRESHOLD
    return timedelta(days=days) if days > 0 else _DEFAULT_FRESHNESS_THRESHOLD


def _ladder_state(remote_configured: bool, has_verified_sidecar: bool, unpushed_count: int) -> str:
    """Collapse the raw facts to exactly one ladder rung (worst→best precedence).

    No remote (or not inited) is `unconfigured` regardless of everything else. With a remote
    but no verified sidecar it is `never_backed` — the born-safe never-pushed default that can
    never read green. With a verified sidecar, HEAD level with the pushed sha
    (`unpushed_count == 0`) is `backed_up`, and any commits ahead is `pending`.
    """
    if not remote_configured:
        return STATE_UNCONFIGURED
    if not has_verified_sidecar:
        return STATE_NEVER_BACKED
    if unpushed_count == 0:
        return STATE_BACKED_UP
    return STATE_PENDING


@dataclass(frozen=True)
class BackupStatus:
    """A per-call snapshot of the backup's truthful state — computed live from git and the
    verified-push sidecar on every `status()` call, NEVER a cached boolean.

    `state` collapses to exactly one rung of the four-state ladder. `escalated` is the layered
    LOUD-nag signal: a `pending` state that has either gone stale (older than the freshness
    threshold) or is stuck on a non-retryable push error. The rest are the raw facts the UI
    (Issue #87) renders — the verified push-completion time, the honest unpushed count, and the
    last collapsed error class.

    Invariant: `backed_up` — the only green rung — requires `remote_configured` AND
    `unpushed_count == 0` AND a real `last_push_time`, so a committed-but-unpushed repo, or one
    with a corrupt/absent sidecar, can never read green.
    """

    state: str
    remote_configured: bool
    unpushed_count: int
    last_push_time: datetime | None
    last_error_class: str | None
    escalated: bool = False


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
        freshness_threshold: timedelta | None = None,
    ) -> None:
        # Resolve to an ABSOLUTE path now, so every git call runs against an
        # unambiguous location regardless of the process's cwd at call time.
        self._data_dir = Path(data_dir).resolve()
        self._remote_config = remote_config
        self._config_path = Path(config_path) if config_path is not None else None
        # How long a `pending` state stays quiet amber before escalating to LOUD. Resolved
        # once at construction — env-configurable, default 3 days — but an explicit argument
        # wins, so a test can pin a tiny threshold deterministically.
        self._freshness_threshold = (
            freshness_threshold
            if freshness_threshold is not None
            else _read_freshness_threshold_from_env()
        )
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

    async def sweep(self) -> None:
        """Reconcile the repo with no banked action — the startup/idle self-heal (Issue #86).

        Best-effort and never raising (it runs as a bare lifespan task). Three jobs, in order:

        1. **Crash-orphan recovery.** `commit()` stages and commits any uncommitted change a
           crash-mid-write left in the tree — a no-op on a clean tree. This is also what makes
           launch re-verification honest: a HEAD level with the sidecar but a *dirty* tree reads
           `backed_up` (status counts commits, not the worktree), a stale green; committing the
           orphan advances HEAD past the sidecar so the very next `status()` reverts to `pending`.
        2. **Re-verify unpushed.** Recompute HEAD-vs-the-verified-sidecar (never `@{u}`), so a
           session that ended committed-but-unpushed — or that just recovered an orphan — is seen.
        3. **Flush.** If anything is unpushed, schedule a push. `_schedule_push` already coalesces
           (it never stacks on an in-flight cycle), so a sweep firing next to a banked write's push
           spawns no redundant second cycle. With `origin` unreachable the cycle is an honest,
           non-blocking failure that leaves state pending for the next sweep; with a recovered
           network it self-heals — all with no banked action required.

        No-op when the repo isn't inited yet: nothing has been banked, so there is nothing to
        sweep, and the sweep must never create the repo the first `bank()` creates lazily (that
        would turn an unconfigured app into one carrying a `.git`).
        """
        if not (self._data_dir / ".git").exists():
            return  # never-banked / unconfigured: nothing to sweep, and never init here.
        await self.commit(_SWEEP_COMMIT_MESSAGE)  # crash-orphan recovery; no-op on a clean tree.
        sidecar = self._read_verified_sidecar()
        pushed_sha = sidecar[0] if sidecar is not None else None
        if await self._count_unpushed(pushed_sha) > 0:
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

    # ----------------------------------------------------------------------- status

    async def status(self) -> BackupStatus:
        """Compute the current backup status live from git + the verified-push sidecar.

        Read-only and lock-free by design: it runs only non-mutating git queries
        (`remote get-url`, `rev-list --count`), so it never collides with an in-flight
        commit/push and a slow (up to 30s) push never delays a status read. Born-safe like
        every other path — every git call goes through `_run`, which never raises — so a
        not-yet-inited repo, a remote with no upstream, or an empty/detached HEAD each return
        a clean value object rather than throwing. Crucially it counts unpushed commits against
        the SIDECAR sha, NEVER `@{u}` (which fatals with no upstream), so it is fully
        upstream-independent.
        """
        url = await self._run("remote", "get-url", "origin")
        remote_configured = url.ok and bool(url.stdout.strip())

        sidecar = self._read_verified_sidecar()
        pushed_sha = sidecar[0] if sidecar is not None else None
        last_push_time = sidecar[1] if sidecar is not None else None
        unpushed_count = await self._count_unpushed(pushed_sha)

        state = _ladder_state(remote_configured, sidecar is not None, unpushed_count)
        return BackupStatus(
            state=state,
            remote_configured=remote_configured,
            unpushed_count=unpushed_count,
            last_push_time=last_push_time,
            last_error_class=self._last_push_error_class,
            escalated=self._is_escalated(state, last_push_time),
        )

    def _is_escalated(self, state: str, last_push_time: datetime | None) -> bool:
        """Whether a `pending` state has crossed into the LOUD nag class.

        Only `pending` escalates — the other rungs carry their own UI treatment. It goes loud
        when EITHER the last push error is non-retryable (`auth-failed`/`rejected` — a stuck
        token or diverged remote that will not self-heal) OR the last verified push is older
        than the freshness threshold (the quiet-amber clock has run out). A transient `offline`
        error alone does NOT escalate — only elapsed time does — so an off-grid contractor is
        given the full freshness window before being nagged.
        """
        if state != STATE_PENDING:
            return False
        if self._last_push_error_class in _ESCALATING_ERROR_CLASSES:
            return True
        if last_push_time is None:
            return False
        return datetime.now(timezone.utc) - last_push_time > self._freshness_threshold

    def _read_verified_sidecar(self) -> tuple[str, datetime] | None:
        """Read the verified-push sidecar, returning `(pushed_sha, pushed_at)` or None.

        Returns None on anything short of a fully trustworthy record — a missing/unreadable
        file, malformed JSON, a missing key, an empty sha, or an unparseable timestamp — so a
        corrupt sidecar is treated as "no verified push" (→ `never_backed`) and can never read
        as green. A timestamp without a timezone is coerced to UTC so the freshness comparison
        stays aware-vs-aware; every sidecar this service writes is already UTC-aware.
        """
        path = self._data_dir / ".git" / _PUSH_SIDECAR_NAME
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
            sha = str(data["pushed_sha"]).strip()
            pushed_at = datetime.fromisoformat(str(data["pushed_at"]))
        except (ValueError, TypeError, KeyError):
            return None
        if not sha:
            return None
        if pushed_at.tzinfo is None:
            pushed_at = pushed_at.replace(tzinfo=timezone.utc)
        return sha, pushed_at

    async def _count_unpushed(self, pushed_sha: str | None) -> int:
        """Count commits on HEAD not yet covered by the verified-pushed sha — upstream-free.

        Uses `git rev-list --count <pushed_sha>..HEAD` against the SIDECAR sha, NEVER
        `@{u}..HEAD` (which fatals exit 128 with no upstream). With no sidecar every commit is
        unpushed, so it counts all of HEAD. If the sidecar sha is not in local history (a
        rewritten or foreign sha) the range query fatals — that is caught and folded back to
        the total-commit count. A not-yet-inited/empty repo yields 0. Never raises, never fatals.
        """
        if pushed_sha:
            ahead = await self._count_commits(f"{pushed_sha}..HEAD")
            if ahead is not None:
                return ahead
            # Sidecar sha unknown to this repo → fall back to counting everything.
        total = await self._count_commits("HEAD")
        return total if total is not None else 0

    async def _count_commits(self, revspec: str) -> int | None:
        """`git rev-list --count <revspec>`; None if the query fails (unresolvable revspec,
        not a repo) so the caller can fall back — this never fatals into the caller."""
        result = await self._run("rev-list", "--count", revspec)
        if not result.ok:
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

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
