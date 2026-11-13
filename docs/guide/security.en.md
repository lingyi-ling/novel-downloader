## Security Notes and Auditing

`novel-downloader` is a local tool for downloading and exporting novels. It contacts a large number of third-party sites and can also start a local REST API for other devices, so it must consider four classes of risk at once: **remote content, local-network exposure, local file permissions, and credential storage**.

This page describes the project's threat model, where sensitive data lives, how to audit it yourself, and how to report vulnerabilities.

!!! warning "Three things to remember"
    1. The API binds to `127.0.0.1` by default; do not change it to `0.0.0.0` without extra protections.
    2. When `--token` is set, both `/api/v1/*` and `/downloads/*` require authentication; without a token, both are open to any reachable network — but browser requests with a non-loopback origin are rejected by the origin gate (see below) unless you explicitly enable `--cors`.
    3. Cookies, platform credentials, and `settings.toml` are all stored in plaintext; do not commit, sync, or share them.

---

### Threat model

#### Assets to protect

| Asset | Location | Impact if leaked |
| --- | --- | --- |
| Site cookies / login sessions | `novel_cache/<site>/*.cookies` | Others can impersonate your account on the site |
| Music platform credentials | `session.json` and `platform_creds.json` under the platform data directory | NetEase / TIDAL and similar accounts get taken over |
| API token | Command-line argument or `NOVEL_API_TOKEN` | Download tasks can be controlled and the export directory read |
| Downloaded and exported files | `general.output_dir` (default `./downloads`) | Exposes your reading history and book metadata |
| Raw HTML / logs | `general.raw_data_dir`, `general.debug.log_dir` | May contain cookies, request headers, and private page content |
| Local configuration file | `settings.toml` | May contain site passwords, cookies, and proxy credentials |

#### Attack surface and trust boundaries

| Entry point | Risk |
| --- | --- |
| Local REST API | When bound to `0.0.0.0`, reachable from the same LAN (or even the internet, depending on the firewall); without a token, downloads can be controlled |
| `/downloads` static directory | Protected by the same authentication when a token is set; without a token, only port reachability protects it |
| Cross-origin browser requests | Backstopped by the origin gate by default (see below); with `--cors` enabled, any website can call your local API from a browser |
| Content returned by target sites | HTML, JS, fonts, and images all come from third parties; parsers and plugins treat it as untrusted input |
| Local plugins | With `[plugins] enable_local_plugins = true`, local plugin code is loaded and executed |
| Third-party dependencies | Dependency vulnerabilities are out of scope for the audit script; use tools such as `pip-audit` separately |
| Cloud sync / shared folders | When the project lives in OneDrive, Dropbox, or a network share, cookies and exported files are synced along with it |

#### Not covered by these protections

- An attacker who already has your operating-system account (they can read any file of that user).
- A token you pass in plaintext on the command line (it ends up in shell history and the process list).
- Deliberately packaging and sending `novel_cache/`, `settings.toml`, or `downloads/` to someone else.
- A compromised target site or one that serves malicious content; this project does not sandbox content.

---

### Using the API token

By default the API listens on `127.0.0.1:8081` and performs no authentication when no token is configured. This is only appropriate for "only this machine can reach it" scenarios.

Ways to configure a token:

```bash
# Recommended: environment variable, stays out of shell history
export NOVEL_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
novel-api

# Or a command-line argument (appears in shell history and the process list)
novel-api --host 127.0.0.1 --port 8081 --token "$NOVEL_API_TOKEN"
```

Clients must send one of the following headers:

```http
Authorization: Bearer <token>
```

```http
X-API-Key: <token>
```

Implementation detail: tokens are compared with `secrets.compare_digest` to avoid timing side channels; an empty token is allowed through.

#### The browser-origin gate (Origin gate)

Binding to `127.0.0.1` only stops *remote* clients, not *browser* ones: any website
you visit can fire cross-origin requests at `http://127.0.0.1:<port>` (browsers
always attach an `Origin` header to cross-origin POSTs). The API therefore installs
an origin gate by default (`OriginCheckMiddleware` in `apps/api/middleware.py`):

- Requests with a **non-loopback** `Origin` are answered with `403` — including
  look-alike hosts such as `127.0.0.1.evil.com` and `Origin: null` (sandboxed
  iframes);
