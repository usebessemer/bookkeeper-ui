# Backup & restore runbook (consultant setup)

The Slice-6 backup engine (`bookkeeper_ui/backup.py`) turns the data dir into a
**git repository the app manages for the client** — a durable, versioned copy of
the books that pushes off-machine after every banked write. This is the
**ownership axis**: the books live on the client's machine and back up only to the
client's **own private GitHub repo**, never to us and never to a shared server.

This is an **operations runbook, not an in-app wizard.** The consultant runs the
one-time setup once per client; after that the client (Javed) never touches git.
Everything here is `gh` + `git` on the command line — the app owns the repo and the
pushes, so day-to-day there is nothing to run.

> **Design floor — the engine is born-safe.** Every git call goes through
> `_run`, which never raises (a missing `git`, a permissions fault, a non-zero exit
> are all caught and logged). A never-configured remote, a failed `gh`, or an
> offline machine leaves the app **fully functional** — backup is best-effort,
> never load-bearing. So none of the steps below can wedge the app if they are
> skipped or fail; they only wire the off-machine copy.

## One-time setup (consultant-run, once per client)

Run these on the client's machine, signed in as the consultant. The remote is
read from git (`origin`), **never from an env var** — so wiring it is the whole
job.

**1. Authenticate `gh` over HTTPS.** The token goes to the **OS keychain**, never
into the repo or the data dir:

```bash
gh auth login          # choose: GitHub.com → HTTPS → authenticate in browser
```

Pick **HTTPS**, not SSH. The engine's secrets guard only pushes to a plain
`https://` (or `file://`) origin, and `gh`'s HTTPS credential helper keeps the
token in the OS keychain (macOS Keychain / Linux Secret Service) — see
[Secrets: what the app never writes](#secrets-what-the-app-never-writes).

**2. Let the app create the repo — do not `git init` by hand.** Launch the app
against the client's data dir and perform **one banked write** (import a
transaction and confirm it). On the first banked write the engine lazily inits the
repo the way it needs it: `git init -b main`, a **repo-local** commit identity
(`bookkeeper_ui/backup.py:625-626` — never `--global`, so it works on a box with no
global git identity), the managed `.gitignore`, the `config.snapshot.json`, and an
initial commit.

> **Do not run `git init` in the data dir yourself.** `_ensure_repo` is a no-op
> when `.git` already exists (`bookkeeper_ui/backup.py:616`), so a hand-inited repo
> skips the repo-local identity — and on an unattended client box with no global
> `user.name`/`user.email` the first commit then silently fails (caught and logged,
> app unaffected, but nothing is ever committed). Let the app own repo creation.

**3. Create the client's private repo and wire it as `origin`.** `--source`
points at the existing local repo from step 2:

```bash
gh repo create <client>/books --private --source=<data_dir> --remote=origin
```

This creates a **private** repo under the client's own account and adds it as
`origin` on the local backup repo. It does **not** push, and it does **not** set
upstream tracking.

**4. Push the initial history and establish upstream.** `gh repo create --remote`
and `git remote add` do *not* set tracking, so do it explicitly:

```bash
git -C <data_dir> push -u origin main
```

From here the app pushes on its own: every banked write commits locally (the
durability floor) and schedules a best-effort `git push --force-with-lease origin
main`, verified against the remote before it reads green.

**5. Note the repo is auto-managed.** Add a short `README` to the GitHub repo so
nobody hand-edits it:

> This repository is an **auto-managed backup** of a Bessemer Bookkeeper data dir.
> It is written by the app, not by hand — do not edit, force-push, or commit to it
> directly. To restore, see the consultant's backup runbook.

## Environment

The backup engine is wired in `build_app_from_env` (`bookkeeper_ui/api.py:1670`).
All of these are optional.

