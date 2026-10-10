# Install ITU MCP + PaperBridge

Install **tzi4/itu-mcp** with Python 3.11 or newer. PaperBridge's Python package
and static viewer pages are included. No second repository or MCP connection
is required.

## Linux and macOS

```bash
git clone https://github.com/tzi4/itu-mcp.git
cd itu-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
itu-mcp --version
itu-mcp --list-tools
```

## Windows PowerShell

```powershell
git clone https://github.com/tzi4/itu-mcp.git
cd itu-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\itu-mcp.exe --version
.\.venv\Scripts\itu-mcp.exe --list-tools
```

Activation is optional. Linux and macOS use `.venv/bin/python` and
`.venv/bin/itu-mcp`.

## Desktop client configuration

For a client using the standard `mcpServers` JSON configuration:

```json
{
  "mcpServers": {
    "itu-mcp-paperbridge": {
      "command": "/absolute/path/itu-mcp/.venv/bin/itu-mcp",
      "env": {
        "NINOVA_USERNAME": "YOUR_ITU_USERNAME",
        "NINOVA_PASSWORD": "YOUR_ITU_PASSWORD"
      }
    }
  }
}
```

On Windows, replace the command with your own executable path using escaped
backslashes, e.g. `C:\\Projects\\itu-mcp\\.venv\\Scripts\\itu-mcp.exe`.
Enter credentials privately in your client configuration. Do not commit or share
that configuration. Omit credentials for public tools and research metadata.

For Codex, the equivalent server entry is:

```toml
[mcp_servers.itu-mcp-paperbridge]
command = "/absolute/path/itu-mcp/.venv/bin/itu-mcp"
```

Configure credentials using your client's private environment settings.
Do not use `uvx itu-mcp`, `pip install itu-mcp`, or upstream `.mcpb` files to
install this fork: those resolve to a different distribution. Fork-specific
bundles will be available only when this repository publishes them.

## Optional browser retrieval

Research metadata tools are available immediately; publisher retrieval is off
by default. `pip install .` does not download browser binaries. To provision
managed Chromium from the activated virtual environment:

```bash
python -m playwright install chromium
```

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
| Saved OBS registration drafts | Optional `NINOVA_OBS_REGISTRATION_WRITES=1`; each save still requires explicit confirmation |
| Phone access while your computer is off | Your own running server, remote HTTP setup and a compatible AI client |
| Morning briefings | Scheduling in your AI app or a separate automation setup |

For remote mail, explicitly enable `NINOVA_REMOTE_ENABLE_MAIL=1`. Remote file
tools and submissions are separate opt-ins: `NINOVA_REMOTE_ENABLE_FILES=1` and
`NINOVA_REMOTE_ENABLE_WRITES=1`. These adapters require API-key authentication
and an absolute `NINOVA_REMOTE_FILE_ROOT` pointing to your managed data directory.
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

## Update

```bash
git pull --ff-only origin main
python -m pip install .
```

Restart or reconnect the MCP client after updating. For development, use
`python -m pip install -e .` and branch from this fork's `main`.
Each owner uses a private state directory and PaperBridge archive. Updates do
not copy another user's credentials, documents or browser sessions.

For HTTP deployment, see [advanced configuration](advanced.md) and
[security](security.md). The maintainer's service files and tunnel settings are
private operator configuration, separate from this source distribution.