- Non-browser clients (CLI, SDK, curl) send no `Origin` header and are unaffected;
- Loopback origins (`http://127.0.0.1:any-port`, `http://localhost`,
  `http://[::1]`) pass, so locally served pages keep working;
- `--cors` is the explicit opt-in for cross-origin browser access: with the flag
  the gate is **not installed** and CORS governs what browsers may do.

Known residual risk: a same-origin DNS-rebinding page (the attacker's domain
resolves to the public internet to load, then rebinds to `127.0.0.1`) issues
same-origin requests that carry **no `Origin` header**, which the gate cannot
recognize. For complete defense against that read/drive scenario, configure
`--token` — the reason this documentation keeps repeating "exposed means
tokened". A related input check: the `book_id` in task and watchlist requests
becomes the directory `raw_data/<site>/<book_id>`, so the API rejects values
containing path separators, `..`, a drive-letter colon and similar characters
(`422`), keeping the chapter cache inside the raw-data tree.

#### When bound to `0.0.0.0`

`--host 0.0.0.0` listens on every interface. If no token is set at startup, the program prints a warning to stderr. More importantly:

- Both `/api/v1/*` and `/downloads/<filename>` are protected by the token;
- `/docs`, `/redoc` and `/openapi.json` are **disabled automatically** on a non-loopback bind, so the full route and parameter schema is not exposed to the LAN; they are served only on a loopback listen, without the token dependency.

So when you truly need cross-device access, open the port with a token configured and restrict source networks with a firewall. Recommendations:

1. Set a strong token;
2. Use a firewall to allow only trusted subnets to the port;
3. Put authentication on `/downloads` at the reverse proxy layer as well;
4. Never combine `--cors` with a non-loopback address.

#### Webhook outbound data

When `NOVEL_API_WEBHOOK` is set, task information (title, site, export paths, etc.) is POSTed to that address when a task finishes. Point it only at services you trust, so your reading history is not sent to a third party.

Webhook URLs often carry a shared secret in the query string (`?token=...`). When a
failed delivery is logged, the URL is redacted first (only
`scheme://host[:port]/path` remains) and the query string urllib3 embeds in its
exception text is scrubbed the same way — no query string or basic-auth
credentials ever reach the log, matching the access-log policy of recording the
path but never the query string.

---

### Download directory and file sharing

Relevant settings:

```toml
[general]
output_dir = "./downloads"    # the API's /downloads static directory
raw_data_dir = "./raw_data"   # raw JSON / chapter content
cache_dir = "./novel_cache"   # cache + cookies
```

Risk points:

- `downloads/` and `raw_data/` live in the current working directory by default, so they are easy to archive, back up, or sync to the cloud as a whole.
- The API's `/downloads` maps directly to `output_dir`; filenames and the history endpoint expose "what you downloaded".
- `raw_data/` and `logs/` can contain full HTML, request headers, and exception stack traces — they leak cookies more easily than the finished e-books.
- On multi-user machines, if these directories are world-readable, other local users can read your exported files too.

Recommendations:

- Keep `output_dir`, `raw_data_dir`, and `cache_dir` in directories only the current user can access;
- Do not put these directories in OneDrive / Dropbox / a Git repository;
- When exposing the API, only expose the necessary export directory and authenticate at the reverse proxy layer;
- Periodically delete `raw_data/` and logs you no longer need.

---

### Where cookies and credentials are stored

| Type | Default location | Override |
| --- | --- | --- |
| Site cookies | `<cache_dir>/<site>/aiohttp.cookies`, by default `./novel_cache/<site>/` | `general.cache_dir`; the login helper and some searchers also honor `NOVEL_CACHE_DIR` |
| Music login sessions | `%LOCALAPPDATA%\novel_downloader\music\session.json` (Windows) | `NOVEL_MUSIC_HOME` |
| Music platform credentials | `platform_creds.json` in the same directory | `NOVEL_MUSIC_HOME` |
| Cookies / passwords in site configuration | `[sites.<site>]` in `settings.toml` | Manual editing |
| Application state | `%LOCALAPPDATA%\novel_downloader\state.json` | `NOVEL_HOME` |

