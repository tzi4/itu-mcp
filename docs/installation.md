# Install ITU MCP + PaperBridge

Install **tzi4/itu-mcp-paperbridge** from this repository. PaperBridge's Python
package and static viewer pages are included. No second repository or MCP
connection is required.

Do not use `uvx itu-mcp`, `pip install itu-mcp`, or upstream `.mcpb` files to
install this fork: those resolve to a different distribution. Fork-specific
bundles will be available only when this repository publishes them.

## On your computer

**macOS and Linux:**

```bash
curl -LsSf https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install.sh | sh
```

**Windows PowerShell:**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install.ps1 | iex"
```

You can read both scripts first: [install.sh](../scripts/install.sh) and
[install.ps1](../scripts/install.ps1). They do the same thing:

1. Install [uv](https://docs.astral.sh/uv/) with its official installer if it is
   not on your computer yet.
2. Look up the current commit of `main` and install exactly that commit as a uv
   tool, so `itu-mcp` ends up in uv's tool folder (usually `~/.local/bin`). uv uses
   Python 3.11 to 3.14 from your computer or downloads one. Git is not needed.
3. Run `itu-mcp --setup`.

`itu-mcp --setup` asks for your ITU username and password; the password is read
with hidden input. It writes them to `~/.itu-mcp/.env` (on Windows
`%USERPROFILE%\.itu-mcp\.env`), a file only your user account can read. The server
reads this file wherever your MCP client starts it, so client configs only contain
the path to `itu-mcp`, never your credentials. Leave the username empty to use only
the public tools; you can run `itu-mcp --setup` again at any time.

Then it offers to add the server to the clients it finds:

| Client | What setup does |
| --- | --- |
| Claude Desktop | Adds `itu-mcp-paperbridge` to `claude_desktop_config.json`, keeping your other servers and a `.bak` copy |
| Cursor | The same in `~/.cursor/mcp.json` |
| Codex | Appends an `[mcp_servers.itu-mcp-paperbridge]` table to `~/.codex/config.toml` |
| Claude Code | Prints `claude mcp add --scope user itu-mcp-paperbridge /path/to/itu-mcp` for you to run |
| Others | Prints an `mcpServers` JSON snippet |

Restart the client afterwards and ask it to run `auth_status`, or check the login
from a terminal with `itu-mcp --check-auth`.

Settings: `ITU_MCP_REF` installs a branch, tag or commit other than `main`, and
`ITU_MCP_SKIP_SETUP=1` skips the setup step.

### Let your AI assistant install it

If you use Claude Code, Codex or another assistant that can run commands, you can
paste this prompt (the idea comes from [Ninova MCP](https://github.com/hikmedit/ninova-mcp)):

> Install the ITU MCP + PaperBridge MCP server from https://github.com/tzi4/itu-mcp-paperbridge
> (not the `itu-mcp` package on PyPI). Run the install command for my operating system
> from docs/installation.md with `ITU_MCP_SKIP_SETUP=1`, then register `itu-mcp` as an
> MCP server named `itu-mcp-paperbridge` in the client I use, merging into any existing
> configuration. Never ask me for my ITU password and never put credentials in a
> config file or command. When you are done, tell me to run `itu-mcp --setup` myself
> in a terminal to enter my login, and to restart the client.

## On a Linux server

For an always-on server with systemd, such as a Raspberry Pi with a 64-bit OS:

```bash
curl -fsSLO https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install-server.sh
sudo sh install-server.sh
```

[install-server.sh](../scripts/install-server.sh) is safe to run again; that is
also how you upgrade. It:

- installs uv into `/opt/itu-mcp-paperbridge/bin` if uv is missing, creates a
  virtual environment in `/opt/itu-mcp-paperbridge/venv` and installs the current
  commit of `main` into it;
- creates the system user `itu-mcp` and the private data directory
  `/var/lib/itu-mcp-paperbridge`;
- creates `/etc/itu-mcp-paperbridge/env` once, owned by root with mode 600, with
  a random MCP path and API key, and asks for your ITU login with hidden input.
  Later runs keep the file as it is;
- writes and enables `itu-mcp-paperbridge.service`, which runs `itu-mcp-remote`
  on `127.0.0.1:8000` and starts on boot.

Environment variables change the defaults: `ITU_MCP_REF`, `ITU_MCP_SERVICE`,
`ITU_MCP_USER`, `ITU_MCP_PREFIX`, `ITU_MCP_STATE_DIR`, `ITU_MCP_CONFIG_DIR` and
`ITU_MCP_PORT` (for a new env file). Pass them through sudo, for example
`sudo ITU_MCP_PORT=8100 sh install-server.sh`.

The service listens on the loopback interface only. To use it from ChatGPT or
Claude.ai, you need your own HTTPS endpoint in front of it, for example a tunnel
service or a reverse proxy with a certificate, and then:

1. Set `NINOVA_PUBLIC_BASE_URL` and `NINOVA_ALLOWED_HOSTS` in the env file
   (`sudo nano /etc/itu-mcp-paperbridge/env`) and restart the service with
   `sudo systemctl restart itu-mcp-paperbridge`.
2. Your connector URL is your HTTPS address followed by `NINOVA_REMOTE_MCP_PATH`.
   MCP requests must carry `NINOVA_REMOTE_API_KEY` as `Authorization: Bearer` or
   `X-API-Key`. Show both privately with
   `sudo grep -E '^NINOVA_REMOTE_(MCP_PATH|API_KEY)=' /etc/itu-mcp-paperbridge/env`.

Remote file tools and write actions stay off until you also set
`NINOVA_REMOTE_FILE_ROOT`. The [advanced settings](advanced.md) and the
[security guide](security.md) explain every remote option. Check the service with
`systemctl status itu-mcp-paperbridge` and `journalctl -u itu-mcp-paperbridge`.

To remove it: `sudo systemctl disable --now itu-mcp-paperbridge`, then delete
`/etc/systemd/system/itu-mcp-paperbridge.service`, `/opt/itu-mcp-paperbridge`,
`/etc/itu-mcp-paperbridge` and, if you no longer need the data,
`/var/lib/itu-mcp-paperbridge`.

## Updates

Updating is one command, and the update notice names the right one for your install:

| Install | Command |
| --- | --- |
| macOS or Linux computer | `itu-mcp --update` |
| Windows computer | `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install.ps1 \| iex"` |
| Linux server | `curl -fsSL https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install-server.sh \| sudo sh` |

