# Website deployment

The BookTone website is the plain static site under `website/`: no build step, no package manager. It is served by nginx from `/var/www/noveldl` on the server, at <http://43.165.170.43/noveldl/>. The deployment script `scripts/deploy_site.py` only syncs files; it **never modifies the nginx configuration**.

## Deployment manifest

`SITE_FILES` in `scripts/deploy_site.py` is an explicit allowlist. Only the files listed there are uploaded:

| File | Purpose |
| --- | --- |
| `index.html` | Home page |
| `404.html` | Static 404 page |
| `main.js` | Interaction script |
| `style.css` | Styles |
| `favicon.svg` | Site icon |
| `og-image.png` | Social sharing image |
| `robots.txt` | Crawler rules |
| `sitemap.txt` | Sitemap |

`404.html` is already in the list and is uploaded by every deployment. Any new file under `website/` must also be added to `SITE_FILES`, otherwise it is never published; `--dry-run` prints the exact file names that would be uploaded, so use it to confirm the manifest.

## Running a deployment

```bash
# List the files that would be uploaded, without connecting
.venv/Scripts/python.exe scripts/deploy_site.py --dry-run

# Upload for real
.venv/Scripts/python.exe scripts/deploy_site.py
```

The script uploads to a temporary directory under `/tmp` on the server, checks that every file arrived, then assembles the new site: the new directory starts as a copy of the current docroot, this run's files are copied on top, and two renames atomically replace `/var/www/noveldl`. A failed swap restores the old directory, so the site is never left half-updated. The remote step runs under `sudo -n`, so the deploy user needs passwordless sudo for the docroot.

`paramiko` is only needed for deployment; install the extra if it is missing:

```bash
pip install -e .[deploy]
```

Authentication prefers an SSH key/agent; only when no key is available does it read `NOVELDL_DEPLOY_PASSWORD` from the environment. The password is never written to disk, logs, or the command line.

## Why an uploaded 404.html still does not appear

Uploading `404.html` only means the file exists at `/var/www/noveldl/404.html` on the server. When a path does not exist, nginx returns its own built-in 404 page by default and **does not automatically read a file of the same name from the docroot**. The error page must be named explicitly in the server-side nginx configuration, for example inside the site's `server` block:

```nginx
error_page 404 /noveldl/404.html;

location = /noveldl/404.html {
    internal;
}
```

- The second argument to `error_page` is a **URI**, not a filesystem path; the site is mounted under the `/noveldl/` prefix, so it must be `/noveldl/404.html`.
- `location = /noveldl/404.html { internal; }` reserves that URI for nginx's internal error handling: a client requesting it directly does not get the page with `200`, it still gets `404`.
- This configuration is **not part of this repository**. It currently lives on the server at `/etc/nginx/sites-available/parfait.conf`. `scripts/deploy_site.py` only has passwordless sudo for the docroot; it replaces files under `/var/www/noveldl` and neither can nor should touch nginx.
- After editing, validate the syntax and reload; this requires server administrator rights and cannot be done by the deploy script:

  ```bash
  sudo nginx -t
  sudo systemctl reload nginx
  ```

- This is a one-time server-side configuration. Later deployments only replace the docroot contents and neither overwrite nor undo it; conversely, redeploying will not add it for you.

## Verification

Once the configuration is active, every nonexistent path should return `404` with the body of `website/404.html`. `curl -I` alone only shows the status code and cannot prove the body is the custom page, so check all of the following:

```bash
# 1) Nonexistent path: the status code must be 404
curl -s -o /dev/null -w '%{http_code}\n' \
  http://43.165.170.43/noveldl/__no_such_page__

# 2) The response body must be the custom 404 page
curl -s http://43.165.170.43/noveldl/__no_such_page__ | grep -F "404 · 页面未找到"

# 3) Byte-for-byte check of the live error page against the source
curl -s http://43.165.170.43/noveldl/__no_such_page__ -o /tmp/live-404.html
cmp /tmp/live-404.html website/404.html && echo "404 page OK"

# 4) internal is effective: requesting 404.html directly still returns 404 (not 200)
curl -s -o /dev/null -w '%{http_code}\n' \
  http://43.165.170.43/noveldl/404.html
```

If step 1 returns `404` but step 2 does not find the custom page, nginx is usually not loading `error_page`. Confirm the directive is in the correct `server` block, that nginx was reloaded, and inspect the running configuration:

```bash
sudo nginx -T | grep -n "error_page"
```

The home page should still match byte for byte:

```bash
curl -s http://43.165.170.43/noveldl/index.html -o /tmp/live-index.html
cmp /tmp/live-index.html website/index.html && echo "index OK"
```

## Relationship to the release pipeline

When `website/` changes, run `scripts/deploy_site.py` after the gates pass, then use the `curl` / `cmp` checks above to confirm what is live. Version bumps, the three-platform builds, and the GitHub release are handled by `scripts/release.py` and are a separate flow from this static-site deployment.