A cookie file is a JSON array whose entries look like
`{"name": "...", "value": "...", "domain": "...", "path": "..."}`; the **values are plaintext**.
Writes use a temporary file plus an atomic `os.replace`, and on POSIX systems a best-effort
`chmod 0600`; Windows has no equivalent POSIX permission bits, so the file inherits the parent directory's ACL.

Checking Windows permissions:

```powershell
icacls "$env:LOCALAPPDATA\novel_downloader"
icacls .\novel_cache
```

Recommendations:

- Do not commit `novel_cache/`, `settings.toml`, `downloads/`, or `logs/`;
  these entries are already in `.gitignore`, but do not bypass it with `git add -f`.
- Do not paste configurations containing cookies into issues, chats, or screenshots.
- On logout, suspected leakage, or when migrating machines, delete the corresponding `*.cookies` / `session.json`
  and sign out all sessions on the site itself.
- Periodically check `settings.toml` for leftover `cookies = "..."` or `password = "..."`.

---

### Threat Model for the Update Chain

Automatic updates turn "third-party content" into "an executable program", so the chain gets its own threat model. Its trust anchors, in order:

| Step | Constraint | Code |
| --- | --- | --- |
| Manifest fetch | Fixed HTTPS endpoint `releases/latest/download/latest.json`; a mismatched `schema` is rejected outright | `fetch_update_manifest` / `parse_update_manifest` in `apps/common/updater.py` |
| Download URL | Asset URLs in the manifest must be **HTTPS**; the Android client also only accepts HTTPS | `_manifest_asset`, `UpdateChecker.downloadRequestFailure` |
| Integrity | The asset must carry a **SHA-256**: the desktop and Android clients refuse to download without one, and the CLI does the same in non-interactive (`--yes`) mode (the interactive path skips verification); the digest is recomputed after the download and a mismatch deletes the partial file | `download_asset` / `verify_sha256` |
| Before install | The digest is **recomputed right before the installer starts**, closing the time-of-check/time-of-use window between verification and installation | `webview_app._install_verified`, `installPrecheck` |
| Release side | Before upload, `scripts/audit_release_assets.py` performs a **release asset leak audit** (allowlist, archive member paths, archive text members scanned with the `scan_secrets` rules, private keys / deploy passwords / tokens in raw bytes); matches report only the marker name, never the value | `audit_release_content` in `scripts/release.py` |

**Optional manifest signing**: update manifests support Ed25519 signatures. `updater.TRUSTED_UPDATE_KEYS` is empty by default, so manifests are **accepted unsigned** and the trust anchor is HTTPS plus each asset's SHA-256. Once a trusted key is configured, a missing manifest, an unsigned manifest, a bad signature or an unknown `key_id` is rejected, and the client **stops falling back** to the GitHub API — otherwise deleting the manifest would bypass signature checking.

Residual risks:

- With signing disabled (the default), an attacker who can modify both the manifest and the assets (for example, someone who controls the repository) can ship a malicious package whose digest is internally consistent. Enable manifest signing and pin the public key in the client when you need a stronger guarantee.
- The asset leak audit covers release artifacts only; it is not a substitute for protecting the build machine and release credentials.

---

### Auditing it yourself

The repository ships an offline audit script, `scripts/security_audit.py`; it **does not touch the network** and does not query CVEs.

```bash
# Human-readable report
python scripts/security_audit.py

# JSON report (stdout contains only JSON)
python scripts/security_audit.py --json

# Additionally audit a specific configuration file
python scripts/security_audit.py --config path/to/settings.toml

# Also scan fake credentials under tests/
python scripts/security_audit.py --scan-tests
```

Exit codes:

| Exit code | Meaning |
| --- | --- |
| `0` | Audit completed, no high-priority findings |
| `1` | Audit completed, at least one high-priority finding |
| `2` | Bad arguments, or the audit itself could not run |

Checks:

| Check | Content |
| --- | --- |
| `dependencies` | Compares `pyproject.toml` against `importlib.metadata` and reports missing or version-mismatched dependencies; missing core dependencies are high priority |
| `configuration` | `verify_ssl = false`, plaintext token/cookie/password in configuration files, overly broad configuration file permissions |
| `code_tls` | Hardcoded `verify=False` / `ssl.CERT_NONE` in the source |
| `api_docs` | Whether `--host 0.0.0.0` examples in the docs mention tokens and whether `--cors` is enabled alongside |
| `storage` | Plaintext cookie / credential files, cloud-sync directories, POSIX permissions |
| `credentials` | Cookie, token, API key, and private-key patterns in tracked files; only locations and redacted lengths are reported |

