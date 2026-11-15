# Storage and Backup

`novel-cli storage` reports how much disk space the application's data directories
use and provides the `backup` / `verify` / `restore` subcommands for chapter
databases. This page explains **what a backup actually contains, what happens on
failure, and how to verify and restore**; the complete option reference lives in
[CLI Usage - storage subcommand](./cli-examples.en.md#storage-subcommand).

## What is backed up {#what-is-backed-up}

`storage backup` backs up only the chapter databases under `raw_data`. It is not a
whole-machine migration tool:

| Location | Content | Included |
| --- | --- | --- |
| `raw_data/<site>/<book>/chapter.<stage>.sqlite` | Chapter text, chapter metadata, resume flags | **Yes** |
| `raw_data/<site>/<book>/book_info.<stage>.json` | Book metadata and table of contents JSON | No |
| `settings.toml` | Application configuration | No |
| `cache/` (or the configured `cache_dir`) | Cookies, tokens, login state, caches | No |
| `downloads/` (or the configured `output_dir`) | Exported TXT / EPUB / HTML files | No |
| Logs, covers, images, audio | Runtime artifacts and media | No |

Consequently:

* Restoring a backup does **not** restore a login session. After moving to a new
  machine or clearing `cache/`, sign in again.
* The restore covers chapter data. Book metadata JSON can be fetched again, and
  exported files must be exported again.
* Backup files are **plaintext SQLite** and may contain paid or private text. Keep
  them in a controlled directory, never commit them to Git, and never put them on
  a public cloud drive. For where cookies and credentials live, see
  [Security](./security.en.md#cookie-credential-storage).

## Disk usage and cleanup {#disk-usage}

```bash
# Usage of the four data directories, free space of each volume, largest subdirectories
novel-cli storage

# Machine-readable output
novel-cli storage --json

# Preview what --clean would delete without deleting anything
novel-cli storage --clean --dry-run
```

`--clean` deletes the entire `raw_data`, `cache`, `downloads`, and `logs`
directories. It is a bulk cleanup and **also removes the chapter databases**. Run
`storage backup` before cleaning `raw_data`, and make sure the backup completed
with verification. The full `--dry-run` output shape is documented in
[CLI Usage](./cli-examples.en.md#storage-subcommand).

## Backup: storage backup {#storage-backup}

```bash
novel-cli storage backup --out ~/novel-backups
novel-cli storage backup --out ~/novel-backups --keep 7
novel-cli storage backup --out ~/novel-backups --keep 7 --dry-run
novel-cli storage backup --out ./backups --json
```

### Source discovery rules {#source-discovery}

Discovery starts at the configured `general.raw_data_dir` and follows fixed rules:

1. Recursively find files whose names strictly match `chapter.<stage>.sqlite`;
   `<stage>` cannot contain `.`, `/`, or `\`. Thus `chapter.raw.sqlite` and
   `chapter.cleaner.sqlite` are sources, while `chapter.raw.sqlite.bak` and
   `chapter.raw.20260102T030405Z.sqlite` are not.
2. Only **regular files** are accepted. Symlinks are ignored so a link pointing
   outside the data directory cannot pull foreign data into a backup.
3. Paths are sorted and processed one by one; the source databases are only ever
   read.
4. If `--out` is a proper subtree of `raw_data`, that subtree is excluded from
   source discovery, so old backups are not backed up again. If `--out` is exactly
   the `raw_data` root, no exclusion is applied, but backup names carry a
   timestamp and never match the source pattern anyway.
5. When no database is found, the command succeeds (`0`), reports `count: 0`, and
   **does not** run `--keep` rotation; existing old backups stay untouched.

`--out` supports `~` and environment variables (`%VAR%` on Windows) and is created
when missing. If the path exists but is not a directory, that is a usage error
with exit code `2`, and nothing is written.

### Output layout and naming {#backup-layout}

Every source database produces a full snapshot that mirrors the original
site/book layout:

```text
<out>/<site>/<book>/chapter.<stage>.<YYYYMMDDTHHMMSSZ>.sqlite
```

The timestamp is UTC. Two backups can land in the same second (concurrent runs, a
scheduled job overlapping a manual one, or insufficient clock precision). The name
is claimed atomically with `O_CREAT|O_EXCL`; the loser tries
`chapter.<stage>.<timestamp>.1.sqlite`, then `.2`, and so on, up to `.1000`.

* **An existing file is never overwritten.** Same-name backups, placeholder
  directories, and files another process is creating are all skipped.
* If all 1001 accepted names for the second are taken, the command fails with
  `sequence_exhausted` and applies the set-wide rollback described below.
* A backup is a **full snapshot**, not an incremental one; `--keep` only rotates,
  it never compresses or merges history.

### Post-write verification (default) {#post-write-verify}

After each copy is published, it is **re-read from the bytes that were written**
and validated, not merely assumed good because copying returned no error. The
rules are the same as `storage verify`:

* checksums of the `-wal` sidecar (backup output itself switches to
  `journal_mode=DELETE`, so it usually has no sidecar);
* SQLite `PRAGMA integrity_check`;
* schema version: older versions are accepted, **newer than this build** is not;
* the required `chapters` table and the required `id`, `title`, `content` columns.

Only a set in which every copy passes is a successful backup. `files[].verified`
and the top-level `verified` are then `true`.

### What `--no-verify` really does {#no-verify}

`--no-verify` skips the **full post-write check** above (the same rules as
`storage verify`). The snapshot writer still re-reads the temporary file and runs a
basic integrity check, but it does not check the schema version or required
columns.

It exists for one case only: a database written by a **newer build**, which this
build would reject as "schema too new", yet whose bytes must be preserved. In that
case:

* the backup is still written and the report says `verified: false`;
* this build's `storage restore` will refuse it as a newer schema;
* recover it with the newer build, or confirm it yourself as described in
  [Verifying an archived copy](#verify-archived-backup) and let the newer build
  handle it.

Do not use `--no-verify` for routine backups. A copy that was not fully verified
cannot be assumed restorable.

### Set-wide rollback, including Ctrl+C {#rollback}

A backup set is only useful as a whole; a partial set would let the next retry
mistake it for a complete backup. Therefore:

* when the current copy fails to write or verify, that copy and its `-wal` /
  `-shm` / `-journal` are removed first;
* then **every copy this run already wrote** is removed;
* the command exits `1` and **does not run** `--keep` rotation;
* pre-existing backups under `--out` and the source databases under `raw_data`
  are untouched; sources are read-only;
* rollback removes files, so directories created by this run may remain empty.

**Ctrl+C follows the same rollback.** If the interrupt arrives during backup or
post-write verification, the whole set written by this run is removed, then the
exception is re-raised and the CLI prints `Aborted.` and exits `1`. Unlike an
ordinary runtime failure, `--json` does **not** emit an additional failure line on
Ctrl+C; scripts should treat an interrupt as its own exit condition.

### `--keep` retention {#keep-retention}

`--keep N` runs **after** a new set has been published and verified:

* `0` (the default) disables pruning; a negative value exits `2`.
* Only **regular files** named exactly `chapter.<stage>.<UTC>.sqlite` or
  `chapter.<stage>.<UTC>.<n>.sqlite` are considered. Directories, symlinks,
  hand-renamed files, and any other names left by older versions are never touched.
* Retention is grouped by "containing book directory + stage". A book holding both
  `raw` and `cleaner` databases keeps `N` copies of each; one stage never evicts
  the other.
* Each group is sorted newest first by `(UTC timestamp, sequence, path)` and the
  newest `N` are kept.
* Files written by the current run are always protected. Even a clock skew can
  leave one extra file behind, but it can never delete a fresh backup.
* `--dry-run` only reports which old files would be deleted, **deleting nothing**,
  while the new backup is still written. It requires `--keep N` with `N > 0`;
  otherwise it exits `2`.

### What `retention_failed` means {#retention-failed}

Retention failure is a different outcome from backup failure:

* the new backup was written completely and passed verification, and **is kept**;
* an old backup could not be deleted, or a directory could not be read, so
  rotation did not finish;
* the command exits `1` so cron jobs and monitoring notice the growing directory;
* `--json` reports `ok: false`, `error: "retention_failed"`, a non-zero `count`,
  `files` still listing the new backups, and `retention.failed` listing the paths
  that could not be removed or read;
* the new backups are not rolled back, and old backups that were removed
  successfully stay removed.

Fix the permissions, disk, or directory problem and run again; a rerun creates
another new backup and retries rotation.

### Exit codes {#backup-exit-codes}

| Exit code | Meaning |
| --- | --- |
| `0` | Backup succeeded; this includes "no chapter databases found" |
| `1` | Runtime failure: unreadable/corrupt source, damaged WAL, newer schema, exhausted sequence, I/O failure, post-write verification failure, unreadable configuration, internal error, or `retention_failed` |
| `2` | Usage error: missing `--out`, `--out` pointing at a file, negative `--keep`, or `--dry-run` without `--keep` |

Usage errors do not print a `--json` report.

## Verification: storage verify {#storage-verify}

```bash
novel-cli storage verify
novel-cli storage verify --json
```

It scans only the **live** chapter databases under `raw_data`, never the backup
directory under `--out`, and it does not copy, rename, or repair anything.

### Read-only behavior and real side effects {#verify-read-only}

Verification opens each database read-only (SQLite URI `mode=ro`) and additionally
sets `PRAGMA query_only = ON`. When a WAL database cannot be opened read-only, the
command copies the main file plus `-wal` / `-shm` to a private temporary directory
and opens the copy; the source bytes are unchanged.

One SQLite behavior worth knowing: **opening a WAL-mode database read-only can
create an empty `-wal` / `-shm` sidecar pair**, used for shared-memory
coordination. These are empty files, not data, and they do not mean the database
was modified or damaged. `storage verify` never changes the contents of an
existing main file or WAL. If the directory permissions forbid creating those
sidecars, the temporary-copy path above is used.

### Checks and what counts as corrupt {#verify-checks}

Each database is checked in order:

1. **WAL checksums**: `-wal` is inspected before SQLite opens the main file. A torn
   tail at the end of the file, caused by a crash, is safe and SQLite discards it;
   only mid-file damage where a bad frame is followed by an intact one is reported
   as `wal_damaged`. This prevents SQLite from silently ignoring a bad WAL and
   losing committed chapters that were not checkpointed yet.
2. **SQLite integrity**: `PRAGMA integrity_check` must return `ok`.
3. **Schema version**: older than this build is accepted; newer is not.
4. **Required structure**: the `chapters` table and the `id`, `title`, `content`
   columns must exist.

Any failure counts as `corrupt` and appears in `errors[]` with a `path` and a
human-readable `reason`. Here "corrupt" means **this build cannot safely use the
database**, including:

* mid-file WAL damage;
* the file is not a SQLite database (`not a database`);
* integrity check failure or malformed file;
* a newer schema;
* a missing `chapters` table or missing required columns;
* another SQLite error, or an I/O or permission error that prevents reading.

So `corrupt` does not necessarily mean the physical disk is failing, but it does
mean the database must be restored or fetched again.

### JSON fields, progress, and interruption {#verify-json}

`--json` writes exactly one line to stdout:

```json
{"ok":true,"total":12,"healthy":12,"corrupt":0,"errors":[]}
```

| Field | Meaning |
| --- | --- |
| `ok` | `true` only when the run was not interrupted and `corrupt == 0` |
| `total` | Number of databases discovered at scan start |
| `healthy` | Databases whose verification completed and passed |
| `corrupt` | Databases whose verification completed and failed (`checked - healthy`) |
| `errors[]` | Each problem has `path` (home prefix shortened to `~`) and `reason` |
| `interrupted` | Present and `true` only after a Ctrl+C interruption |

In human mode, the scan announcement and per-database progress go to **stderr**:

```text
Scanning for chapter databases...
Checking 3/12: ~/.../chapter.raw.sqlite
~/.../chapter.raw.sqlite: ok
```

A large library or slow disk therefore never looks hung, and stdout stays clean.
On Ctrl+C:

* the walk stops; the summary counts only databases whose check returned, and the
  database in flight is never counted as healthy;
* stderr prints `Interrupted after checking X of Y database(s): H healthy, C corrupt.`;
* the CLI then prints `Aborted.` and exits `1` as usual;
* `--json` still emits its one complete line with `ok: false` and
  `interrupted: true`; `total` remains the discovered count, while `healthy` /
  `corrupt` cover only the **completed** part, and `errors` contains only confirmed
  problems;
* if the interrupt lands during scanning/discovery (before any database is
  opened), `--json` still writes its one line, with `total: 0` and
  `interrupted: true`; stdout is never empty;
* a partial result **never** exits `0`.

Exit codes: all healthy (including zero databases) is `0`; any corruption or an
interrupt is `1`.

`errors[].reason` is localized text for humans, not a stable error code. To branch
on categories, use the `error` field of `storage backup --json`, not the `reason`
text from verify.

## The `--json` failure contract {#backup-json-failure}

On a **runtime failure**, `storage backup --json` still writes one line to stdout;
stdout is never empty:

```json
{"command":"storage backup","ok":false,"error":"integrity_failed",
 "message":"...","source":"...","timestamp":"20260102T030405Z",
 "out":"...","count":0,"bytes":0,"files":[],"verified":false,
 "verification_failed":false,"retention":{"keep":0,"dry_run":false,
 "candidates":[],"removed":[],"failed":[]}}
```

Key fields:

| Field | Value on failure |
| --- | --- |
| `ok` | `false` |
| `error` | Stable category, see the table below |
| `message` | Human-readable reason (ordinary runtime failures also write it to stderr) |
| `source` | The source database that triggered the failure; `null` when discovery or configuration loading failed |
| `count` / `bytes` / `files` | `0` / `0` / `[]` after set-wide rollback |
| `verified` | `false` |
| `verification_failed` | `true` when the post-write check failed, otherwise `false` |
| `verification_error` | Only on post-write failure: `source`, `target`, `reason` |
| `retention` | Empty report on failure; the `retention_failed` case is the exception below |

Stable `error` categories:

| `error` | Meaning |
| --- | --- |
| `not_database` | The source file is not a SQLite database |
| `integrity_failed` | The main database failed its integrity check |
| `wal_damaged` | The `-wal` checksum chain is damaged mid-file |
| `schema_unsupported` | Schema newer than this build, or missing required table/columns |
| `sequence_exhausted` | All accepted backup names for the same second are taken |
| `io_error` | I/O failure in the source or destination |
| `database_error` | Another SQLite error |
| `verification_failed` | Fallback category for a post-write check that did not refine further |
| `retention_failed` | New backups were kept successfully; only rotation failed. **No rollback**, `count` is non-zero |
| `config_error` | The configuration could not be loaded; `source` is `null` and nothing was written |
| `internal_error` | Unexpected exception |

Additional rules:

* Every runtime failure removes the copies this run already wrote before printing
  the report, so except for `retention_failed`, `count` is `0` and `files` is
  empty.
* A post-write verification failure also carries `verification_failed: true` and
  `verification_error`; `error` is the specific category of that failure.
* `retention_failed` is the only `ok: false` result that keeps the new backups:
  `count` / `files` still describe them, and the failures are in
  `retention.failed`. It is also the only failure report with **no `source`
  field**, because rotation failed rather than a source database.
* **Usage errors** (missing `--out`, `--out` pointing at a file, negative
  `--keep`, `--dry-run` without `--keep`) exit `2` and print no JSON.
* **Ctrl+C** rolls back the set but does not print the JSON failure line above;
  the CLI handles it as an interrupt and exits `1`.

## Online backup, WAL, and read-only opens {#wal}

`storage backup` uses SQLite's **online backup API** (`Connection.backup()`), not a
raw file copy:

* it reads a consistent transaction snapshot, including committed pages that are
  still in `-wal`; uncommitted transactions are not included;
* the application can therefore keep running during a backup without losing
  committed chapters that have not been checkpointed yet;
* the output is switched to `journal_mode=DELETE`, producing a self-contained
  single file with no `-wal` / `-shm`;
* the temporary file is `fsync`ed and then atomically moved into place; on failure
  it is removed.

**Do not manually copy only the `chapter.<stage>.sqlite` main file.** If the
database is in WAL mode, a main-file-only copy loses committed data that is still
in `-wal` and has not been checkpointed, yielding a copy that looks fine but is
missing chapters. Use `storage backup`, or at least handle the main file and its
`-wal` / `-shm` as a unit.

WAL damage handling:

* Backup and verification both inspect the `-wal` frame checksums before opening
  the main database.
* A torn tail at the end, caused by a crash, is safe and SQLite discards it; **do
  not** treat it as damage.
* Mid-file damage, where a bad frame is followed by an intact one, is refused
  (`wal_damaged`). Do not "fix" it by deleting the WAL, which permanently loses
  its committed chapters; restore from a backup or let a build that can read that
  WAL handle it.
* If `-shm` cannot be created or opened (for example, a read-only directory or a
  file copied without its sidecars), the code copies the main file and sidecars
  to a temporary directory and recovers the WAL on the copy; the source is still
  never written.
* `storage backup` can likewise leave empty `-wal` / `-shm` sidecars next to a
  source when it opens it read-only. This is normal read-only-open behavior; it
  does not change the source contents or affect the backup.

For a more predictable point-in-time snapshot, pause active downloads or
processing during the backup. The online backup itself is safe, but a running task
makes the snapshot correspond to a moving state (the database remains
transaction-consistent).

## Recovery workflows {#restore}

### Before restoring {#before-restore}

1. **Stop every writer**: the desktop app, an active CLI download, a `follow`
   poller, the local API server, and so on. Restore installs the target file with
   an atomic replace and deletes the old `-wal` / `-shm` / `-journal`; if a process
   still has the target open, replacement may fail on Windows, and on other
   platforms an old connection may keep writing.
2. Run `novel-cli storage verify` first to confirm the scope, so you restore the
   right book and stage.
3. Confirm the backup is usable. A default `storage backup` verified it after
   writing; a copy that was moved, produced by an older version, or written with
   `--no-verify` should first be checked as described in
   [Verifying an archived copy](#verify-archived-backup).

### Restore one database from a backup {#restore-one}

```bash
# With an intact backup layout, the target path is derived from <site>/<book>
novel-cli storage restore \
  --from ~/novel-backups/qidian/1010868264/chapter.raw.20260102T030405Z.sqlite \
  -y

# When the file was moved/renamed, or you want another target, pass --to explicitly
novel-cli storage restore \
  --from ./backup.sqlite \
  --to ./raw_data/qidian/1010868264/chapter.raw.sqlite \
  -y
```

Rules:

* `--from` must be a valid chapter database: integrity, schema version, and
  required columns are checked first; on failure nothing is written.
* The target must be inside `raw_data_dir` and named
  `chapter.<stage>.sqlite`.
* If the target exists, `-y` is required; otherwise the command exits with usage
  error `2` and writes nothing. If the target does not exist, `-y` is unnecessary.
* Before replacing an existing database, the old one is saved as
  `chapter.<stage>.sqlite.pre-restore-<UTC>.bak` in the same directory. A readable
  old database is preserved with an online snapshot; a corrupt one that SQLite
  cannot open is copied byte-for-byte (with sidecars) so the original data is not
  lost.
* After installing the new database, the old `-wal` / `-shm` / `-journal` beside
  the target are deleted so SQLite cannot apply a foreign log to the new file.
* `--json` reports `from`, `target`, `replaced_existing`, `pre_restore_backup`,
  `schema_version`, `chapters`, `bytes`, and `sha256`.

### Restore a whole book or everything from a backup set {#restore-set}

A backup set is organized as
`<site>/<book>/chapter.<stage>.<timestamp>.sqlite`; restore each database
individually:

* one book may have several stages such as `raw` and `cleaner`; restore each to
  its own `chapter.<stage>.sqlite`, never under another stage's target name;
* timestamps may differ between databases in a set (for example, a book added
  later); choose the desired point in time per book instead of assuming the whole
  set came from the same second;
* if the backup directory was moved, `--from` can point at the new location;
  target derivation uses only the last two directory levels. If the layout was
  rearranged, use `--to`;
* after restoring, run `novel-cli storage verify` again and confirm everything is
  healthy.

### Undo a restore with `.pre-restore-*.bak` {#restore-pre-restore}

The old database left by a replacing restore can be restored back:

```bash
novel-cli storage restore \
  --from raw_data/qidian/1010868264/chapter.raw.sqlite.pre-restore-20260102T030405Z.bak \
  --to raw_data/qidian/1010868264/chapter.raw.sqlite \
  -y
```

The `.bak` name does not match the backup naming rules, so **`--to` is
mandatory**. If that `.bak` is a byte-for-byte copy of a corrupt database, `storage restore` rejects
it; in that case, stop every process, manually copy the `.bak` (and any
`.bak-wal` / `.bak-shm` siblings) back over the target, delete the target's
`-wal` / `-shm` / `-journal`, and run `storage verify`. Do not let the
application open the target during the manual copy.

### No usable backup {#no-backup}

When the database is confirmed corrupt and no usable copy exists:

1. stop the application;
2. move the corrupt main file and its `-wal` / `-shm` / `-journal` out of
   `raw_data` (do not leave them where the application will open them again);
3. download or process the book again so the application rebuilds the chapter
   database.

This loses the chapters held only in that database; if they came from a source
that no longer serves them, they may not be fully recoverable.

### Verifying an archived copy {#verify-archived-backup}

`storage verify` scans only the live `raw_data`, not the backup directory. To check
a backup file:

```bash
# Check byte integrity (compare with sha256 from backup --json)
sha256sum ~/novel-backups/qidian/1010868264/chapter.raw.20260102T030405Z.sqlite

# Let SQLite run its integrity check
sqlite3 ~/novel-backups/qidian/1010868264/chapter.raw.20260102T030405Z.sqlite \
  'PRAGMA integrity_check;'
```

`schema_version` and the chapter row count are also available in `files[]` from
`storage backup --json`. To run the full check from this build, temporarily point `general.raw_data_dir` at
a directory containing only that copy and run `storage verify`, then switch it
back; never mix the copy into the real data directory.

## Related documentation {#related}

* [CLI Usage - storage subcommand](./cli-examples.en.md#storage-subcommand):
  complete options and output field tables.
* [File Saving](./file-saving.en.md): `raw_data` layout and export locations.
* [Troubleshooting](./troubleshooting.en.md): quick commands for deleted
  databases, cleanup, and restore.
* [Security](./security.en.md#cookie-credential-storage): where cookies,
  credentials, and plaintext data should live.