## Security Notes and Auditing

`novel-downloader` is a local tool for downloading and exporting novels. It contacts a large number of third-party sites and can also start a local REST API for other devices, so it must consider four classes of risk at once: **remote content, local-network exposure, local file permissions, and credential storage**.

This page describes the project's threat model, where sensitive data lives, how to audit it yourself, and how to report vulnerabilities.

!!! warning "Three things to remember"
    1. The API binds to `127.0.0.1` by default; do not change it to `0.0.0.0` without extra protections.
    2. When `--token` is set, both `/api/v1/*` and `/downloads/*` require authentication; without a token, both are open to any reachable network.
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
| `--cors` | Once enabled, any website can call your local API from a browser |
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

#### When bound to `0.0.0.0`

`--host 0.0.0.0` listens on every interface. If no token is set at startup, the program prints a warning to stderr. More importantly:

- Both `/api/v1/*` and `/downloads/<filename>` are protected by the token;
- `/docs` and `/openapi.json` do not depend on the token (they only expose API descriptions).

So when you truly need cross-device access, open the port with a token configured and restrict source networks with a firewall. Recommendations:

1. Set a strong token;
2. Use a firewall to allow only trusted subnets to the port;
3. Put authentication on `/downloads` at the reverse proxy layer as well;
4. Never combine `--cors` with a non-loopback address.

#### Webhook outbound data

When `NOVEL_API_WEBHOOK` is set, task information (title, site, export paths, etc.) is POSTed to that address when a task finishes. Point it only at services you trust, so your reading history is not sent to a third party.

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
- [ ] Do not use `--cors` on `0.0.0.0`.
- [ ] Set `settings.toml` permissions to `600` (POSIX) or readable only by the current user (Windows ACL).
- [ ] Keep `novel_cache/`, `downloads/`, and `logs/` out of cloud sync, shared backups, and Git.
- [ ] Periodically delete `*.cookies`, `session.json`, and `platform_creds.json` you no longer need.
- [ ] On suspected leakage, sign out all sessions on the site immediately and log in again.
- [ ] Run `python scripts/security_audit.py` and confirm there are no high-priority findings.
- [ ] Watch dependency security advisories and supplement CVE checks with tools like `pip-audit` when needed.
