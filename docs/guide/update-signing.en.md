# Update manifest signing

The update manifest `latest.json` supports **Ed25519** signatures. It is **off by default**:
`updater.TRUSTED_UPDATE_KEYS` is empty, so an unsigned manifest is accepted and the trust
anchor is HTTPS plus each asset's SHA-256.

As soon as a trusted public key is compiled into the client, the manifest becomes the **only**
trust anchor: an unsigned manifest, a bad signature, an unknown key — or no manifest at all —
makes the client refuse the update, and it **stops falling back to the GitHub API**.

This page is for two audiences: users who want to know what the mechanism actually protects, and
maintainers who have to enable or rotate the key. The release pipeline itself is described in
[Updates and auto-update](updates.en.md).

!!! note "Current status"
    Signing is implemented and tested but **not enabled** (`TRUSTED_UPDATE_KEYS` is empty), so
    today's clients behave exactly as they did before signatures existed. Section 3 covers
    enabling it.

---

## 1. Manifest format

The manifest is a small JSON document published with every release at a stable URL:

```text
https://github.com/lingyi-ling/novel-downloader/releases/latest/download/latest.json
```

### Fields

| Field | Type | Signed | Meaning |
| --- | --- | --- | --- |
| `schema` | integer | yes | Manifest format version, currently always `1`; must be **exactly** the version the client knows — `true`, `1.0` and `2` are all refused |
| `version` | string | yes | Version, e.g. `1.4.1` |
| `tag` | string | yes | Git tag, e.g. `v1.4.1`; with `v` stripped it must equal `version` |
| `repo` | string | yes | `owner/name`; must match the repository the client asked about |
| `generated_from` | string | yes | Where the document came from (usually the release page); informational |
| `platforms` | object | yes | Keys are `<platform>[-<shape>]`, values are `{name, url, sha256, size}` |
| `notes_url` | string | yes (optional) | Release notes URL |
| `pub_date` | string | yes (optional) | Publish date; informational |
| `key_id` | string | **no** | Identifies the signing key; the client uses it to look up the public key |
| `signature` | string | **no** | base64 of the signature |

Every `platforms` entry is signed in full:

| Member | Constraint | If violated |
| --- | --- | --- |
| `name` | Plain file name, no `/`, `\` or NUL | Entry unusable (skipped) |
| `name` | Must match the shape its key claims (a `windows-x64-installer` entry cannot name an APK) | **Whole manifest refused** |
| `url` | Must be `https://`, with no query or fragment | Entry unusable (skipped) |
| `url` | Must be `https://github.com/<repo>/releases/download/<tag>/<name>`, with repo, tag and file name all agreeing with the manifest itself | **Whole manifest refused** |
| `sha256` | 64 lowercase hex characters | Entry unusable (skipped) |
| `size` | Optional positive integer | Missing/invalid just means no size hint; entries above 1 GiB are unusable |

The difference between "entry skipped" and "whole manifest refused" is deliberate: a
structurally unusable entry cannot be used safely, so ignoring it is enough, while an entry
pointing somewhere else means the document itself is not trustworthy and dropping one row would
not help. Manifests produced by the release script hit neither case (a missing digest aborts the
release).

### What the signature covers, and the canonical JSON

The signature covers **every field except `key_id` and `signature`**, including each
`platforms` entry's `name` / `url` / `sha256` / `size`. Retargeting a download, swapping a
digest, dropping a platform or adding a field all invalidate it.

The signed input is not the bytes on disk. It is the **parsed object re-serialised** in
canonical form:

