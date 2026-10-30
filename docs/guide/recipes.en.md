## Common Recipes

Copy-paste recipes for common tasks. Every command assumes `novel-cli` is on `PATH`; confirm with `where novel-cli` (Windows) or `which novel-cli`.

Configuration lookup order is `--config` file → `settings.toml` in the current directory → application home (`NOVEL_HOME` or the platform data directory). **Scheduled tasks and scripts must `cd` to the directory that holds the configuration first**, or use a command that accepts `--config`; otherwise they may read a different configuration.

---

### 1. Batch-download a reading list

#### Multiple book IDs on one site

`download --site` accepts several book IDs:

```bash
novel-cli download --site b520 8_8187 12345 23456
```

> `--start` / `--end` apply to the **first** `book_id` only. To give each book its own range, run separate commands or use structured `book_ids` in the configuration file.

#### Maintain the list in the configuration file (recommended)

```toml
# settings.toml
[sites.b520]
book_ids = ["8_8187", "12345", "23456"]

[sites.qidian]
book_ids = ["1010868264"]
```

```bash
# Omit book_ids to read them from the configuration
novel-cli download --site b520
novel-cli download --site qidian
```

#### Run across several sites

Git Bash:

```bash
for site in b520 qidian n23qb; do
  novel-cli download --site "$site" || exit 1
done
```

Windows `cmd.exe`:

```bat
for %s in (b520 qidian n23qb) do novel-cli download --site %s
```

PowerShell:

```powershell
foreach ($site in 'b520', 'qidian', 'n23qb') {
    novel-cli download --site $site
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

#### Download from a list of URLs

URL mode accepts **exactly one** URL per invocation, so loop:

```bash
# books.txt contains one book URL per line; blank lines are skipped
while IFS= read -r url; do
  [ -z "$url" ] && continue
  novel-cli download "$url" || exit 1
