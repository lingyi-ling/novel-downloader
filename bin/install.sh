#!/usr/bin/env bash
# One-line installer for Novel Downloader on Linux / macOS.
#
#   bash <(curl -sSL https://raw.githubusercontent.com/lingyi-ling/novel-downloader/main/bin/install.sh)
#
# Installs into ~/.noveldl/venv and links the entry points into
# ~/.local/bin (created when missing). No administrator rights required.

set -euo pipefail

REPO="lingyi-ling/novel-downloader"
VENV="$HOME/.noveldl/venv"
BIN_DIR="$HOME/.local/bin"

say()  { printf '==> %s\n' "$1"; }
ok()   { printf '    %s\n' "$1"; }
warn() { printf '    %s\n' "$1" >&2; }

# ---------------------------------------------------------------- python
say "Looking for Python 3.11+"
PYBIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
            PYBIN="$candidate"
            break
        fi
    fi
done
if [ -z "$PYBIN" ]; then
    warn "Python 3.11+ was not found."
    warn "Install it first (e.g. 'sudo apt install python3.12 python3.12-venv') and re-run."
    exit 1
fi
ok "using $("$PYBIN" --version)"

# ---------------------------------------------------------------- venv
say "Creating virtualenv at $VENV"
"$PYBIN" -m venv "$VENV"

say "Installing novel-downloader[api,music]"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -U "novel-downloader[api,music]"

# ---------------------------------------------------------------- wrappers
say "Linking entry points into $BIN_DIR"
mkdir -p "$BIN_DIR"
for entry in novel-cli novel-api novel-desktop; do
    ln -sf "$VENV/bin/$entry" "$BIN_DIR/$entry"
    ok "$BIN_DIR/$entry"
done

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not on your PATH yet. Add it with:"
        warn "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
        ;;
esac

"$BIN_DIR/novel-cli" --version && ok "verified"

cat <<'EOS'

Novel Downloader installed.
  CLI        : novel-cli
  REST API   : novel-api   (127.0.0.1:8081)
  Desktop    : novel-desktop  (needs a desktop session; uses pywebview)

Quick start: https://github.com/lingyi-ling/novel-downloader#-quick-start
EOS