```python
json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

That is: keys sorted recursively, no space after `:` or `,`, and non-ASCII characters emitted
as raw UTF-8 (no `\u` escaping). The file on disk is those canonical bytes plus one trailing
newline; the **newline is not signed**, because it is not part of the re-serialisation.

!!! warning "Verifiers must use the same serialisation"
    The signature is over the parsed document, not over file bytes. Any verifier must
    re-serialise with the rule above or it will only ever report "does not verify". Today the
    only verifiers are the Python client (`updater.verify_manifest_signature`) and the release
    script (`build_update_manifest.verify_document`), which share one implementation.

### Key formats and `key_id`

| Item | Format |
| --- | --- |
| Private key | 32-byte Ed25519 **seed**, 64 hex characters, written by `--keygen` |
| Public key | 32-byte **raw** Ed25519 public key, 64 hex characters |
| Signature | 64-byte RFC 8032 signature, standard base64 (decoded with `validate=True`, so newlines and spaces are rejected) |
| `key_id` | `sha256(32-byte raw public key).hexdigest()[:16]` — 16 hex characters |

`key_id` is only a selector for "which public key", and is deliberately not covered by the
signature. The client uses it to find the public key in `TRUSTED_UPDATE_KEYS`, then verifies
with that key. Relabelling a valid signature with a different **trusted** `key_id` therefore
still fails: the key that gets looked up does not match the signer.

### Example

An unsigned manifest (the default state):

```json
{"generated_from":"https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.4.1","platforms":{"windows-x64-installer":{"name":"NovelDownloader-setup-1.4.1-x64.exe","sha256":"…","size":43259516,"url":"https://github.com/lingyi-ling/novel-downloader/releases/download/v1.4.1/NovelDownloader-setup-1.4.1-x64.exe"}},"repo":"lingyi-ling/novel-downloader","schema":1,"tag":"v1.4.1","version":"1.4.1"}
```

Signing adds exactly two fields:

```json
{"key_id":"0123456789abcdef","signature":"<base64, 64 bytes>", "...": "every other field unchanged"}
```

---

## 2. Behaviour once enabled

While `TRUSTED_UPDATE_KEYS` is non-empty, `check_for_update` treats the manifest as the only
trust anchor. Every rejection happens **before any asset is downloaded**:

| Situation | Result | `UpdateCheck.error` |
| --- | --- | --- |
| Valid signature whose `key_id` is in the table | Accepted, `source="manifest"` | `None` |
| No `signature` / `key_id` | Refused | `untrusted-manifest: manifest is not signed but a trusted key is configured` |
| `key_id` not in `TRUSTED_UPDATE_KEYS` | Refused | `untrusted-manifest: manifest is signed with an unknown key: …` |
| Signature does not match (edited content, wrong key, broken base64) | Refused | `untrusted-manifest: manifest signature does not verify: …` |
| The table's public key itself is malformed (not hex, wrong length) | Refused (fail-closed, never crashes) | `untrusted-manifest: manifest signature does not verify: …` |
| Valid signature but bad `schema` / `tag` / `repo` / URL | Refused | `untrusted-manifest: …` (specific reason) |
| Manifest **cannot be fetched** (404, network failure, non-HTTPS redirect, over 1 MiB) | Refused | `manifest-unavailable` |

The important part: **none** of these ever requests
`https://api.github.com/repos/…/releases/latest`. That is the whole point — otherwise an
attacker who deletes `latest.json` would push the client onto the API path they also control,
and signature checking would be decorative.

!!! danger "Hard requirement once enabled: every release must ship `latest.json`"
    With signing on, forgetting the manifest means **auto-update stops** for every client with a
    pinned key. `scripts/release.py` already rebuilds `latest.json` from the assets actually
    being uploaded and uploads it **last**, so everything it points at is already in place. Do
    not delete it or skip it by hand.

### What losing the private key means

The private key is the only thing that can sign a manifest the client accepts; the client only
knows the public key. Therefore:

- Losing the private key means **no new manifest can ever be produced that those clients
  accept**. Clients with the pinned key report `untrusted-manifest` or `manifest-unavailable`
  and **do not** fall back to the API.
- There is no remote reset. Recovery means **hand-distributing** a build with a new public key
  compiled in; only after users install it do they trust the new signature.
- Old versions do not recover on their own — they stay on the last version they could verify.

So the private key must be backed up offline (at least two copies, in separate places) and must
never enter the repository, CI variables, logs or screenshots.

---

## 3. Enabling it

### 3.1 Generate the key pair

```bash
python scripts/build_update_manifest.py --keygen ~/.novel_downloader/update.key
```

Output looks like:

```text
private seed: C:\Users\<you>\.novel_downloader\update.key (keep it out of the repository)
public key  : <64 hex characters>
key id      : <16 hex characters>
trusted key entry for updater.TRUSTED_UPDATE_KEYS:
    "<key id>": "<public key>",
```

Details:

- The seed is created with `O_EXCL`, so the script **refuses to overwrite** an existing file;
  move the old one away yourself if you want a different path.
- It is created with `0600` on POSIX; Windows ignores the creation mode, so the script
  re-asserts `chmod 0600` (best effort) and the real permissions come from the parent
  directory's ACL — check them yourself.
- Only the seed's **path** is printed, never its contents.

### 3.2 Compile the public key into the client

Paste the printed line into `src/novel_downloader/apps/common/updater.py`:

```python
TRUSTED_UPDATE_KEYS: Mapping[str, str] = {
    "<key id>": "<public key>",
}
```

This is a constant **compiled into the program**. It is not a `settings.toml` option, and there
is no environment variable or runtime switch. Changing it requires rebuilding and shipping the
client — which is exactly the point of pinning: a running program cannot be talked into trusting
another key.

After editing, run the tests that pin the format and the fail-closed behaviour:

```bash
.venv/Scripts/python.exe -m pytest tests/apps/test_update_manifest_signature.py -q
.venv/Scripts/python.exe -m pytest tests/test_build_update_manifest.py -q
```

### 3.3 Sign at release time

