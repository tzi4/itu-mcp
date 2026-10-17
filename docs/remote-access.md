# Using it from your phone and the web

The install command on your computer is enough for AI apps that start the server
themselves (see [Good to know](../README.md#good-to-know-chatgpt-and-claude)). For
ChatGPT on the web or your phone, Claude on claude.ai or your phone, or Claude
Cowork, the AI service has to reach the server from the internet. This guide sets
that up in four steps. It is how I use the server from my Raspberry Pi.

**Privacy:** whoever can reach the server can use your ITU account through it. Keep
the MCP path and API key secret, and serve only yourself (see
[security](security.md)).

## 1. Install the server on an always-on machine

Any Linux machine with systemd that stays on works, such as a Raspberry Pi with a
64-bit OS or a small cloud server:

```bash
curl -fsSL https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install-server.sh | sudo sh
```

It asks for your ITU login once, with hidden input, and creates
`/etc/itu-mcp-paperbridge/env` with a random MCP path and API key. The service
listens only on `127.0.0.1:8000`. Details:
[installation guide](installation.md#on-a-linux-server).

You can skip this step and use your own computer instead (only with the tunnel in
step 2a). The assistant can then reach the server only while that computer is on.

## 2. Make it reachable

Choose one of two ways.

### 2a. For ChatGPT: OpenAI Secure MCP Tunnel (what I use)

[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
connects ChatGPT to a private server without a public address or open port: a
small program, `tunnel-client`, runs next to the server and only makes outgoing
connections to OpenAI.

1. In the OpenAI Platform tunnel settings
   (`platform.openai.com/settings/organization/tunnels`), create a tunnel and link it
   to your ChatGPT workspace. Your role needs the Tunnels permissions listed in the
   guide.
2. Install `tunnel-client` from
   [openai/tunnel-client](https://github.com/openai/tunnel-client/releases/latest)
   and point it at the server: `http://127.0.0.1:8000` followed by your
   `NINOVA_REMOTE_MCP_PATH`, with your `NINOVA_REMOTE_API_KEY` as
   `Authorization: Bearer`. On your own computer you can point it at the local
   `itu-mcp` command instead (the guide's stdio sample). Then you don't need the
   server's path and key.
3. Run it as a service so it starts on boot, and check it with
   `tunnel-client doctor`.

Show your path and key privately with
`sudo grep -E '^NINOVA_REMOTE_(MCP_PATH|API_KEY)=' /etc/itu-mcp-paperbridge/env`.
Never paste them into a chat.

### 2b. For Claude (or both): your own HTTPS address

Claude connects to custom connectors from Anthropic's cloud, so it needs a public
HTTPS address. Put a reverse proxy with a certificate, or a tunnel service, in
front of `127.0.0.1:8000`. Then add the address to
`/etc/itu-mcp-paperbridge/env`:

```bash
NINOVA_PUBLIC_BASE_URL=https://itu.example.com
NINOVA_ALLOWED_HOSTS=itu.example.com
NINOVA_ALLOWED_ORIGINS=https://claude.ai,https://claude.com
```

and restart: `sudo systemctl restart itu-mcp-paperbridge`. Your connector URL is
`https://itu.example.com` followed by `NINOVA_REMOTE_MCP_PATH`. Leave the API key
on.

## 3. Add the connector

**ChatGPT:** open [chatgpt.com/plugins](https://chatgpt.com/plugins), select **+**,
then **Add custom MCP server**. Under **Connection**, choose **Tunnel** and select
your tunnel. Confirm the warning and create it. Menu names have changed several
times; the Secure MCP Tunnel guide has the current ones. ChatGPT's own custom
connectors sign in with OAuth or not at all, and this server has no OAuth yet. That
is why I use the tunnel rather than a public address with ChatGPT.

**Claude:** open **Customize → Connectors**, then **Add custom connector**. Enter
your connector URL, choose **No sign in**, and under **Request headers** add
`Authorization` with the value `Bearer <your API key>`. The connector belongs to your
Claude account, so it appears on claude.ai, in Claude Desktop, Cowork and the
phone apps. Free plans allow one custom connector.

## 4. Check it

In a new chat, ask: "Run itu_health." It lists each ITU service with ok or the
reason it failed. After an update, ask ChatGPT to **Refresh** the connector in its
settings so it sees new tools.

Write actions and file tools on a remote server have extra switches; see
[advanced settings](advanced.md#remote-http-server-chatgpt--claudeai-custom-connectors)
and [security](security.md).
