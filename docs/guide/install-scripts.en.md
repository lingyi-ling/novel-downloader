## Windows Install Scripts

This page describes the PowerShell scripts in the repository's `packaging/`
directory for installing the standalone `novel-cli` / `novel-api` builds.
They target the Windows onedir bundles produced by `scripts/build_release.py`,
do not require a Python environment, and do not require administrator rights.

| Script | Purpose |
| --- | --- |
| `packaging/install-cli.ps1` | Install `novel-cli`, add it to the user `PATH`, verify `--version` |
| `packaging/install-api.ps1` | Install `novel-api`, optionally register a logon task |
| `packaging/verify.ps1` | Verify `SHA256SUMS.txt` and zip archive integrity |

> The scripts only touch local files, the current user's `PATH`, user
> environment variables and the current user's Task Scheduler jobs. They do not
> need, and should not be run with, administrator rights.

---

### Prerequisites

**System requirements**

* Windows 10 / 11 (x64)
* Windows PowerShell 5.1 (built in) or PowerShell 7+
* The published executables are PyInstaller onedir bundles; the sibling
  `_internal` directory must stay next to the executable

**Release layout**

`scripts/build_release.py` produces:

```text
novel-downloader-<version>-windows-x64/
├── novel-cli-<version>-windows-x64.zip
├── novel-api-<version>-windows-x64.zip
├── SHA256SUMS.txt
└── manifest.json
```

Extracting either zip yields a top-level `novel-cli/` or `novel-api/` folder;
the installer detects it automatically.

**Execution policy**

If scripts are blocked, use a process-scoped `Bypass` (applies only to that one
command):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\install-cli.ps1 -Dir .\dist\novel-cli
```

Or unblock the individual files:

```powershell
Unblock-File .\packaging\*.ps1
```

---

### Verify the release first

Verify a downloaded or locally built release before installing. By default the
scripts read `SHA256SUMS.txt` from the current directory and check every file it
lists:

```powershell
cd C:\Path\To\novel-downloader-1.0.0-windows-x64
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Path\To\repo\packaging\verify.ps1
```

Verify the output of a local build:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\verify.ps1 `
    -ReleaseDir .\dist\release\novel-downloader-1.0.0-windows-x64
```

Parameters:

| Parameter | Description |
| --- | --- |
| `-ReleaseDir <directory>` | Directory containing `SHA256SUMS.txt`; defaults to the current directory |
| `-ChecksumFile <file>` | Explicit checksum file; archive names are resolved relative to this file's directory |
| `-SkipZipTest` | Compare SHA-256 only; do not open the zip archives |

What is checked:

1. Every non-empty, non-comment line is parsed as `<sha256>  <filename>`
   (the `*filename` binary marker is accepted).
2. Each file is hashed with `Get-FileHash -Algorithm SHA256` and compared.
3. Each listed `.zip` is opened and every entry is fully read, which detects
   truncated or corrupted compressed data.

Every problem is reported and the script exits non-zero if anything failed.

---

### Install novel-cli

From a release zip:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\install-cli.ps1 `
    -Zip "$HOME\Downloads\novel-cli-1.0.0-windows-x64.zip"