```bash
# One-shot release: build → rebuild latest.json → sign → audit → tag → upload
python scripts/release.py --sign-key ~/.novel_downloader/update.key
```

On success it prints `==> latest.json signed (key id <id>)`; `--dry-run` tells you up front
whether this run would sign. A signing failure (unreadable file, not hex, not 32 bytes) aborts
the release **before the tag is created**.

To build and sign the manifest on its own:

```bash
python scripts/build_update_manifest.py \
    --release-json payload.json --version 1.4.1 --tag v1.4.1 \
    --repo lingyi-ling/novel-downloader \
    --sign-key ~/.novel_downloader/update.key \
    --out latest.json
```

### 3.4 Verify a published manifest {#verify-published-manifest}

`--check` re-derives a manifest from the release payload and compares it field by field with the
existing file (ignoring `signature` / `key_id`); `--public-key` additionally verifies the
signature:

```bash
python scripts/build_update_manifest.py \
    --release-json payload.json --version 1.4.1 --tag v1.4.1 \
    --repo lingyi-ling/novel-downloader \
    --check latest.json --public-key <64 hex public key>
```

`payload.json` is the API response for `releases/tags/v1.4.1`. Exit code `0` means the document
matches and the signature is valid; a mismatch prints `stale: <field>` and a bad signature prints
`stale: the existing manifest signature does not verify against the given public key`.

### 3.5 Enabling checklist

- [ ] The private key is backed up offline and confirmed absent from the repository, CI
      variables, logs and screenshots.
- [ ] The public key and `key_id` are in `TRUSTED_UPDATE_KEYS`, and the tests pass.
- [ ] The release process always passes `--sign-key` and always uploads `latest.json`.
- [ ] At least one published manifest has been re-checked with `--check --public-key`.
- [ ] You accept that, once enabled, a missing manifest means those clients stop auto-updating.

---

## 4. Rolling releases and key rotation

### 4.1 Why you cannot just switch keys

A manifest carries **one** signature and one `key_id`, so there is no transitional format with
both old and new signatures. If a release is signed with a new key straight away, clients that
still trust the old key will:

1. see a `key_id` that is not in their table → refuse the manifest;
2. **not** fall back to the API;
3. stop auto-updating from then on.

That is deliberate fail-closed behaviour, and it means rotation must be staged.

### 4.2 Three-phase rotation

| Phase | Client change | Manifest signed with | Effect |
| --- | --- | --- | --- |
| A (bridge release) | `TRUSTED_UPDATE_KEYS` holds **both** the old and the new public key | old | Existing clients upgrade to the bridge release, which trusts both keys |
| B (switch) | none | **new** | The bridge release and newer clients verify; anything older than the bridge release starts refusing the manifest and stops auto-updating |
| C (cleanup) | remove the **old** public key | new | Only the new key is trusted; leaking the old private key no longer matters |

Phase A must ship **before** phase B, with enough time for users to update. The manifest has no
"minimum client version" field and clients do not report their version, so this threshold can
only be judged **by hand over a time window** — the manifest cannot enforce it.

### 4.3 On client version gates

Schema `1` has no `min_client_version` and the client does not "ask to upgrade" when it is too
old — it simply refuses the manifest. Consequently:

- After phase B, users stuck before the bridge release lose auto-update **silently** (the
  desktop app shows no error bar) and must download a new build by hand.
- If that cost is unacceptable, do not rotate — or treat the bridge release as a forced
  distribution and say so in the release notes.
- Expressing a version gate in the manifest later would require changing both `schema` and the
  client parser; current clients reject a mismatched `schema` outright, so bumping `schema` is
  itself a full-rotation-scale operation.

---

## 5. Client differences

| Client | Reads `latest.json` | Verifies signature | When the manifest is unusable |
| --- | --- | --- | --- |
| Desktop | yes (`update_state` / `check_update_async` go through `check_for_update`) | yes | No API fallback; the reason lands in `update_state`'s `error` |
| CLI | yes (`novel-cli update` and the post-command hint both use `check_for_update`) | yes | No API fallback; `novel-cli update` prints `Update check failed: untrusted-manifest: …`, the silent hint prints nothing |
| Android | yes | **no** | Always falls back to the GitHub API |

Stated plainly:

- **Android does not verify signatures today.** `UpdateChecker.parseManifest` checks `schema`,
  `version`, `platforms` and the HTTPS/digest requirements only; it ignores `key_id` and
  `signature` entirely. Unknown keys are tolerated, so a signed manifest will not break parsing —
  but it also gains **no** protection. Android's trust anchor remains HTTPS plus the APK's
  SHA-256. Until Android implements verification, enabling signing protects the desktop app and
  the CLI only.