| env var | default | meaning |
|---|---|---|
| `BOOKKEEPER_UI_BACKUP` | `on` | The git-backup engine. Any value other than `off` (case-insensitive) keeps it **on**, so a mis-set value never silently drops backups. `off` passes `backup=None` — the app runs exactly as pre-feature, inits no repo, pushes nothing. |
| `BOOKKEEPER_UI_DATA_DIR` | `data` | The books directory the engine turns into the managed git repo (the `.git/` lives here). |
| `BOOKKEEPER_UI_CONFIG` | `examples/config.json` | The active `config.json`, which lives **outside** the data dir and is snapshotted into the tree on each commit (see [Restore](#restore-runbook)). |
| `BOOKKEEPER_BACKUP_FRESHNESS_DAYS` | `3` | How long a `pending` (committed-but-unpushed) state stays quiet amber before the safe-signal UI escalates to the LOUD nag (`bookkeeper_ui/backup.py:226`). |

**The remote is not an env var.** `origin` is read from git on every push
(`bookkeeper_ui/backup.py:422`). Wiring the remote is step 3 above; there is no
`BOOKKEEPER_UI_BACKUP_REMOTE`.

## Born-safe: a fresh install with no remote

A brand-new install with no remote wired yet is fully supported and never crashes:

- **It runs and lazily inits.** The repo is created on the first banked write, not
  before (`_ensure_repo`, `bookkeeper_ui/backup.py:608`). The startup sweep never
  creates it (`bookkeeper_ui/backup.py:372`) — a never-banked app carries no `.git`.
- **Commits accrue locally.** With no `origin`, each push is a clean, honest no-op
  (`_PUSH_UNCONFIGURED`, `bookkeeper_ui/backup.py:424`); commits queue locally as
  the offline queue and flush the moment a remote is wired.
- **The nag is loud, not silent.** `unconfigured` and `never_backed` both render
  the LOUD "not backed up" banner (the safe-signal UI, #87) — an un-wired backup is
  never a quiet green.
- **It never crashes** on a missing remote, a failed `gh`, a missing `git`, or an
  offline network: every git call is caught in `_run` and collapsed to an error
  *class*, never raised into a handler.

Covered by `tests/test_backup_startup_sweep.py::test_lifespan_is_born_inert_with_no_backup`,
`tests/test_backup_service.py::test_no_remote_is_a_clean_unconfigured_noop`, and
`tests/test_backup_signal_ui.py::test_unconfigured_and_never_backed_are_loud`.

## Restore runbook

The backup repo is **self-contained**: `config.json` is snapshotted into the tree
as `config.snapshot.json` on every commit (`bookkeeper_ui/backup.py:651`), so a
clone of the private repo carries everything needed to run. Restore onto a fresh
machine:

**1. Authenticate `gh`** (so the restored install can resume pushing to `origin`):

```bash
gh auth login          # GitHub.com → HTTPS, as in setup
```

**2. Clone the backup into the new data dir.** The clone already carries `origin`
(the HTTPS clone URL), so the app resumes pushing with no extra wiring:

```bash
git clone https://github.com/<client>/books <new data_dir>
```

**3. Restore `config.json`.** The active config lives **outside** the data dir —
it holds `chart_of_accounts`, `attribution_target_labels`, and `materiality_floor`,
none of which the app can run without. Recover it from the snapshot the clone
already carries, to a path **outside** the data dir:

```bash
cp <new data_dir>/config.snapshot.json ./config.json
```

> **Copy it out of the data dir, don't point at the snapshot in place.** Pointing
> `BOOKKEEPER_UI_CONFIG` directly at `<new data_dir>/config.snapshot.json` makes the
> next commit try to snapshot the file onto itself; the copy keeps config where it
> belongs (outside the tree). If the consultant kept the original `config.json`
> elsewhere, restore that instead — it and the snapshot are the same file.

**4. Point the env at the clone and run:**

```bash
export BOOKKEEPER_UI_DATA_DIR=<new data_dir>
export BOOKKEEPER_UI_CONFIG=./config.json
uvicorn bookkeeper_ui.api:build_app_from_env --factory
```

## Secrets: what the app never writes

No secret or token is ever written to the data dir, the working tree, or
`.git/config`:

- **The token lives in the OS keychain**, put there by `gh`'s HTTPS credential
  helper (step 1) — never in the repo or the data dir.
- **The managed `.gitignore` excludes `.git-credentials` and `.netrc`**
  (`BACKUP_GITIGNORE`, `bookkeeper_ui/backup.py:99`). No such file is ever written
  here; excluding them defensively means a misconfigured helper can never leak one
  into the tree.
- **The secrets guard refuses a tokenized remote.** A `user:token@host` origin (or
  any non-`https`/`file` network scheme) is refused *before* `git push` ever runs
  (`_is_safe_remote`, `bookkeeper_ui/backup.py:157`), so a credential can never
  reach a git command line, a log line, or `.git/config`. The refusal never echoes
  the URL.
- **No interactive prompt can write a credential.** Every git call forces
  `GIT_TERMINAL_PROMPT=0` (`bookkeeper_ui/backup.py:677`), so a push that needs
  credentials fails cleanly instead of hanging on (or being answered by) an askpass
  helper.

Verified by `tests/test_backup_service.py::test_real_push_leaves_no_secret_in_git_config`
(a real push leaves `.git/config` credential-free, origin URL has no embedded
username) and `::test_tokenized_remote_is_refused_and_never_pushed` (a token never
reaches `git push`).

## LFS-readiness and the write-once artifact store

The receipt-blob directory `artifacts/` is deliberately **kept and isolated** — it
is *not* in the managed `.gitignore` (`bookkeeper_ui/backup.py:97`), so the raw
receipt bytes are backed up like every other record, and they sit in their own
directory. That isolation means a future Git-LFS escalation is a drop-in: a single

```gitattributes
artifacts/** filter=lfs diff=lfs merge=lfs -text
```

rule would move the blobs to LFS **without restructuring the tree**.

That escalation is **not needed for v1**, because artifact storage is already
write-once. `FileArtifactStore.put` (`bookkeeper_ui/candidates.py:230-236`) no-ops
a re-put for an existing candidate id ("first write wins"), and the id is a
content SHA-256, so each distinct blob is stored exactly once on disk — and git,
being content-addressed, stores each blob once too. Repo growth is bounded by the
count of *distinct* receipts, not by re-submissions. Reach for LFS only if repo
size ever actually bites.
