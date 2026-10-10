# Contributing

I maintain this fork independently at **tzi4/itu-mcp**. Create topic branches
from this repository's `main` and target pull requests here. Changes to the
original project require a separate, explicit decision; no upstream PR is
created automatically.

## Ideas, bugs and feature requests

You don't need to submit code to contribute. If something doesn't work, or you
have an idea for a new feature, [open an issue](https://github.com/tzi4/itu-mcp/issues).
I'd love to hear what you're trying to do and how I can make the project more
useful for you.

For feature requests, describe the workflow you want and what's missing today.
For bugs, include the affected tool, what you expected, what happened, and your
OS, Python version and AI client. Include only logs or examples you've removed
private information from; never share credentials, cookies, private mail or
student data. Please write issues and pull requests in English.

## Pull requests

Write new documentation, public names, diagrams, comments and explanatory
text in English. Use **ITU MCP + PaperBridge** for the fork's public name.
Keep original upstream attribution. Exact university labels, API fields and
multilingual test fixtures may retain their source language when required
for accurate parsing or testing.

Keep changes focused. Preserve source attribution and document public behavior
and configuration. Include relevant offline tests for parser changes,
permission boundaries, retrieval behavior and protocol changes.

```bash
python -m pip install -e .
NINOVA_USERNAME=dummy NINOVA_PASSWORD=dummy python -m unittest discover -s tests -v
```

Use synthetic fixtures. Do not include credentials, cookies, private mail,
captured student data, browser profiles, PDFs or document databases.
Live network and account tests are separate from the offline suite.

PaperBridge runs in the importing MCP server's process. Keep metadata discovery,
institution configuration, retrieval and storage separate. Report unavailable
full text and unread pages accurately. A recorded permission basis documents
existing rights; it does not create them. MCP tools must not grant permissions
or ingest arbitrary local paths.