```

From a local build directory:

```powershell
.\packaging\install-cli.ps1 -Dir .\dist\novel-cli
```

Parameters:

| Parameter | Description |
| --- | --- |
| `-Zip <file>` | Release zip; mutually exclusive with `-Dir` |
| `-Dir <directory>` | Built bundle directory containing `novel-cli.exe`; mutually exclusive with `-Zip` |
| `-InstallRoot <directory>` | Install root, default `%LOCALAPPDATA%\Programs\novel-downloader` |
| `-NoPath` | Do not modify the user `PATH`; intended for tests or when another tool manages `PATH` |
| `-Force` | Terminate `novel-cli.exe` started from the destination before replacing it |

What it does:

1. Installs into `%LOCALAPPDATA%\Programs\novel-downloader\novel-cli\`.
2. Adds that directory to the **current user's** `PATH` (user-level environment
   variable, idempotent, added at most once).
3. Runs `%LOCALAPPDATA%\Programs\novel-downloader\novel-cli\novel-cli.exe --version`
   as a verification step.
4. Exits non-zero if any step fails.

On upgrade, the old directory is renamed to a temporary backup before the new
one is moved into place. If the backup cannot be removed because a file is in
use, the script warns and leaves it on disk.

> `-Force` terminates running `novel-cli` processes and may interrupt a download.
> Prefer closing tasks manually before upgrading.

**PATH changes require a new terminal**

The script updates the user environment variable; already-open terminals keep
their old environment. After installation:

1. Close and reopen PowerShell / Windows Terminal / cmd.
2. Verify:

```powershell
where.exe novel-cli
novel-cli --version
```

If the command is still missing, sign out and back in, or restart Explorer. To
use it in the current session only:

```powershell
$env:Path += ";$env:LOCALAPPDATA\Programs\novel-downloader\novel-cli"
```

---

### Install novel-api

The basic installation matches the CLI:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\install-api.ps1 `
    -Zip "$HOME\Downloads\novel-api-1.0.0-windows-x64.zip"
