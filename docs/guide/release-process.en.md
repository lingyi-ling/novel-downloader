# Release process (maintainers)

A one-page release manual for maintainers. The repository's hard rules (what must be run when)
live in `AGENTS.md`; this page does not repeat those clauses, it explains **why it is done this
way, the exact commands, and what to do when a step fails**.

One release produces eight GitHub Release assets: three Windows bundles, one installer, one APK,
plus `SHA256SUMS.txt`, `manifest.json` and the update manifest `latest.json`.
`scripts/release.py` chains these into an idempotent pipeline where no failure leaves a
"looks complete" public release behind.

---

## 0. Prerequisites {#prerequisites}

| Item | Requirement | If missing |
| --- | --- | --- |
| OS | Windows (PyInstaller cannot cross-compile; the installer only builds on Windows) | the build fails |
| Interpreter | `.venv/Scripts/python.exe` (Python 3.12) | every command below assumes this path |
| Inno Setup 6 | `ISCC.exe` on `PATH` or in its default install directory | the `desktop` target refuses to start; use `--skip-installer` for a portable-only release |
| Android signing | `android/keystore.properties` + the keystore file (both git-ignored) | `release.py` skips the APK with a warning; a bare `assembleRelease` fails outright |
| GitHub credentials | `GITHUB_TOKEN` / `GH_TOKEN`, or a password `git credential fill` can return for `github.com` | aborts before tagging |
| Network | `api.github.com` and `uploads.github.com` reachable; `github.com:443` may be blocked | see [Restricted network](#network) |

The docs and deployment tools are optional extras:

```bash
.venv/Scripts/python.exe -m pip install -e ".[docs,deploy]"
```

---

## 1. The version number: one source, four copies {#version-sync}

The version is defined exactly once, in `src/novel_downloader/__init__.py`. Everything else is a
**hand-maintained copy** with a different consumer, so missing any one of them fails in a
different way:

| Location | Who reads it | Covered by an automatic gate |
| --- | --- | --- |
| `__version__` in `src/novel_downloader/__init__.py` | the package, PyInstaller version resources, `doctor`, `latest.json` | the source |
| `versionName` / `versionCode` in `android/app/build.gradle.kts` | the version the APK reports, the system's upgrade decision | `release.py` and `check_versions.py` compare `versionName`; `tests/test_android_release_config.py` checks `versionCode` |
| `## v<version> (YYYY-MM-DD)` at the top of `CHANGELOG.md` | the release notes body | `release.py`: must be the topmost version section with a non-empty body |
| `pyproject.toml` | setuptools packaging | `check_versions.py`: must use `dynamic = ["version"]`, no second literal |
| the JSON-LD `softwareVersion` in `website/index.html` | search-engine structured data | `tests/test_website_static.py` pins it to `__version__` |

Bump in this order:

1. `__version__ = "X.Y.Z"`.
2. `versionName = "X.Y.Z"`, and recompute `versionCode` by the rule below.
3. Add `## vX.Y.Z (YYYY-MM-DD)` at the top of `CHANGELOG.md`; its body is the release notes.
   Leave older sections untouched, and do not keep `## Unreleased` above it — `release.py`
   requires this version to be the **topmost** version section.
4. Sync `softwareVersion` in `website/index.html`.

### 1.1 `versionCode` is "which release this is", not a free increment {#version-code}

The rule is: **the number of `## vX.Y.Z` sections in `CHANGELOG.md` whose version is ≤ the current
one** (`v1.0.0` → `1`, `v1.37.0` → `51`). So the order is "write the changelog section first, then
read off the ordinal":

```bash
.venv/Scripts/python.exe -c "
import re,pathlib
text = pathlib.Path('CHANGELOG.md').read_text(encoding='utf-8')
versions = {tuple(map(int, m)) for m in re.findall(r'^## v(\d+)\.(\d+)\.(\d+)', text, re.M)}
current = (1, 38, 0)          # change this to the version being released
print(sum(1 for v in versions if v <= current))
"
```

Put that number into `versionCode`. `tests/test_android_release_config.py` asserts it equals that
ordinal and that no ordinal repeats in history; a reused code makes Android treat the new package
as older and silently ignore the upgrade. The ordinal is entirely determined by how many version
sections the changelog has, so a forgotten section shifts it by one — which is why "write the
changelog first, then read off the number" is the only safe order.

Then run the consistency check; it reports every copy that has not been updated:

```bash
.venv/Scripts/python.exe scripts/check_versions.py
```

The output looks like `[OK  ] package/Android versionName: 1.37.0 (versionCode 51)`; any `FAIL`
line names the file to edit. CI's `versions` job runs the same script on every push.

!!! note "A literal no gate covers"
    `docs/guide/settings-reference*.md` contains a sample desktop-preferences export whose
    `"app_version"` is documentation only and is not pinned by a test. Update it after a release
    if you remember; forgetting it does not fail anything.

---

## 2. Pre-release gates {#pre-release-gates}

Run them in this order. It goes from cheap to expensive: syntax and type errors surface in
seconds, so there is no reason to discover them after a ten-minute PyInstaller run.

```bash
# 1. Tests
.venv/Scripts/python.exe -m pytest tests -q

# 2. Static checks
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m ruff format --check src tests scripts
.venv/Scripts/python.exe -m mypy src

# 3. Version consistency
.venv/Scripts/python.exe scripts/check_versions.py

# 4. Docs
.venv/Scripts/python.exe -m mkdocs build --strict
```

### 2.1 When the change touches user-visible strings

```bash
.venv/Scripts/python.exe scripts/update_pot.py
.venv/Scripts/python.exe scripts/update_po.py   # then fill the zh_CN msgstr, drop #, fuzzy
.venv/Scripts/python.exe scripts/compile_mo.py
```

`messages.po` is tracked; `*.mo` and `*.pot` are not (see `.gitignore`).
`tests/test_i18n_coverage.py` requires every `t()` msgid to have a non-empty, non-fuzzy `zh_CN`
translation; one missing entry turns it red.

### 2.2 When the change touches Android

```bash
cd android
./gradlew.bat testDebugUnitTest lintDebug --no-daemon --console=plain
```

With no emulator/adb on this machine, that only proves "it compiles, the unit tests and lint
pass", not "it works on a phone".

An Android release build has **no debug-signing fallback**: without a keystore, `assembleRelease`
fails at the task guard (`assembleDebug` and the unit tests are unaffected). That is deliberate —
a debug-signed APK installs, reports the release `versionName`, and can never be upgraded by the
real key afterwards (Android refuses the signature change), which turns a missing file into a
broken update channel. `tests/test_android_release_config.py` locks that invariant in, together
with `isDebuggable = false`, `isShrinkResources = false` and the rest of the release config.

### 2.3 What to do when a gate fails

| Gate | Usual cause | Fix |
| --- | --- | --- |
| `pytest` | behaviour changed without updating tests; i18n coverage short | add the test or the `zh_CN` translation, do not weaken the assertion |
| `ruff check` / `format --check` | unformatted code | `ruff format src tests scripts`, then re-run the check |
| `mypy src` | a new unannotated function, an `Any` leak | add annotations; CI only type-checks the build chain and a few scripts under `scripts/` |
| `check_versions.py` | only one copy was bumped | fix the file each output line names |
| `mkdocs build --strict` | an internal link to a missing page or anchor | fix the link; a new page must also be added to `nav` in `mkdocs.yml` |
| `test_i18n_coverage` | a new string is untranslated or marked `#, fuzzy` | run the three scripts in 2.1 |

---

## 3. Running the release {#run-release}

### 3.1 Dry run first

```bash
.venv/Scripts/python.exe scripts/release.py --dry-run
```

`--dry-run` **runs no subprocess and touches no network**: it validates the version, the tag
shape and the `CHANGELOG` section, then prints the plan (build command, tag, asset list, the
platforms `latest.json` would advertise). Use it to confirm:

- the `Tag behind` line says what you expect (refused by default);
- the `Assets` list contains `NovelDownloader-setup-<ver>-x64.exe`;
- the platform count and keys in `latest.json` (5 by default).

### 3.2 The real release

```bash
.venv/Scripts/python.exe -u scripts/release.py > build/rel.log 2>&1
tail -30 build/rel.log
```

!!! warning "Never pipe release.py into tail"
    With `release.py | tail` the exit status comes from `tail`, which swallows a failure as 0.
    Redirect to a file and read it instead. `-u` makes the log land in real time, so the last
    step is still visible if the process is killed.

Stages and where they fail:

| Stage | What it does | On failure |
| --- | --- | --- |
| Preflight | version / CHANGELOG / tag shape / Android `versionName` | any mismatch exits 2 **before the build** |
| Credential scan | `scan_secrets.py` over Git-tracked files | a hit aborts, printing only `path:line: rule` |
| Worktree | `git status --porcelain` must be clean | uncommitted changes abort (`--allow-dirty` skips it) |
| Build | `build_release.py` for three bundles + `build_desktop.py` for the installer | exits 2; the last 8 log lines are redacted and printed |
| Directory check | `verify_release_dir`: `manifest.json` complete, no extra files | see [Resuming](#resume) |
| Android | `assembleRelease` + the 16 KB alignment gate | an alignment failure aborts; without signing material `release.py` warns and skips the APK |
| Assets | collect, `verify_release_requirements`, rebuild `latest.json`, content audit | see [the three hard gates](#hard-gates) |
| Tag / push | local tag, `git push`, REST fallback when needed | see [Restricted network](#network) |
| Release | create draft → upload → verify digests → publish | a failure leaves a resumable draft |

Only two exit codes exist: `0` for success, `2` for a `ReleaseError` or an argument error.

---

## 4. The three hard gates {#hard-gates}

All three live in `release.py`, all abort the release, and all run **before the tag is created** —
once the tag is pushed, GitHub's ref points at this build and fixing it afterwards means moving
or deleting a tag.

### 4.1 16 KB page alignment (`scripts/check_android_alignment.py`)

Since Android 15, some arm64 devices use 16 KB memory pages. On such a device a 4 KB-aligned
`.so` **installs and then crashes on launch**. Two independent things must hold:

1. entries mmapped at runtime inside the zip (`lib/*/*.so` and uncompressed resources) must start
   on a 16 KB boundary;
2. every `PT_LOAD` segment of each `lib/*/lib*.so` must have `p_align` ≥ 16 KB.

Checking only one is a false sense of safety. `release.py` runs it over every produced APK and
aborts on any violation. To re-check one APK by hand:

```bash
.venv/Scripts/python.exe scripts/check_android_alignment.py \
  android/app/build/outputs/apk/release/app-release.apk
```

It prints `zip_aligned` and `elf_p_align` per library; exit codes are `0` clean, `1` violation,
`2` unreadable APK. The fix is to build the offending dependency with
`-Wl,-z,max-page-size=16384` or upgrade it; do not bypass the check.

### 4.2 Asset leak audit (`scripts/audit_release_assets.py`)

The pre-build `scan_secrets.py` looks at **source**; this gate looks at the **bytes that will
actually be uploaded**. PyInstaller can pull a stray `.env`, a `.pdb` or a token-bearing sample
config out of the build directory, and nothing else in the pipeline inspects the bytes.

It checks three classes:

- **publication surface**: every file must be on the name allowlist, non-empty and plausibly
  sized; with `--release-json` it must also match the remote asset set exactly.
- **archive members**: ZIP/APK members may not contain traversal paths, VCS/build directories,
  credentials, source maps or debug symbols.
- **raw bytes**: streamed in 1 MiB chunks (with a 96-byte cross-chunk overlap) for private-key
  headers, deploy passwords and token-shaped strings.

A hit reports only `asset: member:line: rule` or a marker name, **never the matched value**, so
it is safe to paste into an issue. This gate has **no skip switch**. Manual re-check:

```bash
.venv/Scripts/python.exe scripts/audit_release_assets.py \
  --dir dist/release/novel-downloader-1.37.0-windows-x64
```

Exit codes are `0` clean, `1` problems, `2` usage/IO error. Limits: members over 2 MiB are
skipped, per-asset text scanning is capped at 64 MiB, a whole run at 256 MiB; nested archives are
never extracted.

### 4.3 Post-upload verification (`verify_assets`)

Uploading is the one step whose failure used to be invisible: a concurrent run or a dropped
connection left a release that looked complete while a file was missing. So after uploading,
`release.py` **re-reads the release** and asserts:

- every intended asset is present;
- the remote digest equals the local SHA-256 (a remote asset with no digest is treated as
  "must replace");
- when `latest.json` is included it **must** have a remote digest, otherwise the manifest is
  refused in an unverified state.

The release is created as a **draft** for the whole run and only `PATCH`ed public after this
passes. A mid-run failure therefore leaves a resumable draft, not a half-populated public release.

### 4.4 Two more "hard enough" preconditions

- **Pre-build credential scan**: `--skip-secret-scan` turns it off, but only for a verified false
  positive; once off, the whole build is unchecked for credentials.
- **Desktop payload contract**: the `desktop` target requires
  `NovelDownloader-setup-<ver>-x64.exe` by default and refuses to publish without it — installed
  copies only update from `windows-x64-installer`, so a missing installer silently falls back to
  the portable zip: the release looks fine while installed users never get the update. With
  `--skip-installer` the platform is absent from the manifest, and the **portable zip must
  exist** as the fallback.

---

## 5. Resuming: `--skip-build` and `--allow-tag-behind` {#resume}

When the build succeeded but the upload failed (typically a 408 or a dropped link), do not
rebuild — reuse the existing output directory:

```bash
.venv/Scripts/python.exe -u scripts/release.py --skip-build > build/rel.log 2>&1
```

`--skip-build` reuses `dist/release/novel-downloader-<version>-*/` and, before doing anything
else, proves the directory is complete with `verify_release_dir`:

- `manifest.json` exists, matches the version and has no `failures`;
- every requested target has at least one artifact that exists, is non-empty and matches the
  recorded size;
- there is **no** file in the directory that `manifest.json` does not list (a leftover zip from
  another version would otherwise be uploaded as part of this release).

Assets already uploaded with a matching digest are skipped by SHA-256; a differing digest is
deleted and re-uploaded.

**A tag behind HEAD** (more commits landed after tagging) is still refused by default, because a
real build's artifacts come from `HEAD` and must not be attached to an older tag. If you have
confirmed the existing artifacts were built from the tag, use:

```bash
.venv/Scripts/python.exe -u scripts/release.py \
  --skip-build --allow-tag-behind > build/rel.log 2>&1
```

Rules:

| Situation | Result |
| --- | --- |
| `--allow-tag-behind` without `--skip-build` | the combination is rejected (a real build must come from HEAD) |
| the tag is an ancestor of HEAD | allowed, prints a notice, **the tag is not moved** |
| the tag diverged from HEAD (not an ancestor) | refused; `--allow-tag-behind` cannot help, handle the tag by hand |
| the tag points at another commit | refused; never silently reused |
| the remote tag exists at a different commit | refused |

`release.py` **never moves or deletes a tag**. Changing a tag is a destructive operation and
needs explicit approval first.

---

## 6. Restricted network {#network}

In mainland China it is common for **`github.com:443` to be unreachable while `api.github.com`
works**. They are separate services: building, uploading and creating the release all go through
the API, and only `git push` uses github.com.

### 6.1 Automatic tag-push fallback

When `git push` fails, `release.py` creates `refs/tags/<tag>` with
`POST /repos/<slug>/git/refs`, pointing at **the tag's own commit**
(`git rev-parse <tag>^{commit}`), not HEAD. That way "a docs commit landed after tagging" cannot
make the tag point at the wrong commit.

If the REST call returns 422 with `Object does not exist`, the commit the tag points at **has not
been pushed yet**. Push `main` first, then re-run `--skip-build`:

```bash
.venv/Scripts/python.exe scripts/push_via_api.py --dry-run
.venv/Scripts/python.exe scripts/push_via_api.py
```

`scripts/push_via_api.py` uses only the Git Data API: it reads the remote branch tip, finds the
"content already synced" boundary by comparing trees, replays each commit after it (uploading
missing blobs, rebuilding trees, preserving the original message/author/committer and dates),
and finally updates the branch ref **without force**. If the remote has commits the local branch
does not contain, the script errors out instead of overwriting. Credentials likewise stay in
HTTP headers only — never printed, never written to disk.

Note: a commit created through the API has a different sha from the local one but the **same
tree**, so local `main` diverges from the remote. Keep pushing through the API afterwards; a
plain `git push` would be non-fast-forward.

### 6.2 A one-off recovery script

The repo also has an **untracked** one-off script, `build/push_replay.py` (`build/` is
git-ignored). It does the same job but hardcodes the repository and branch, adds a content-level
boundary check and a post-push verification (the final remote tree must match local `HEAD` file
for file). It has no `--dry-run` and takes no arguments, so it is only for a single manual run
once you are sure the remote content equals some local commit:

```bash
.venv/Scripts/python.exe build/push_replay.py
```

Use `scripts/push_via_api.py` for day-to-day releases; it has `--dry-run`, takes the repo and
branch as arguments, and is covered by tests.

### 6.3 Large uploads

An APK is on the order of 70 MB and regularly dies halfway through a system proxy.
`upload_asset` behaves as follows:

- attempt 1 goes through the system proxy;
- attempts 2–4 go **direct** (`ProxyHandler({})`), backing off 4 / 8 / 16 seconds;
- a single upload times out at 900 s; read requests time out at 120 s with 3 retries;
- a timed-out attempt may actually have completed, in which case GitHub answers
  `already_exists`; the script looks the asset up by name and compares digests, treating a match
  as success.

So on `HTTP 408 Upload body timed out`, just:

```bash
.venv/Scripts/python.exe -u scripts/release.py --skip-build > build/rel.log 2>&1
```

If both the proxy and direct access drop, re-upload with `requests` directly
(`trust_env=False` + `proxies={}`), then run `--skip-build` again so `verify_assets` re-checks
the digests.

!!! danger "Never drop a file into the release directory by hand"
    `verify_release_dir` treats any file not in `manifest.json` as an error and aborts.
    If an asset is missing, rebuild or fix the build output and re-run — do not copy a file in.

---

## 7. Website deployment {#website}

Only needed when something under `website/` changed; it is a separate flow from the release.

```bash
# Show which files would be uploaded, without connecting
.venv/Scripts/python.exe scripts/deploy_site.py --dry-run

# Deploy for real
.venv/Scripts/python.exe scripts/deploy_site.py
```

`SITE_FILES` is an explicit allowlist; any new file under `website/` must be added to it or it is
never published. The script uploads to a `/tmp` staging directory on the server, checks
everything arrived, then atomically replaces `/var/www/noveldl` with two renames; a failed swap
restores the previous directory.

Verify byte for byte afterwards (`curl` and `cmp` are bash-side paths, so `/tmp` is fine):

```bash
curl -s http://43.165.170.43/noveldl/index.html -o /tmp/live-index.html
cmp /tmp/live-index.html website/index.html && echo "index OK"

curl -s http://43.165.170.43/noveldl/__no_such_page__ -o /tmp/live-404.html
cmp /tmp/live-404.html website/404.html && echo "404 OK"

curl -s http://43.165.170.43/noveldl/sitemap.xml -o /tmp/live-sitemap.xml
cmp /tmp/live-sitemap.xml website/sitemap.xml && echo "sitemap OK"
```

`curl -I` alone only shows a status code and cannot prove the body is the custom page. The 404
page and nginx configuration are covered in [Website deployment](website-deploy.en.md).

---

## 8. Update manifest signing {#signing}

Signing is implemented but **off by default** (`updater.TRUSTED_UPDATE_KEYS` is empty). Pointers
only — the full story is not repeated here:

- **Format, canonical JSON, `key_id`, per-client differences**: see
  [Update manifest signing](update-signing.en.md).
- **Enabling**: `build_update_manifest.py --keygen` to make the key → put the public key into the
  Python `TRUSTED_UPDATE_KEYS` **and** Android's `UpdateChecker.TRUSTED_UPDATE_KEYS` (two tables,
  two separate builds) → pass `--sign-key` on every release afterwards.
- **Rotation**: it must go through "a transition version trusting both keys → sign with the new
  key → remove the old key"; it cannot be done in one step. See section 4 of
  [Update manifest signing](update-signing.en.md).

Once enabled there is one hard constraint: **every release must ship `latest.json`**, otherwise
clients that pinned the public key refuse the update and stop falling back to the API.
`release.py` already uploads the manifest **last**, so every file it points at is in place.

Sign at release time:

```bash
.venv/Scripts/python.exe scripts/release.py --sign-key ~/.novel_downloader/update.key
```

On success it prints `==> latest.json signed (key id <id>)`; `--dry-run` tells you up front
whether this run would sign. A signing failure (unreadable, not hex, not 32 bytes) aborts
**before the tag is created**.

---

## 9. Post-release verification {#post-release}

`release.py` already checked the digests, but one independent check separates "the script said
success" from "production really is correct".

### 9.1 All assets present

A complete release has **8** assets:

| Asset | Produced by |
| --- | --- |
| `novel-cli-<ver>-windows-x64.zip` | `build_release.py` |
| `novel-api-<ver>-windows-x64.zip` | `build_release.py` |
| `NovelDownloader-<ver>-windows-x64.zip` | `build_release.py` (portable) |
| `NovelDownloader-setup-<ver>-x64.exe` | `build_desktop.py` (Inno Setup) |
| `novel-downloader-<ver>-android.apk` | `gradlew assembleRelease` |
| `SHA256SUMS.txt` | merged bundle + APK + installer + `latest.json` digests (6 lines today) |
| `manifest.json` | the build record (targets / artifacts / failures) |
| `latest.json` | the update manifest, rebuilt from the real assets |

The usual reasons for one being missing: `--skip-android` (7), `--skip-installer` (7, and the
manifest has no `windows-x64-installer`), or missing Android signing material (7, plus a warning).

```bash
V=1.37.0
curl -s "https://api.github.com/repos/lingyi-ling/novel-downloader/releases/tags/v$V" \
  | .venv/Scripts/python.exe -c "import json,sys; d=json.load(sys.stdin); print('draft', d['draft'], 'assets', len(d['assets'])); [print(' ', a['name'], a['size']) for a in d['assets']]"
```

The unauthenticated API allows 60 requests per hour, so spend them sparingly.

### 9.2 The stable endpoint and the manifest contents

Clients read `releases/latest/download/latest.json` (a 302 to the CDN). It must be **byte for
byte identical** to the one built locally:

```bash
mkdir -p build/relcheck
curl -sL -o build/relcheck/latest.json \
  https://github.com/lingyi-ling/novel-downloader/releases/latest/download/latest.json
cmp build/relcheck/latest.json "dist/release/novel-downloader-$V-windows-x64/latest.json" \
  && echo "manifest byte-identical"
```

Then confirm the manifest describes itself correctly and covers every platform:

```bash
.venv/Scripts/python.exe -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print(d['version'], d['tag'], d['repo']); print(sorted(d['platforms']))" build/relcheck/latest.json
```

Expect `version == tag[1:]`, `repo == lingyi-ling/novel-downloader`, and the platforms
`android`, `windows-x64-api`, `windows-x64-cli`, `windows-x64-installer`, `windows-x64-portable`
(the last one is absent with `--skip-installer`).

`SHA256SUMS.txt` and `latest.json` give the same digest for the same file; a disagreement is a
release-blocking bug. `release.py` enforces it with `validate_sums_against_manifest`, and you can
spot-check one line:

```bash
curl -sL "https://github.com/lingyi-ling/novel-downloader/releases/download/v$V/SHA256SUMS.txt" \
  | grep "NovelDownloader-setup-$V-x64.exe"
```

### 9.3 The client's view

```bash
.venv/Scripts/novel-cli.exe update --check --json
```

It reads the same manifest. Once signing is enabled you can also verify it offline with the
public key (never touching the private one):

```bash
.venv/Scripts/python.exe scripts/build_update_manifest.py \
  --release-json payload.json --version "$V" --tag "v$V" \
  --repo lingyi-ling/novel-downloader \
  --check build/relcheck/latest.json --public-key <64-hex public key>
```

`payload.json` is the API response for `releases/tags/v$V`. Exit code `0` means the document
matches and the signature verifies; otherwise it prints `stale: <field>`.

---

## 10. Failure quick reference {#troubleshooting}

| Symptom | Usually means | Fix |
| --- | --- | --- |
| `--version X does not match __version__ Y` | only `--version` or only the package was changed | edit `__init__.py`; the two must be equal |
| `CHANGELOG.md has no section ## vX.Y.Z (` | the section is missing, or written `## X.Y.Z` without the `v` | use `## vX.Y.Z (YYYY-MM-DD)` at the very top |
| `CHANGELOG.md's newest version section is ...` | this version is not the topmost section | move this version's section to the top |
| `android versionName '...' does not match` | the Gradle copy was not synced | edit `versionName` in `build.gradle.kts` |
| `versionCode is N but X.Y.Z is release #M` | `versionCode` is not the CHANGELOG ordinal | recompute it per [1.1](#version-code) |
| `working tree has N uncommitted change(s)` | uncommitted changes | commit, or use `--allow-dirty` once you are sure |
| `no GitHub token available` | neither the env var nor the credential helper has one | set `GITHUB_TOKEN`, or make `git credential fill` return a password |
| `the desktop target requires the Inno Setup installer` | ISCC is not installed | install Inno Setup 6, or `--skip-installer` for portable-only |
| `... is not 16 KB page-size ready` | a `.so` is still 4 KB aligned | rebuild that dependency with `-Wl,-z,max-page-size=16384` |
| `release asset audit failed (...)` | an artifact contains credentials, debug symbols or a bad path | read the reported asset and member, fix the build input; there is no skip switch |
| `tag vX.Y.Z exists locally at ... but HEAD is ...` | the tag is behind or diverged | behind: `--skip-build --allow-tag-behind`; diverged: handle the tag by hand |
| `--allow-tag-behind requires --skip-build` | wrong flag combination | add `--skip-build`, or rebuild from HEAD |
| `tag ... already exists on origin at ...` | the remote tag points at another commit | stop, confirm, then handle it; `release.py` never moves a tag |
| REST 422 `Object does not exist` | the commit the tag points at is not on the remote yet | push `main` with `push_via_api.py`, then `--skip-build` |
| `HTTP 408 Upload body timed out` | a large asset died in the proxy | just resume with `--skip-build`; uploaded assets are skipped by digest |
| live `latest.json` is 404 | the release is still a draft, the upload failed, or it was deleted | re-run `--skip-build`; never delete the manifest by hand |
| the client reports `manifest-unavailable` | the manifest could not be fetched (draft / network / HTTP redirect) | confirm the release is published and the asset exists |
| `warning: the release has extra asset(s)` | the remote has older files this build did not produce | harmless (the manifest does not point at them); delete by hand for tidiness |

---

## 11. Division of labour with CI {#ci}

`.github/workflows/release.yml` is **manual-dispatch only** and deliberately does not listen for
tag pushes: the local release script pushes the tag itself, so a tag-triggered CI run would build
a second set of artifacts (without the signed APK or the Inno Setup installer) and overwrite what
had just been uploaded — exactly what broke v1.4.0. The workflow runs on `windows-latest`,
publishes with `GITHUB_TOKEN`, and skips Android because it has no signing material.

So:

- run `release.py` locally for a normal release (it has the keystore and ISCC);
- `ci.yml` only runs gates (lint / mypy / version consistency / tests / Android unit tests) and
  never publishes;
- do not add a tag trigger to `release.yml`.

Related pages: [Updates and auto-update](updates.en.md) (client behaviour and manifest format),
[Update manifest signing](update-signing.en.md) (signing and rotation),
[Website deployment](website-deploy.en.md) (deployment and the 404 config),
[Security Notes § release asset content scanning](security.en.md#release-asset-scan).