Each installs the latest commit of `main` and keeps your saved login and client
settings; running the original install command again does the same. Restart or
reconnect your MCP client afterwards; the server script restarts the service
itself. On Windows, `itu-mcp --update` prints the PowerShell command, because a
running program cannot replace its own `.exe` there.

The server tells you when an update exists. At most once a day, on a background
thread that never delays a tool call, it asks the GitHub API how many commits
`main` is ahead of the commit you installed. The request carries only that commit
ID. When an update exists, `auth_status` returns an `update_notice`, the next
session's server instructions ask your assistant to mention it once, and a remote
server logs it. Check right away with `itu-mcp --check-update`, see the installed
commit with `itu-mcp --version`, and set `ITU_MCP_UPDATE_CHECK=0` to turn the check
off. The result is cached in `update-check.json` in the state directory.

The fork does not publish versioned releases yet, so the commit you installed
identifies your build. The install scripts record it; installs from a local clone
(`pip install .`) do not, so they get no update notices.

## Manual install from source

With Python 3.11 or newer you can also install from a clone.

### Linux and macOS

```bash
git clone https://github.com/tzi4/itu-mcp-paperbridge.git
cd itu-mcp-paperbridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
itu-mcp --version
itu-mcp --list-tools
```

### Windows PowerShell

```powershell
git clone https://github.com/tzi4/itu-mcp-paperbridge.git
cd itu-mcp-paperbridge
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\itu-mcp.exe --version
.\.venv\Scripts\itu-mcp.exe --list-tools
```

Activation is optional. Linux and macOS use `.venv/bin/python` and
`.venv/bin/itu-mcp`. Run `itu-mcp --setup` to save your login and configure your
clients, or configure them by hand as below. Update with
`git pull --ff-only origin main` and `python -m pip install .`.

### Client configuration by hand

For a client using the standard `mcpServers` JSON configuration:

```json
{
  "mcpServers": {
    "itu-mcp-paperbridge": {
      "command": "/absolute/path/itu-mcp/.venv/bin/itu-mcp"
    }
  }
}
```

On Windows, replace the command with your own executable path using escaped
backslashes, e.g. `C:\\Projects\\itu-mcp\\.venv\\Scripts\\itu-mcp.exe`.

For Codex, the equivalent server entry is:

```toml
[mcp_servers.itu-mcp-paperbridge]
command = "/absolute/path/itu-mcp/.venv/bin/itu-mcp"
```

The server finds your credentials in `~/.itu-mcp/.env`. You can instead put
`NINOVA_USERNAME` and `NINOVA_PASSWORD` in the client's private `env` settings;
do not commit or share that configuration. Omit credentials for public tools and
research metadata.

## Optional browser retrieval

Research metadata tools are available immediately; publisher retrieval is off
by default. Installing does not download browser binaries. To provision
managed Chromium for any install method:

```bash
itu-mcp --paper-browser install
```

From an activated source virtual environment, `python -m playwright install chromium`
does the same.

Set `PAPERBRIDGE_BROWSER_ENGINE=chromium` in the MCP server's environment and
record your actual document/provider permissions as described in
[PaperBridge setup](paperbridge.md). Linux may also need browser system
dependencies through your normal administrator workflow. A headed server
browser additionally needs a display.

## Use the features I use on my Pi

This repository contains the same ITU MCP and PaperBridge application sources
I use on my Raspberry Pi. Each installation has its own accounts, settings,
browser and document archive. A source install makes the tools available;
configure the features you want to use:

| Feature | Your setup |
| --- | --- |
| Ninova, OBS and Portal | Your own `NINOVA_USERNAME` and `NINOVA_PASSWORD` |
| ITU Mail | Your own university mail access; optional separate mail credentials are described in [advanced configuration](advanced.md) |
| IEEE / Springer full text | A provisioned browser, `PAPERBRIDGE_BROWSER_ENGINE=chromium`, your own institutional access and recorded processing permissions; see [PaperBridge setup](paperbridge.md) |
| PDF page images | A client that can receive images; preview and automatic attachment support depend on the client |
| Saved OBS registration drafts | On by default (`NINOVA_OBS_REGISTRATION_WRITES=0` turns them off); each save still requires explicit confirmation |
| Phone access while your computer is off | Your own running server, remote HTTP setup and a compatible AI client |
| Morning briefings | Scheduling in your AI app or a separate automation setup |

Remote mail tools are on by default; `NINOVA_REMOTE_ENABLE_MAIL=0` turns them off.
Remote file tools and write actions (assignment submissions)
are on by default once API-key authentication and an absolute
`NINOVA_REMOTE_FILE_ROOT` pointing to your managed data directory are configured;
without them they stay off so a basic setup still starts. Set
`NINOVA_REMOTE_ENABLE_FILES=0` or `NINOVA_REMOTE_ENABLE_WRITES=0` to turn them off.
If you set `NINOVA_REMOTE_ALLOWED_TOOLS`, include the tools you want to expose.
Uploads must also be allowed by `NINOVA_ALLOW_UPLOADS`.

Remote assignment files must be deliberately placed in the managed `uploads`
folder through SSH/SFTP. Review the exact file and submission preview, then
confirm the upload; downloaded files are not automatically turned into
submissions. See [remote configuration](advanced.md) and [security](security.md).

For PaperBridge, check `papers_status` after configuring your client. It reports
which providers are configured, without testing login or subscription access.
My Pi uses Chromium for IEEE and Springer, a private display and a configured
network route. You can use the documented headless default; if you choose headed
mode, provide your own display. Use a network route that works for your access.
Python, dependencies, browser versions and client behavior can differ between
machines, so matching application sources does not guarantee identical results.

## Development

For development, use `python -m pip install -e .` and branch from this fork's `main`.
Each owner uses a private state directory and PaperBridge archive. Updates do
not copy another user's credentials, documents or browser sessions.

For other HTTP deployments, see [advanced configuration](advanced.md) and
[security](security.md). The maintainer's service files and tunnel settings are
private operator configuration, separate from this source distribution.
