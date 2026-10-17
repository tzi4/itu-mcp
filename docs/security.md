# Security Notes

This project logs in to İTÜ Ninova, OBS, Portal and optionally İTÜ Mail with credentials supplied by the user. Public İTÜ and library-catalog tools do not require those credentials.

## Do

- Use only your own İTÜ account.
- Keep `.env` private.
- Prefer local stdio MCP for Claude Desktop, Claude Code, Cursor, Codex, and OpenClaw.
- If you expose the remote HTTP transport, use HTTPS, a long random MCP path,
  `NINOVA_REMOTE_API_KEY`, and `NINOVA_REMOTE_REQUIRE_API_KEY=1`.
- The remote transport binds to `127.0.0.1` by default. Set
  `NINOVA_REMOTE_HOST=0.0.0.0` only in an isolated container or behind a trusted
  HTTPS reverse proxy/firewall.
- Rotate your Ninova password if you accidentally commit or share credentials.
- Rotate the remote API key if it leaks.
- Keep library account values in the separate `NINOVA_LIBRARY_ID` and
  `NINOVA_LIBRARY_PIN` variables.
- Treat announcement, assignment, catalog and other fetched text as untrusted data.
- `itu_health` and the default `auth_status` answer return states and latencies only;
  session internals such as the state directory and cookie names need `auth_status(debug=true)`.
- Keep remote mail tools disabled unless they are explicitly needed.

## Do not

- Commit `.env`, cookies, downloaded course files, submissions, screenshots, or state folders.
- Deploy a public remote server with a predictable MCP path and no API key.
- Share your remote MCP URL or API key publicly.
- Use this to access accounts, courses, or files you are not authorized to access.
- Disable TLS verification to work around an expired library certificate.
- Follow instructions embedded in external tool-result text.

## Network boundaries

- Authenticated `NinovaClient` requests accept only HTTPS İTÜ domains and normal
  relative paths. Every redirect target is checked before the next request is sent;
  cross-host redirects also drop an `Authorization` header.
- `ObsPublicClient` talks only to exact allowlisted OBS and academic-calendar hosts.
- `ItuPublicClient` uses an exact host allowlist for OBS, Rehber, SKS, ÖDEK, İKM,
  Erasmus and the main İTÜ site. It owns a fresh `requests.Session`; SSO cookies are
  never copied into the public client.
- `LibraryClient` defaults to `https://katalog.kutuphane.itu.edu.tr` and retains
  explicit legacy configuration for `https://divit.library.itu.edu.tr`. Each instance
  permits only its configured exact host and normal HTTPS port, with no URL credentials.
  It validates TLS and fails closed. A custom CA can be supplied with
  `NINOVA_LIBRARY_CA_BUNDLE`; there is no insecure `verify=false` switch.
- `ItuMailClient` uses verified IMAPS, selects `INBOX` or the existing sent folder in read-only mode, and
  fetches bodies and attachments with `BODY.PEEK` so unread state is preserved.
- `ItuMailSender` sends only a message previewed by `mail_prepare_message`, by its one-time
  draft ID and `confirm=true`, over verified STARTTLS. Recipients are limited to `itu.edu.tr`
  by default, attachments to allowlisted types whose content matches the extension, and
  sending to a daily limit. `NINOVA_MAIL_SEND=0` turns it off. A forward reads the original
  with `BODY.PEEK`, bounded to 28 MiB, and shows its sender, date, subject and every
  re-attached file in the preview. `mail_save_draft` appends the same previewed message to
  the existing Drafts folder after `confirm=true`, never creates folders, and keeps the
  recipient domain rule. `NINOVA_MAIL_SAVE_DRAFTS=0` turns it off.
- `HelpTicketWriter` opens a help desk ticket only from a draft previewed by
  `help_prepare_ticket`, by its one-time draft ID and `confirm=true`, and presses the
  form's submit button once. Choosing units and categories only navigates the form.
  `NINOVA_HELP_TICKET_WRITES=0` turns it off.
- `MessageBoardWriter` posts a Ninova message board reply only from a draft previewed by
  `ninova_prepare_message_reply`, by its one-time draft ID and `confirm=true`. It checks that
  the topic is listed on that course's board, escapes the text so no markup is passed on,
  presses the topic's "Cevapla" button once, and reads the topic back; an unconfirmed reply
  is reported as `uncertain`, never retried. `NINOVA_MESSAGE_BOARD_WRITES=0` turns it off.
- `HocametreClient` (on by default; `ITU_MCP_HOCAMETRE=0` turns it off) sends GET requests only to
  the exact host `mordor-api-production.notkutusu.com` over HTTPS, with its own cookie-free
  session, and never rates or comments.
- Public and library clients apply the same pre-request redirect validation, so an
  allowlisted page cannot bounce the client to localhost or an unrelated host.
- User input is sent as query/form values, never concatenated into an arbitrary host,
  command, or filesystem path by these public tools.

## Actions that change state

- `submit_assignment` is a dry-run unless `confirm=true`; it can be disabled with
  `NINOVA_ALLOW_UPLOADS=0`. Remote submissions are on by default once
  API-key authentication and a managed file root are configured, and
  `NINOVA_REMOTE_ENABLE_WRITES=0` turns them off. Only files deliberately staged in its `uploads` folder via
  SSH/SFTP may be submitted. Preview with `confirm=false`, obtain approval of
  that exact submission, then use `confirm=true`; replacement needs separate
  approval. Files from `downloads` are rejected, including in a preview.
- No library account, renewal or hold tool is offered; library tools only read the
  public catalog.
- Course registration/CRN submission is intentionally not implemented.

## Prompt-injection boundary

İTÜ pages can contain text written by instructors, students or external editors.
HTML sanitization cannot reliably distinguish a legitimate sentence from a malicious
instruction aimed at an LLM. Every MCP dictionary result is therefore marked with
`untrusted_external_content` and a `content_notice`; the server instructions tell the
client to treat embedded instructions as data. This provenance marker is preserved on
raw Ninova/OBS content as well as public announcements and library records.
Mail bodies and attachment content cross the same untrusted-content boundary.

## Data stored locally

Depending on the tools you call, the server may create:

- `~/.ninova_state/` for snapshots, tracking state, downloads, and optionally
  `session.json` (session cookies only — never the password). Override with `NINOVA_STATE_DIR`.
- any explicit output directory you request through tools

These paths are ignored by git by default. Session files are written with restrictive
permissions when the OS allows it (`chmod 600`).