The script **never** prints matched credential values; the `evidence` in its output contains only key names, line numbers, and `<redacted:N chars>`.

Limitations:

- No CVE lookups and no dynamic behavior analysis;
- On Windows, ACLs are not verified; run `icacls` manually;
- Real examples in documentation or test fixtures may be misreported as credentials; judge them in context;
- The security boundary of plugins and site parsers relies on code review and is outside automated checks.

---

### Repository credential scanning

`scripts/security_audit.py` inspects the local runtime environment; to treat the **repository itself** (working tree, index, and commit history) as a credential-leak surface, use `scripts/scan_secrets.py`. It depends only on the Python standard library and `git`, runs fully offline, and reports only rule names and locations — **matched values are never echoed** — so a terminal transcript or CI log cannot become a new leak.

```bash
# Working tree: every file Git tracks
python scripts/scan_secrets.py

# Index: only staged additions/copies/modifications/renames
python scripts/scan_secrets.py --staged

# History: read-only scan of lines each commit added
python scripts/scan_secrets.py --history

# Bound the history walk by revision range and commit count (with --history only)
python scripts/scan_secrets.py --history --since v1.20.0 --max-commits 200

# Machine-readable output: stdout is JSON only
python scripts/scan_secrets.py --json
```

Output looks like `path:line: rule` (history scans use `commit:path:line: rule`). `--json` emits `count` and `findings` (history also includes `commits_scanned`, `skipped_commits`, and `truncated`), likewise without plaintext.

| Rule | What it recognises |
| --- | --- |
| `private-key` | PEM private-key headers (RSA/EC/OpenSSH/DSA/PGP); only the block marker is matched |
| `aws-access-key` | AWS access key ids prefixed with `AKIA` / `ASIA` |
| `github-token` | `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` and `github_pat_` tokens |
| `slack-token` | `xoxa-`/`xoxb-`/`xoxp-`/`xoxr-`/`xoxs-` tokens |
| `bearer-token` | A literal `Bearer` credential (placeholders excluded) |
| `generic-secret` | A literal assignment to a credential-shaped key such as `password`, `secret`, `token`, or `api_key` (environment/config references and placeholders excluded) |
| `sensitive-file` | A tracked `*.jks`, `*.keystore`, `*.p12`, `*.pfx`, `*.bks`, `keystore.properties`, `id_rsa`, etc. |

Scope and limits:

- Only files known to Git are read (`git ls-files`; `--staged` reads the staged blobs from the index). Build output and local credential files ignored by `.gitignore` are out of scope.
- Binary files, unknown extensions, and files larger than 2 MiB are skipped for content scanning; however, a keystore/private-key **file name** is itself a `sensitive-file` finding.
- `--history` scans only the lines each commit added in `git log -p -U0`, so a credential deleted from the working tree is still found. Merge commits produce no patch by default; a commit whose patch exceeds 8 MiB is skipped as a whole, and the walk stops after 256 MiB of accumulated patch (the output says so). Use `--max-commits` to narrow the range. The scan is read-only: nothing is checked out or rewritten.
- `--staged` and `--history` are mutually exclusive; `--since` / `--max-commits` require `--history`, and `--max-commits` must be at least 1.

Exit codes:

| Exit code | Meaning |
| --- | --- |
| `0` | No findings |
| `1` | At least one finding, or the scan could not run (argument conflict, `git` failure, etc.; the error goes to stderr and never includes file content) |

#### Optional local pre-commit hook

To run the staged scan automatically before every `git commit`, install the repository-level hook explicitly:

```bash
# Install/update .git/hooks/pre-commit (writes this tool's marker)
python scripts/install_git_hooks.py

# An existing pre-commit from another tool is refused by default;
# --force backs it up to pre-commit.bak first
python scripts/install_git_hooks.py --force

# Remove only a hook installed by this tool; other hooks and another tool's
# pre-commit are left alone
python scripts/install_git_hooks.py --uninstall
```