```

Or:

```powershell
.\packaging\install-api.ps1 -Dir .\dist\novel-api
```

It installs into `%LOCALAPPDATA%\Programs\novel-downloader\novel-api\`, adds
that directory to the user `PATH`, and verifies the executable with
`novel-api.exe --help`.

In addition to `-Zip`, `-Dir`, `-InstallRoot`, `-NoPath` and `-Force`:

| Parameter | Description |
| --- | --- |
| `-RegisterStartup` | Create/replace a per-user logon task; requires `-Token` |
| `-Token <string>` | API token stored in the user `NOVEL_API_TOKEN` environment variable; also persisted when used without `-RegisterStartup` |
| `-TaskName <name>` | Task Scheduler job name, default `novel-downloader-api` |

**Register the logon task**

```powershell
.\packaging\install-api.ps1 -Dir .\dist\novel-api -RegisterStartup -Token (New-Guid).Guid
```

This does three things:

1. Installs/replaces `novel-api`.
2. Writes the token to `NOVEL_API_TOKEN` under `HKCU\Environment` (the value is
   never printed).
3. Creates a per-user task with `schtasks /Create /SC ONLOGON /F`. The task
   action contains only the full path to `novel-api.exe`; the token is inherited
   from the user environment at logon and is not written into the task
   definition.

The task starts at the **next logon**. To start it immediately:

```powershell
schtasks /Run /TN "novel-downloader-api"
```

Check the service (defaults to `127.0.0.1:8081`):

```powershell
curl.exe -H "X-API-Key: <your-token>" http://127.0.0.1:8081/api/v1/health
```

Stop the current instance without deleting the task:

```powershell
schtasks /End /TN "novel-downloader-api"
```

**Output directory caveat**

The logon task does not set a working directory. The API's
`general.output_dir` defaults to the relative path `./downloads`; if the config
still uses a relative path, it may resolve against the task's working directory
and exports may land somewhere unexpected or fail to be created.

Before enabling `-RegisterStartup`, set an absolute path in
`%LOCALAPPDATA%\novel_downloader\settings.toml`:

```toml
[general]
output_dir = 'D:\Novels\downloads'
```

**Token and security**

* `NOVEL_API_TOKEN` is stored unencrypted in the current user's environment;
  other processes running as that user can read it.
* Passing `-Token` on the command line may leave it in PowerShell history. Use a
  random token and store it safely.
* The API binds to `127.0.0.1` by default. Do not expose it on another address
  without a token.
* The task runs as the current user at logon and stops at logoff; no
  administrator rights are required.

---

### Upgrade

1. Close running `novel-cli` and stop `novel-api` (or use `-Force`):

   ```powershell
   schtasks /End /TN "novel-downloader-api"
   ```

2. Verify the new release with `verify.ps1` as described above.
3. Re-run the same install command. The whole bundle is replaced; configuration
   and user data are untouched:

   ```powershell
   .\packaging\install-cli.ps1 -Zip .\novel-cli-<new-version>-windows-x64.zip
   .\packaging\install-api.ps1 -Zip .\novel-api-<new-version>-windows-x64.zip
   ```

4. The task points at the stable install path, so re-registration is normally
   unnecessary. If the token changed or the task was deleted, re-run the install
   with `-RegisterStartup -Token <new-token>`.
5. Open a new terminal and confirm with `novel-cli --version`.

`-NoPath` is for testing; normal upgrades do not need it. Re-running the
installer never duplicates the `PATH` entry.

---

### Uninstall and cleanup

Close the processes, then remove the task and the environment variable:

```powershell
schtasks /Delete /TN "novel-downloader-api" /F
[Environment]::SetEnvironmentVariable('NOVEL_API_TOKEN', $null, 'User')
```

Remove both install directories from the user `PATH`:

```powershell
$root = Join-Path $env:LOCALAPPDATA 'Programs\novel-downloader'
$remove = @((Join-Path $root 'novel-cli'), (Join-Path $root 'novel-api'))
$current = [Environment]::GetEnvironmentVariable('Path', 'User')
$kept = @($current -split ';' | Where-Object {
    $p = $_.Trim().Trim('"')
    $p -and ($remove -notcontains $p.TrimEnd('\'))
})
[Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
```

Delete the program files:

```powershell
Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs\novel-downloader') -Recurse -Force
```

Optionally delete user data (this removes configuration, download history,
caches and login sessions):

| Content | Default location |
| --- | --- |
| Configuration, state, caches, logs | `%LOCALAPPDATA%\novel_downloader\` |
| Default download/export directory | `general.output_dir`, default relative `./downloads` |
| Music data and login sessions | `%LOCALAPPDATA%\novel_downloader\music\` |
| Custom data roots | Locations pointed to by `NOVEL_HOME` / `NOVEL_MUSIC_HOME` |

Cleanup checklist:

1. Stop `novel-cli` / `novel-api` processes.
2. Delete the `novel-downloader-api` task.
3. Delete the `NOVEL_API_TOKEN` user environment variable.
4. Remove `...\novel-downloader\novel-cli` and `...\novel-downloader\novel-api`
   from the user `PATH`.
5. Delete `%LOCALAPPDATA%\Programs\novel-downloader`.
6. Optionally delete `%LOCALAPPDATA%\novel_downloader` and any custom output
   directory.
7. If a custom `-InstallRoot` was used, delete that directory and its `PATH`
   entries instead.

Apart from the user environment variables (`PATH`, `NOVEL_API_TOKEN`), the scripts write no registry keys and install no system services.

---

### Troubleshooting

**"... is running ..."**

Executables in the destination are still running or otherwise locked. Close the
application and retry, or use `-Force`. `-Force` interrupts downloads and
in-flight API requests.

**`novel-cli` is not recognized**

`PATH` changes apply only to newly started processes. Open a new terminal; if
that does not help, sign out and back in. You can always use the full path:

```powershell
& "$env:LOCALAPPDATA\Programs\novel-downloader\novel-cli\novel-cli.exe" --version
```

**The exe cannot be found after extracting `-Zip`**

The installer looks for `novel-cli.exe` / `novel-api.exe` at the archive root or
in a single direct child directory. If a zip contains several candidates,
extract it and point `-Dir` at the correct bundle directory.

**Antivirus false positives**

Standalone PyInstaller executables are occasionally flagged. Obtain the release
from a trusted source, verify `SHA256SUMS.txt` first, and add an exclusion or use
the `pip install` route if necessary.

**The logon task exists but did not start**

The task triggers at user logon and does not run immediately. Start it with
`schtasks /Run /TN "novel-downloader-api"` or sign out and back in. Signing out
and back in also guarantees that processes see the newly written user
environment variable.

**Port 8081 is already in use**

The default is `127.0.0.1:8081`. Diagnose with
`novel-api --port <port>` manually, or edit the task action/properties. The
installer registers the task with the default arguments.