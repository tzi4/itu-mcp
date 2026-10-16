#!/bin/sh
# Install or upgrade ITU MCP + PaperBridge as a systemd service on a Linux server,
# for example a Raspberry Pi. Run it as root; run it again to upgrade.
#
#   curl -fsSLO https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install-server.sh
#   sudo sh install-server.sh
#
# What it does:
#   - installs uv into the install prefix if no uv is available, and uses it to
#     create a virtual environment (with a suitable Python) and install this fork
#     from GitHub at the current commit of ITU_MCP_REF;
#   - creates a system user and a private state directory;
#   - creates a private environment file (mode 600, read by systemd) with fresh
#     secrets for the MCP path and API key, once; later runs keep it;
#   - asks for your ITU login with hidden input if the file has none yet;
#   - writes a systemd unit for `itu-mcp-remote`, bound to 127.0.0.1, and enables
#     it so it starts on boot.
#
# It never opens the server to the network. For ChatGPT or Claude.ai, put your own
# HTTPS endpoint (a tunnel or reverse proxy) in front of 127.0.0.1:PORT.
#
# Optional settings (environment variables):
#   ITU_MCP_REF         branch, tag or commit to install (default: main)
#   ITU_MCP_SERVICE     systemd service name (default: itu-mcp-paperbridge)
#   ITU_MCP_USER        system user that runs the service (default: itu-mcp)
#   ITU_MCP_PREFIX      program files (default: /opt/itu-mcp-paperbridge)
#   ITU_MCP_STATE_DIR   data directory (default: /var/lib/itu-mcp-paperbridge)
#   ITU_MCP_CONFIG_DIR  directory of the env file (default: /etc/itu-mcp-paperbridge)
#   ITU_MCP_PORT        loopback port for a new env file (default: 8000)
set -eu

REPO="tzi4/itu-mcp-paperbridge"
REF="${ITU_MCP_REF:-main}"
SERVICE="${ITU_MCP_SERVICE:-itu-mcp-paperbridge}"
SERVICE_USER="${ITU_MCP_USER:-itu-mcp}"
PREFIX="${ITU_MCP_PREFIX:-/opt/itu-mcp-paperbridge}"
STATE_DIR="${ITU_MCP_STATE_DIR:-/var/lib/itu-mcp-paperbridge}"
CONFIG_DIR="${ITU_MCP_CONFIG_DIR:-/etc/itu-mcp-paperbridge}"
PORT="${ITU_MCP_PORT:-8000}"
ENV_FILE="$CONFIG_DIR/env"
UNIT_FILE="/etc/systemd/system/$SERVICE.service"
VENV="$PREFIX/venv"
PYTHON_REQUEST=">=3.11,<3.15"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