The hook prefers the repository's `.venv` Python, falls back to `python3`/`python`, and then runs `scripts/scan_secrets.py --staged`; repository paths containing spaces work as well. The installer writes only to the current repository's `.git/hooks/`, **never** changes global Git configuration, and installs nothing by default. If `core.hooksPath` is set, Git does not read `.git/hooks`, so the hook will not run.

This is a local convenience gate for developers, not a security boundary; the CI `secrets` job and the weekly history scan remain the authoritative gates, and skipping or omitting the local hook does not weaken them.

#### The two CI checks

| Workflow | Trigger | Behaviour |
| --- | --- | --- |
| The `secrets` job in `.github/workflows/ci.yml` | Pushes to `main`, any pull request, manual dispatch | Runs `python scripts/scan_secrets.py` over the working tree; only checkout and Python setup, with a 5-minute timeout. A finding fails the job, blocking the pull request before review or release. |
| `.github/workflows/security.yml` | Every Monday at **03:17 UTC** (`cron: "17 3 * * 1"`) and manual dispatch | Checks out the full history with `fetch-depth: 0`; runs the working-tree scan and `--history` one after the other, and either finding exits non-zero. Both `--json` reports are written to files (never echoed to the log) and uploaded as the `security-scan-reports` artifact, **retained for 30 days**. |

Both scans in `security.yml` always run, so one failing report cannot hide the other; the concurrency group is fixed to `security-scan` and never cancels a run in progress. GitHub pauses scheduled workflows after 60 days without repository activity — use `workflow_dispatch` to run one manually.

#### After a finding

1. **Locate it**: use the reported path and line (history findings also carry a commit id) to inspect the context, for example with `git show --stat <commit>` or `git log -1 <commit>`. Do not copy or paste the matched value; determine whether it is still valid and whether other files reuse it.
2. **Rotate first, clean up second**: a value that entered a commit must be treated as leaked. Revoke or rotate it at the issuer (token, password, access key), update the environment variable or secret store, and review the issuer's usage logs. For a private key, replace the key pair and remove the old public key from `authorized_keys`. Deleting the line alone does not recall clones or caches.
3. **Clean the working tree/index**: remove the value or replace it with an environment reference, re-run `python scripts/scan_secrets.py --staged`, and commit once it is clean.
4. **Clean history (with care)**: rewrite history (`git filter-repo`, BFG, …) only when it is genuinely necessary.

!!! warning "Rewriting history is irreversible"
    A rewrite changes every commit id after the affected commit, requires a force push, and invalidates other people's clones, forks, open pull requests, and tags; GitHub may also cache old commits through its API, in which case support must purge them. Take a full mirror backup and notify collaborators first. Rotating the credential always comes first — a history rewrite cannot reach copies that were already cloned, forked, cached, or written to CI logs.

#### False positives and the allowlist

The scanner treats obvious placeholders as false positives: it does not match when the value contains `your`, `example`, `placeholder`, `dummy`, `<...>`, or `${...}`, or when the assignment comes from an environment variable or configuration object (such as `os.environ` or `settings.`). Test-fixture directories such as `tests/` have a narrow, per-rule allowlist instead (see `ALLOWLIST` below).

When an exception is still needed, add a `(path glob, rule set, reason)` entry to `ALLOWLIST` in `scripts/scan_secrets.py`:

```python
ALLOWLIST: tuple[tuple[str, frozenset[str], str], ...] = (
    # ...
    (
        "docs/guide/example.md",          # as precise as possible; avoid *
        frozenset({"generic-secret"}),    # relax only the rule that is a false positive
        "documented placeholder example", # say why it is safe
    ),
)
```

Rules:

- Rules not listed for that path are still enforced; never use `*` to disable every rule.
- Relax only the one rule on a value you have **confirmed synthetic**, and keep the path as narrow as possible (a file or the smallest directory).
- Prefer rewriting the example as an obvious placeholder or environment reference over allowlisting it; a broad allowlist makes a real credential easier to miss.
- Never put a real value in an allowlist reason, and use synthetic credentials only in test fixtures.

---

### Release asset content scanning {#release-asset-scan}

The last content gate before upload is the **release asset content scan**: after the build finishes and `latest.json` is generated, `scripts/release.py` calls `scripts/audit_release_assets.py`, which streams the **text members** of every zip / apk — and of an `.exe` that really is a ZIP (a PyInstaller one-file bundle) — line by line through `scripts/scan_secrets.py`'s rules. It reuses that scanner's rule set, placeholder heuristics and path allowlist, so the two cannot drift apart.