- The desktop app's legacy `check_update` bridge (only used when the front end lacks
  `check_update_async`) reads the GitHub API directly and never goes through
  `check_for_update`, so it is outside the pinned-key protection. Desktop code and front-end
  assets ship together, so real builds never take that path — but it marks the boundary of
  "verification lives in `check_for_update`".
- The desktop UI does not surface check errors as a notice: on a signature failure the sidebar
  simply never shows "update available". For the specific reason use `novel-cli update --json`
  (same logic) or read the `error` field of `update_state`.
- Library callers and the local API do not check for updates and are unaffected.

---

## 6. Troubleshooting {#signing-troubleshooting}

Two rules first: **never change the keys to make a check pass**, and **never print or paste the
private key**. `key_id` and the public key are public information and are safe in logs and
issues.

### Symptom table

| Symptom | Most likely cause | Fix |
| --- | --- | --- |
| CLI says `untrusted-manifest: manifest is not signed but a trusted key is configured` | The release was published without `--sign-key`, or the manifest was replaced with an unsigned one | Rebuild with `--sign-key` and re-upload `latest.json` |
| CLI says `untrusted-manifest: manifest is signed with an unknown key: '<id>'` | The signing key differs from the one compiled into the client (old key, test key, or a client that predates trusting the new key) | Compare the `key_id` with the table's key; check whether that client version has the key built in |
| CLI says `untrusted-manifest: manifest signature does not verify: …` | The document was modified after signing (regenerated, hand-edited, an old file uploaded), or the table's public key does not belong to the private key used | Rebuild and re-sign from the release payload, then re-verify; do not edit the manifest |
| CLI says `manifest-unavailable` | The manifest cannot be fetched: the release has no `latest.json`, is still a draft, the upload failed, the network is down, or a redirect went to plain HTTP | Confirm the release is published and the asset exists; confirm `github.com` is reachable |
| Desktop shows no "update available" bar and no error | The check was refused and the desktop UI does not display check errors | Run `novel-cli update --json` and read `error` |
| `release.py` aborts with `cannot sign latest.json: …` before tagging | The private key file is unreadable, not hex, or not 32 bytes | Check the path and permissions; confirm the file holds the 64 characters `--keygen` wrote |

### Diagnosing

1. **Confirm the manifest really is signed** (public fields only, never the private key):

   ```bash
   python -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('key_id'), 'signature' in d)" latest.json
   ```

2. **Confirm `key_id` matches the compiled public key.** `key_id` is the first 16 hex characters
   of the public key's digest:

   ```bash
   python -c "import hashlib,sys; print(hashlib.sha256(bytes.fromhex(sys.argv[1])).hexdigest()[:16])" <public key hex>
   ```

   A mismatch means a different key signed it — check which file `--sign-key` pointed at during
   the release, rather than editing the public key in the client.

3. **Verify the signature with the public key**, never touching the private key: see
   [3.4](#verify-published-manifest).

4. **Rule out "edited after signing".** The signature covers every field, so any regeneration —
   even one that is semantically equivalent — changes the canonical bytes. Rebuild from the
   release payload instead of editing the signed file.

5. **Tell "signature refused" from "manifest missing".** The first is
   `untrusted-manifest: …` (the document arrived but is not trusted); the second is
   `manifest-unavailable` (nothing arrived). They have completely different fixes.

### Do not

- Do not empty `TRUSTED_UPDATE_KEYS` to "get updates working again" — that disables verification
  permanently, and shipped clients cannot be changed remotely.
- Do not put the private key anywhere but an offline backup, and do not `cat` it into a terminal
  or an issue "just to check".
- Do not hand-edit a signed `latest.json`; rebuild and re-sign.
- Do not delete or skip `latest.json` as a temporary workaround.

---

## 7. What it protects, and what it does not

**Protects:** if the repository or the publishing account is compromised, the attacker can
rewrite `latest.json` and replace assets, but without the private key cannot produce a manifest
clients accept. Because a failed verification does not fall back to the API, deleting the
manifest does not push clients onto the other path the attacker controls either. The signature
binds `name` / `url` / `sha256` / `size` together, so "an installer from elsewhere plus a
self-consistent digest" is refused.

**Does not protect:**

- **Android clients** — no verification today (see section 5).
- **A compromised signing machine or private key** — whoever holds the key can sign valid
  manifests; that is this mechanism's root of trust.
- **Manual downloads by users** — everything outside the update path is unconstrained.
- **A replaced client binary** — the public key is compiled in, and anyone who can replace the
  binary does not need the update path at all.
- **Availability** — `manifest-unavailable` fails that check with no fallback. That is the price
  of fail-closed, not a defect.

For the full update-chain threat model see
[Security Notes § update chain threat model](security.en.md#update-chain-threat-model); for the
release-side gates see
[Updates and auto-update § pre-release asset audit](updates.en.md#pre-release-asset-audit).
