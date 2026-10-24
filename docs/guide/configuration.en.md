## Configuration

`novel-downloader` is configured through `settings.toml`.

This chapter explains how to create, edit, and use the configuration file.

### 1. Initialize the Configuration File

Generate a default configuration file in any working directory:

```bash
novel-cli config init
```

To overwrite an existing file with the same name:

```bash
novel-cli config init --force
```

The command generates:

* `settings.toml`: the main configuration file

---

### 2. Edit `settings.toml`

The basic structure is shown below (replace `<site_name>` with the site you need):

```toml
[sites.<site_name>]
book_ids = [
  "123456",
  "234567"
]
login_required = false
```

The minimal configuration only needs `book_ids` for the target site:

```toml
[sites.linovelib]
book_ids = ["1234"]
```

You can then start a download through the CLI:

```bash
novel-cli download --site linovelib
```

> **Note**:
>
> If a book ID is also passed on the command line, for example
> `novel-cli download --site linovelib 5555`, the **command-line argument
> overrides `book_ids` from the configuration file**.

---

### 3. Login and Cookie Configuration

Some sites require a login to access all content. Configure it as follows.

#### Cookie cache

After a successful login, cookies are cached automatically in the working directory:

```bash
./novel_cache/<site_name>/*.cookies
```

If you encounter login issues, expired cookies, or simply do not want to reuse cached state, delete the corresponding `.cookies` file. The program will try to log in again or prompt for cookies on the next request.

#### 3.1 Log in with username and password

Suitable for sites that support form login, such as ESJ Zone and 百合会.

```toml
[sites.<site_name>]
login_required = true
username = "your_username"
password = "your_password"
```

You can also leave the credentials out of the configuration; the CLI prompts for them when needed.

---

#### 3.2 Log in with cookies

Suitable for sites with complex login flows (for example, CAPTCHAs or QR-code login).

Steps:

1. Log in to the target site in a browser.
2. Open the developer tools (`F12` / `CTRL + SHIFT + I`).
3. Copy the complete cookie string from a network request.
4. Add it to the configuration:

```toml
[sites.<site_name>]
login_required = true
cookies = "complete cookie string"
```

> Cookies configured this way are not renewed automatically; copy a fresh cookie string after they expire.

For more information, see: [Copy Cookies](./copy-cookies.md)

---

#### 3.3 Migrating old configurations: `cookie` → `cookies`

The loader only reads `cookies`. The old `cookie` key still lets the file load, but it is **never used**; configuration validation (`validate_config`) logs a warning such as:

```text
Configuration problem: sites.<site>.cookie: 'cookie' is not read by the loader (supported key is 'cookies').
```

Rename it manually:

```toml
[sites.<site_name>]
login_required = true
cookies = "complete cookie string"   # the old key cookie is deprecated
```

To check and preview migrations in bulk, call the built-in validation and migration helpers (neither writes the file for you):

```bash
# List every validation problem (including deprecated cookie / use_truncation)
python -c "from novel_downloader.infra.config import load_config, validate_config; [print(p) for p in validate_config(load_config())]"

# Preview what migrate_config would change (returns (migrated dict, change list))
python -c "from novel_downloader.infra.config import load_config, migrate_config; print(migrate_config(load_config())[1])"
```

`migrate_config()` performs these idempotent migrations:

| Old form | New form |
| --- | --- |
| `sites.<site>.cookie` | `sites.<site>.cookies` |
| `sites.<site>.use_truncation` | `sites.<site>.parser.use_truncation` |
| Missing `general.download_retry_times` | Filled with the default `2` |

> There is no separate `config validate` / `config migrate` CLI subcommand today. Validation runs automatically whenever the configuration is loaded (warnings only, never blocking), and migration is done through the Python functions above. You still need to write the migrated result back to `settings.toml` yourself.

### 4. Configuration File Lookup Order

On each run, the CLI looks for the configuration in the following order (highest to lowest priority):

(1) **A file explicitly passed on the command line**

```bash
novel-cli download --config path/to/settings.toml
```

(2) **`./settings.toml` in the current directory**

(3) **`settings.toml` in the application home** (`NOVEL_HOME` or the platform data directory; see [File Saving](./file-saving.md))

If more than one match exists, the higher-priority configuration overrides the lower-priority one.