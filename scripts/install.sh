#!/bin/sh
# Install or update ITU MCP + PaperBridge for the current user on Linux or macOS.
#
#   curl -LsSf https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install.sh | sh
#
# It installs uv (https://docs.astral.sh/uv/) if it is missing, installs this
# fork from GitHub as a uv tool (uv fetches a suitable Python when needed), and
# then runs `itu-mcp --setup`, which asks for your ITU login with hidden input
# and adds the server to the MCP clients it finds. Run it again to update.
#
# Optional settings: ITU_MCP_REF (branch, tag or commit; default main) and
# ITU_MCP_SKIP_SETUP=1 (install only).
set -eu

REPO="tzi4/itu-mcp-paperbridge"
REF="${ITU_MCP_REF:-main}"
PYTHON_REQUEST=">=3.11,<3.15"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$REF" in
  *[!A-Za-z0-9._/-]* | "") fail "ITU_MCP_REF may contain only letters, digits, '.', '_', '/' and '-'." ;;
esac
command -v curl >/dev/null 2>&1 || fail "curl is required."

find_uv() {
  for candidate in "$(command -v uv 2>/dev/null || true)" "${XDG_BIN_HOME:-}/uv" \
      "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
  say "Installing uv, the Python package manager used to install ITU MCP..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$(find_uv || true)"
  [ -n "$UV" ] || fail "uv was installed but could not be found; open a new terminal and run this again."
fi

# Install the exact commit so the update check can compare it with GitHub.
if printf '%s' "$REF" | grep -Eq '^[0-9a-f]{40}$'; then
  COMMIT="$REF"
else
  COMMIT="$(curl -fsSL --max-time 20 -H 'Accept: application/vnd.github.sha' \
    "https://api.github.com/repos/$REPO/commits/$REF" 2>/dev/null || true)"
fi
if printf '%s' "$COMMIT" | grep -Eq '^[0-9a-f]{40}$'; then
  SOURCE="https://github.com/$REPO/archive/$COMMIT.tar.gz"
  say "Installing ITU MCP + PaperBridge from $REPO at commit $(printf '%s' "$COMMIT" | cut -c1-7)..."
else
  SOURCE="https://github.com/$REPO/archive/$REF.tar.gz"
  say "Could not look up the commit of '$REF'; installing it without update notices."
fi

"$UV" tool install --python "$PYTHON_REQUEST" --reinstall-package itu-mcp "itu-mcp @ $SOURCE"

BIN_DIR="$("$UV" tool dir --bin)"
ITU_MCP="$BIN_DIR/itu-mcp"
[ -x "$ITU_MCP" ] || fail "The itu-mcp command was not found in $BIN_DIR."
"$ITU_MCP" --version

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Note: $BIN_DIR is not on your PATH. Run \`$UV tool update-shell\` to add it." ;;
esac

if [ "${ITU_MCP_SKIP_SETUP:-0}" = "1" ]; then
  say "Installed. Run \`$ITU_MCP --setup\` to save your login and configure your MCP clients."
elif [ -f "$HOME/.itu-mcp/.env" ] && grep -q '^NINOVA_PASSWORD[=].' "$HOME/.itu-mcp/.env"; then
  # Running the same command again is the update; keep the saved login and client settings.
  say "Updated. Your saved login and client settings are unchanged; restart your MCP client."
elif [ -t 0 ]; then
  "$ITU_MCP" --setup
elif (exec </dev/tty) 2>/dev/null; then
  # Piped into sh: read the answers from the terminal instead of the pipe.
  "$ITU_MCP" --setup </dev/tty
else
  "$ITU_MCP" --setup </dev/null
fi
