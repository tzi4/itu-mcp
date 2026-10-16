# PaperBridge setup

PaperBridge is bundled with this fork. It runs in the same process as ITU MCP;
there is no second server connection and no LLM API call. Every installation
uses its owner's university access and local document archive.

## What is enabled by default?

| Capability | Fresh installation |
| --- | --- |
| Research tools and DOI resolution | Available in the MCP tool registry |
| Crossref metadata discovery | Available when requested; search is not a download |
| Unpaywall discovery | Requires your own contact email |
| Local archived text and PDF pages | Available for documents with recorded external-model permission |
| IEEE / Springer browser retrieval | Disabled until a browser engine is configured |
| Publisher login | Requires configured retrieval, your own credentials and permitted document processing |
| Automatic page attachment | Requested per call; depends on the client's MCP Apps support |
| Synthetic visual diagnostic | Disabled unless `PAPERBRIDGE_VISUAL_PROBE=1` |

Installing the Python package does not download a browser, sign in to a
publisher, grant document permissions or copy the maintainer's archive.
`papers_status` describes configuration without making network requests or
testing subscription entitlement.

## Private storage

The ITU MCP adapter defaults to a `papers` directory within `NINOVA_STATE_DIR`
(normally `~/.ninova_state`). To give the standalone CLI and the integrated
tools the same archive, configure an explicit absolute `PAPERBRIDGE_DATA_DIR`
in both environments. The standalone CLI otherwise defaults to
`~/.local/share/paperbridge`.

Protect that directory using your OS account and file permissions. It contains
original documents and extracted text in a private SQLite archive. Keep it out
of Git, public shares and backups intended for distribution. This is a
single-user application, not a shared multi-user service.

## Optional institutional retrieval

1. Install a matching browser from the application's virtual environment:

   ```bash
   python -m playwright install chromium
   ```

2. Set `PAPERBRIDGE_BROWSER_ENGINE=chromium` in the MCP process's environment.
   Configure your own `NINOVA_USERNAME` and `NINOVA_PASSWORD` privately. Linux
   hosts may require additional browser system dependencies. Headless mode is
   the default. `PAPERBRIDGE_BROWSER_HEADLESS=0` requires an operator-provided
   display on the server.
3. Record actual processing and external-model permission for each relevant
   document, or an applicable provider policy. This records existing rights;
   it does not create a license or establish subscription entitlement.
4. Restart or reconnect your MCP process. Check `papers_status`, then request
   an applicable paper through `papers_get_text`. Check the returned status;
   successful login alone is not proof of a verified PDF retrieval.

Document records belong in `publisher-rights.json` within the private archive,
keyed by a normalized DOI. For example, adapt this record only when you actually
have the stated permissions:

```json
{
  "10.1234/example": {
    "local_processing": true,
    "external_model": true,
    "basis": "Replace with the actual permission or license basis for this document"
  }
}
```

An explicit document denial overrides the optional `publisher-policy.json`
provider record. No permission file is supplied by default, and MCP tools cannot
create these records. Permission is rechecked for text and page reads.

IEEE and Springer Nature Link adapters are experimental. Elsevier and Wiley
retrieval are unimplemented.

## ITU OpenAthens

ITU reaches most subscribed publishers through OpenAthens, which signs in with the
library account: your student or staff number and the library catalog password, not
the central ITU password. Set them privately to turn the OpenAthens adapter on:

```dotenv
PAPERBRIDGE_OPENATHENS_USERNAME=student-or-staff-number
PAPERBRIDGE_OPENATHENS_PASSWORD=library-account-password
```

Without these, `NINOVA_LIBRARY_ID` and `NINOVA_LIBRARY_PIN` are used, since the library
catalog login is the same account. The adapter opens the DOI through
`go.openathens.net/redirector/itu.edu.tr`, fills the form only on ITU's
`login.openathens.net` page, and reads the PDF named by the article page. Permission
records use the provider name `openathens`.

| Publisher | Observed in my tests |
| --- | --- |
| ASME Digital Collection | Works; ITU routes it through the OpenAthens proxy |
| Cambridge Core | Works from a home connection; a VPN address gets a verification page |
| ACS, Oxford Academic, Taylor & Francis, SAGE, ACM | Tried, but often stop at a verification page or refuse the PDF |

A publisher's verification page (for example Cloudflare's "Just a moment...") ends the
attempt with `publisher_interaction_required`. PaperBridge never solves or bypasses it.
ScienceDirect, Wiley, AIAA, ASCE, APS, RSC, Emerald and AIP showed that page every time. Reported open-access locations return public links
instead of starting institutional acquisition. Missing open-access metadata is
not proof of a paywall. Existing authorized archive entries remain readable.

## Important settings

| Variable | Purpose |
| --- | --- |
| `PAPERBRIDGE_DATA_DIR` | Your private archive directory |
| `PAPERBRIDGE_CONTACT_EMAIL` | Your contact email for metadata APIs; do not copy someone else's |
| `PAPERBRIDGE_BROWSER_ENGINE` | `chromium` or `firefox`; unset means no managed retrieval |
| `PAPERBRIDGE_BROWSER_HEADLESS` | `1` by default; `0` requires a server display |
| `PAPERBRIDGE_CHROMIUM_EXECUTABLE` | Optional absolute path to your provisioned full Chromium |
| `PAPERBRIDGE_BROWSER_IDLE_SECONDS` | Default 90; idle browser cleanup, not a per-request deadline |
| `PAPERBRIDGE_SPRINGER_BROWSER_ENGINE` | Optional Springer override; otherwise inherits the common engine |
| `PAPERBRIDGE_PUBLISHER_PROXY` | Optional pre-existing loopback HTTP CONNECT proxy; creates no proxy or VPN |
| `PAPERBRIDGE_VISUAL_PROBE` | Opt-in synthetic client image diagnostic |

Use `papers_get_text` for the relevant excerpts and follow `next_id` when more
text is needed. Select PDF pages based on the text and captions. `papers_get_page`
returns a native image. `papers_view_pages` prepares a selection for compatible
MCP Apps clients, with an 8 MB image budget. Automatic attachment is per-call
opt-in. Tool success or host acknowledgement does not establish that the model
received and inspected the pixels.

## Try the original synthetic example

From the repository root and activated virtual environment:

```bash
paperbridge --data-dir .paperbridge-demo ingest examples/paperbridge/demo-note.txt --title "PaperBridge demo" --url https://example.org/paperbridge-demo --rights-file examples/paperbridge/demo-rights.json
paperbridge --data-dir .paperbridge-demo search rocket --local-only
```

The fixture is original demonstration text, not a scientific publication. Its
permission record applies only to that fixture. Use the returned `first_chunk_id`
with `paperbridge --data-dir .paperbridge-demo fetch ID` to read it.