done < books.txt
```

PowerShell:

```powershell
Get-Content .\books.txt | Where-Object { $_.Trim() } | ForEach-Object {
    novel-cli download $_
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

---

### 2. Export only, without downloading

`export` reads existing raw data (`general.raw_data_dir`) and never touches the network; the book must have been downloaded first.

```bash
# Export a downloaded book as EPUB
novel-cli export --site b520 12345 --format epub

# Export both TXT and EPUB
novel-cli export --site b520 12345 --format txt epub

# Export from the raw stage (skip cleaner processing)
novel-cli export --site b520 12345 --format epub --stage raw
```

* Providing both `--site` and `book_ids` avoids the interactive selector, which makes the command script-friendly.
* The output directory is `general.output_dir` (default `./downloads`).
* `--start` / `--end` still apply to the first book only.
* If no raw data exists for the book, the command exits with code `1`.

Omitting `--site` or `book_ids` enters interactive mode; in a non-interactive shell, provide both.

---

### 3. Scheduled update checks (Windows Task Scheduler)

Approach: maintain the list with `follow`, then let a scheduled task run `follow check` periodically and append to a log.

#### Step 1: Add books to the follow list

```bat
novel-cli follow add b520 8_8187 --title "Example Novel"
novel-cli follow add qidian 1010868264 --title "Another Book"
novel-cli follow list
```

`--title` matters: `follow check` searches the site using it. Without a title it can only search by book ID, which is much less reliable.

#### Step 2: Create a batch script

Create `C:\novel\follow-check.cmd` (keep the path free of spaces):

```bat
@echo off
setlocal
cd /d "C:\novel"

rem Replace this path with the output of: where novel-cli
set "CLI=C:\Users\me\AppData\Local\Programs\Python\Python312\Scripts\novel-cli.exe"
set "LOG=C:\novel\follow.log"

echo [%DATE% %TIME%] follow check >> "%LOG%"
"%CLI%" follow check --all >> "%LOG%" 2>&1

rem Optional: resumable update of specific books (fetches only missing/new chapters)
"%CLI%" download --site b520 8_8187 >> "%LOG%" 2>&1

rem Or: let the CLI download books with new chapters itself, one at a time
rem "%CLI%" follow check --download --wait --wait-timeout 1800 >> "%LOG%" 2>&1

exit /b %ERRORLEVEL%
```

Notes:

* `cd /d "C:\novel"` makes `settings.toml`, `downloads`, `raw_data`, and `logs` resolve to the expected locations.
* `follow check --download` only **prints** download commands by default; it never downloads. For automatic updates, either add explicit `download` commands like the one above, or switch the scheduled task to `follow check --download --wait`: the CLI queues each updated book and waits for it to finish, one book at a time. `--wait-timeout` (default 1800 seconds) bounds the wait per book; a task that exceeds it is cancelled and counted as "cancelled" in the summary, without affecting the remaining books.
* `follow check` exits with `1` if any book's check fails, but the script continues to the download command; with `--wait`, the exit code is also `1` when any download did not succeed (failed/cancelled/timed out). The final code is visible in `follow.log`.

#### Step 3: Register the scheduled task

In an **elevated** command prompt:

```bat
schtasks /Create /TN "NovelFollowCheck" /SC DAILY /ST 08:00 /TR "C:\novel\follow-check.cmd" /F
```

Every six hours:

```bat
schtasks /Create /TN "NovelFollowCheck" /SC HOURLY /MO 6 /TR "C:\novel\follow-check.cmd" /F
```

Run once immediately, inspect, or delete:

```bat
schtasks /Run /TN "NovelFollowCheck"
schtasks /Query /TN "NovelFollowCheck" /V /FO LIST
schtasks /Delete /TN "NovelFollowCheck" /F
```

> By default the task runs only while the user is signed in. To run whether the user is signed in or not, edit the task in the Task Scheduler GUI and save the credentials.
>
> Output is appended to `C:\novel\follow.log`; the scheduled task itself never opens a window.

---

### 4. Connect the Android client to the computer

The Android client talks to the local REST API, so both devices must be on the same local network.

#### Step 1: Start the API on the local network

```bash
novel-api --host 0.0.0.0 --port 8081 --token "change-me"
```

> `--host 0.0.0.0` listens on every interface. Without a token, anyone on the same network can control downloads and read the export directory, so always set a token.

#### Step 2: Open the firewall port

Run in an elevated command prompt:

```bat
netsh advfirewall firewall add rule name="Novel API 8081" dir=in action=allow protocol=TCP localport=8081
```

Remove the rule:

```bat
netsh advfirewall firewall delete rule name="Novel API 8081"
```

#### Step 3: Find the computer's LAN IP

```bat
ipconfig
```

Look for the `IPv4 Address` of the active adapter (for example `192.168.1.23`). PowerShell equivalent:

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' } |
  Select-Object InterfaceAlias, IPAddress
```

#### Step 4: Fill in the address in the app

Open “Settings” in the app:

| Scenario | API address |
| --- | --- |
| Emulator accessing the computer | `http://10.0.2.2:8081/` |
| Physical device accessing the computer | `http://192.168.1.23:8081/` |

If the API was started with a token, enter the same value as the “API key”.

#### Step 5: Verify

On the computer:

```bash
curl http://127.0.0.1:8081/api/v1/health
```

On the phone, open `http://192.168.1.23:8081/docs` in its browser. Seeing the API documentation means the connection works.

---

### 5. Integrate the API into your own program

The API lives in the optional `api` extra:

```bash
pip install "novel-downloader[api]"
novel-api --port 8081 --token "my-secret"
```

When no token is configured, omit the `Authorization` header below (recommended only on `127.0.0.1`).

#### Search → queue → poll → fetch (curl)

```bash
BASE=http://127.0.0.1:8081
TOKEN=my-secret
AUTH="Authorization: Bearer $TOKEN"

# 1. Search
curl -s -X POST "$BASE/api/v1/search" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"keyword":"Battle Through the Heavens","sites":["qidian"],"per_site_limit":5}'

# 2. Queue a download task (the response contains task_id)
curl -s -X POST "$BASE/api/v1/tasks" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"title":"Battle Through the Heavens","site":"qidian","book_id":"123456"}'

# 3. Query the task list or a single task
curl -s "$BASE/api/v1/tasks" -H "$AUTH"
curl -s "$BASE/api/v1/tasks/<task_id>" -H "$AUTH"

# 4. List exported files and download one
curl -s "$BASE/api/v1/history" -H "$AUTH"
curl -o "output.txt" "$BASE/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt"
```

#### Python example (poll until the task finishes)

```python
import time

import requests

BASE = "http://127.0.0.1:8081"
HEADERS = {"Authorization": "Bearer my-secret"}

# Search and take the first result
resp = requests.post(
    f"{BASE}/api/v1/search",
    headers=HEADERS,
    json={"keyword": "Battle Through the Heavens", "sites": ["qidian"], "per_site_limit": 5},
    timeout=30,
)
resp.raise_for_status()
best = resp.json()["results"][0]

# Queue
resp = requests.post(
    f"{BASE}/api/v1/tasks",
    headers=HEADERS,
    json={"title": best["title"], "site": best["site"], "book_id": best["book_id"]},
    timeout=30,
)
resp.raise_for_status()
task_id = resp.json()["task_id"]

# Poll
while True:
    snapshot = requests.get(f"{BASE}/api/v1/tasks", headers=HEADERS, timeout=30).json()
    tasks = snapshot["pending"] + snapshot["running"] + snapshot["completed"]
    task = next((item for item in tasks if item["task_id"] == task_id), None)
    if task is None:
        raise RuntimeError("task disappeared")
    if task["status"] in {"completed", "failed", "cancelled"}:
        break
    time.sleep(2)

print(task["status"], task["progress"], task["exported_paths"])
```

Task fields: `task_id`, `title`, `site`, `book_id`, `status`, `chapters_done`, `chapters_total`, `progress`, `error`, `exported_paths`, `created_at`, `finished_at`. `status` is one of `queued`, `running`, `processing`, `exporting`, `completed`, `cancelled`, `failed`.

#### Follow endpoints

```bash
# Add a book to the follow list
curl -s -X POST "$BASE/api/v1/watchlist" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"site":"b520","book_id":"8_8187","title":"Example Novel"}'

# Check for new chapters; with download=true, updated entries include a ready-to-post task body
curl -s -X POST "$BASE/api/v1/watchlist/check" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"download":true}'
```

* `GET /api/v1/watchlist` lists every followed entry.
* `DELETE /api/v1/watchlist/{site}/{book_id}` removes an entry.
* `/watchlist/check` only checks and never downloads; entries in `needs_download` carry a `task` field that the caller submits itself.
* For live progress use SSE: `GET /api/v1/tasks/stream`; the search stream is `GET /api/v1/search/stream`. SSE requires custom request headers, so use a client that can set them.

`/downloads/*` is protected by the same token as `/api/v1/*` (send the same header when a token is set); still restrict sources with a firewall when exposing the API.

---

### 6. FAQ

**`novel-cli` is not recognized**

```bash
where novel-cli
```

If there is no output, add Python's `Scripts` directory (for example `C:\Users\me\AppData\Local\Programs\Python\Python312\Scripts`) to `PATH`, or call `novel-cli.exe` by absolute path.

**The scheduled task runs but nothing happens**

Check `follow.log`; make sure the script `cd`s to the configuration directory, uses the absolute path returned by `where novel-cli`, and inspect “Last Run Result” in Task Scheduler.

**`follow check` never finds a book**

Give the entry an accurate `--title`:

```bash
novel-cli follow add b520 8_8187 --title "Exact Book Title"
```

For sites that require a login, run `novel-cli login <site>` first.

**Configuration loading warns about `cookie` or `use_truncation`**

See the migration section in [Configuration](./configuration.md) and [Settings Reference](./settings-reference.md). Rename `cookie` to `cookies`, and move `use_truncation` under `[sites.<site>.parser]`.

**Frequent 403 / 503 responses**

Lower `general.max_rps`, increase `general.request_interval`, or switch the HTTP backend. See [Settings Reference](./settings-reference.md).

**Where are the exported files?**

By default under `./downloads` in the working directory; change `general.output_dir` to move them. `novel-cli storage` shows the actual paths.