A hit is tagged `secret-marker` and rendered as:

```text
PROBLEM [secret-marker] <asset>: <member>:<line>: <rule>
```

Only the asset, member, line and rule name are reported; the matched value is **never echoed**.

Scan boundaries (deliberately cheap — do not read this as "everything inside the archive is inspected"):

- A member larger than **2 MiB** is skipped as a whole (the same per-file cap as `scan_secrets`); text scanning is capped at **64 MiB** per asset and **256 MiB** per audit run, and anything beyond the budget is skipped.
- A member with a NUL byte in the first 8 KiB is treated as binary and skipped; directories, symbolic links and nested `.zip` / `.exe` / `.apk` members are not scanned, and nested archives are **never extracted**.
- Only `.zip` / `.apk` files that open as ZIP, plus `.exe` files that really are ZIP, are opened as archives; an `.exe` that is not a ZIP is simply not opened as one.
- A member that cannot be read is reported as an `unreadable-archive` problem instead of being treated as clean; an archive with more than 200,000 members has its member scan stopped and reported as `archive-too-large`.
- The `scan_secrets` path allowlist applies to member paths too (for example the narrow test-fixture exemptions), but those are archive-internal paths, not repository paths.

This complements the pre-build working-tree scan rather than duplicating it:

- Before the build, `release.py` runs `scan_release_secrets` — `scripts/scan_secrets.py` in working-tree mode — over **Git-tracked files only**; `--skip-secret-scan` disables that step (not recommended).
- After the build and before upload, the asset content scan inspects the **bytes that will actually be uploaded**, so it can catch things a source scan cannot see, such as files generated during the build, pulled in from dependencies, or accidentally bundled into an archive. It has no skip flag.

Failure exit codes:

| Invocation | Exit code | Meaning |
| --- | --- | --- |
| `python scripts/audit_release_assets.py --dir <dir>` | `0` | No problems |
| same | `1` | At least one `secret-marker` or other audit problem |
| same | `2` | Bad arguments or an I/O error; the audit could not run |
| `scripts/release.py` | `2` | An asset finding raises `ReleaseError`; the release stops before tagging and uploading |

Limitations: it covers only the text members and raw-byte checks described above; it does not inspect the internals of binary members, does not extract nested archives, and performs no entropy or semantic analysis. It is not a substitute for protecting the build machine and release credentials.

---

### Reporting vulnerabilities

If you find a security vulnerability, please **do not** open a public issue first. Prefer the
"Report a vulnerability" (private security advisory) feature of the GitHub repository:

<https://github.com/lingyi-ling/novel-downloader/security/advisories/new>

Please include in your report:

1. A description of the vulnerability and its impact;
2. Reproduction steps or a minimal reproducer;
3. Affected versions / commits;
4. A suggested mitigation or fix.

Alternative contact: reach the maintainers through the same GitHub private security advisory.

Once a report is confirmed we will assess the impact and ship a fix as soon as possible; please do not disclose details publicly before the fix is released.

---

### Hardening checklist

- [ ] Keep the API on the default `127.0.0.1`; for remote access set a token plus firewall and reverse proxy.
- [ ] Do not use `--cors` on `0.0.0.0`; enable `--cors` only when you really need
      cross-origin browser calls (it turns the origin gate off).
- [ ] Set `settings.toml` permissions to `600` (POSIX) or readable only by the current user (Windows ACL).
- [ ] Keep `novel_cache/`, `downloads/`, and `logs/` out of cloud sync, shared backups, and Git.
- [ ] Periodically delete `*.cookies`, `session.json`, and `platform_creds.json` you no longer need.
- [ ] On suspected leakage, sign out all sessions on the site immediately and log in again.
- [ ] Run `python scripts/security_audit.py` and confirm there are no high-priority findings.
- [ ] Run `python scripts/scan_secrets.py --staged` before committing, and keep the CI `Secret scan` and the weekly history scan green.
- [ ] Watch dependency security advisories and supplement CVE checks with tools like
      `pip-audit` when needed (for this repo's editable install, run it with
      `--skip-editable`).