check_name() {
  case "$2" in
    "" | -* | *[!A-Za-z0-9._-]*) fail "$1 must contain only letters, digits, '.', '_' and '-'." ;;
  esac
}
check_path() {
  case "$2" in
    /*) ;;
    *) fail "$1 must be an absolute path." ;;
  esac
  case "$2" in
    *[!A-Za-z0-9._/-]*) fail "$1 must not contain spaces or special characters." ;;
  esac
  [ "$2" != "/" ] || fail "$1 must not be /."
}

check_name ITU_MCP_SERVICE "$SERVICE"
check_name ITU_MCP_USER "$SERVICE_USER"
check_path ITU_MCP_PREFIX "$PREFIX"
check_path ITU_MCP_STATE_DIR "$STATE_DIR"
check_path ITU_MCP_CONFIG_DIR "$CONFIG_DIR"
case "$REF" in
  "" | -* | *[!A-Za-z0-9._/-]*) fail "ITU_MCP_REF may contain only letters, digits, '.', '_', '/' and '-'." ;;
esac
case "$PORT" in
  "" | *[!0-9]*) fail "ITU_MCP_PORT must be a number." ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || fail "ITU_MCP_PORT must be between 1 and 65535."

[ "$(uname -s)" = "Linux" ] || fail "This installer is for Linux servers with systemd."
[ "$(id -u)" -eq 0 ] || fail "Run it as root, for example: sudo sh install-server.sh"
command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
  || fail "systemd is not running on this machine."
command -v curl >/dev/null 2>&1 || fail "curl is required."

umask 022
mkdir -p "$PREFIX"
# Keep uv's Python and cache inside the prefix so the service user can read them
# and removing the prefix removes everything.
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_CACHE_DIR="$PREFIX/cache"

if [ -x "$PREFIX/bin/uv" ]; then
  UV="$PREFIX/bin/uv"
elif command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
else
  say "Installing uv into $PREFIX/bin..."
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR="$PREFIX/bin" UV_NO_MODIFY_PATH=1 sh
  UV="$PREFIX/bin/uv"
  [ -x "$UV" ] || fail "uv could not be installed."
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
  say "Installing ITU MCP + PaperBridge at commit $(printf '%s' "$COMMIT" | cut -c1-7)..."
else
  SOURCE="https://github.com/$REPO/archive/$REF.tar.gz"
  say "Could not look up the commit of '$REF'; installing it without update notices."
fi

# A missing or broken interpreter (for example after a distribution upgrade)
# means the environment is rebuilt; otherwise it is reused.
if [ ! -x "$VENV/bin/python" ]; then
  "$UV" venv --clear --python "$PYTHON_REQUEST" "$VENV"
fi
"$UV" pip install --python "$VENV/bin/python" --reinstall-package itu-mcp "itu-mcp @ $SOURCE"
"$VENV/bin/itu-mcp" --version

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  NOLOGIN="$(command -v nologin 2>/dev/null || echo /usr/sbin/nologin)"
  useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home \
    --shell "$NOLOGIN" "$SERVICE_USER"
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"
install -d -m 700 -o root -g root "$CONFIG_DIR"

# systemd reads the env file as root before starting the service, so the file
# stays root-only (600) and the service user never needs to read it.
if [ -t 0 ]; then
  "$VENV/bin/python" -m ninova_mcp.installer server-env --file "$ENV_FILE" \
    --state-dir "$STATE_DIR" --port "$PORT" --ask-credentials
elif (exec </dev/tty) 2>/dev/null; then
  "$VENV/bin/python" -m ninova_mcp.installer server-env --file "$ENV_FILE" \
    --state-dir "$STATE_DIR" --port "$PORT" --ask-credentials </dev/tty
else
  "$VENV/bin/python" -m ninova_mcp.installer server-env --file "$ENV_FILE" \
    --state-dir "$STATE_DIR" --port "$PORT"
fi
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"

UNIT_TMP="$(mktemp "$UNIT_FILE.XXXXXX")"
trap 'rm -f "$UNIT_TMP"' EXIT
"$VENV/bin/python" -m ninova_mcp.installer systemd-unit \
  --executable "$VENV/bin/itu-mcp-remote" --env-file "$ENV_FILE" \
  --state-dir "$STATE_DIR" --user "$SERVICE_USER" >"$UNIT_TMP"
chmod 644 "$UNIT_TMP"
mv -f "$UNIT_TMP" "$UNIT_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

LISTEN_PORT="$(sed -n 's/^NINOVA_REMOTE_PORT=//p' "$ENV_FILE" | tail -n 1)"
LISTEN_PORT="${LISTEN_PORT:-8000}"
HEALTHY=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS --max-time 2 "http://127.0.0.1:$LISTEN_PORT/healthz" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 2
done

say ""
if [ "$HEALTHY" = 1 ]; then
  say "ITU MCP + PaperBridge is running as $SERVICE on 127.0.0.1:$LISTEN_PORT and starts on boot."
else
  say "The service did not answer yet. Check it with: journalctl -u $SERVICE -n 50"
fi
say ""
say "Next steps:"
say "  1. Review the settings:   sudo nano $ENV_FILE"
say "     then apply them:       sudo systemctl restart $SERVICE"
say "  2. Put your own HTTPS endpoint (a tunnel or reverse proxy) in front of"
say "     http://127.0.0.1:$LISTEN_PORT and set NINOVA_PUBLIC_BASE_URL and NINOVA_ALLOWED_HOSTS."
say "  3. Your connector URL is that HTTPS address followed by NINOVA_REMOTE_MCP_PATH, and"
say "     MCP requests must carry NINOVA_REMOTE_API_KEY. Show both privately with:"
say "     sudo grep -E '^NINOVA_REMOTE_(MCP_PATH|API_KEY)=' $ENV_FILE"
say "  See https://github.com/$REPO/blob/main/docs/installation.md#on-a-linux-server for details."
say "  Upgrade later by running this script again."
